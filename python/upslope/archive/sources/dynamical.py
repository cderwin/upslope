"""dynamical.org datasets as a source of runs for the archive.

Each dataset is opened once per check (one consistent snapshot) and shared by every model reading it. A field is
read for every lead time of a run at once, over the model's region; virtual datasets decode the upstream GRIB
messages as they're read, so this is the slow part of archiving a run.
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

import httpx
import numpy as np
import xarray as xr
from attrs import define, field

from upslope.archive.config import ArchiveModel, DynamicalRunsConfig
from upslope.archive.conversions import MissingField, UpstreamField
from upslope.archive.grid import RegionGrid
from upslope.archive.sources.base import FieldCache
from upslope.logging import get_logger, log_duration
from upslope.models.dynamical.catalog import open_dataset
from upslope.models.dynamical.source import CATALOG_TIMEOUT, REFERENCE_VARIABLE, GridWindow, grid_window

logger = get_logger(__name__)


def _dataset_id(model: ArchiveModel) -> str:
    if not isinstance(model.source, DynamicalRunsConfig):
        raise TypeError(f"model {model.id} isn't a dynamical.org model")
    return model.source.dataset


def _lead_hours(dataset: xr.Dataset) -> np.ndarray:
    return dataset["lead_time"].values / np.timedelta64(1, "h")


def _init_index(dataset: xr.Dataset, initialized: datetime) -> int | None:
    target = np.datetime64(initialized.astimezone(UTC).replace(tzinfo=None), "ns")
    matches = np.nonzero(dataset["init_time"].values == target)[0]
    return int(matches[0]) if matches.size else None


def read_region(dataset: xr.Dataset, window: GridWindow, variable: str, init: int, lead: int | slice = slice(None)) -> np.ndarray:
    """A variable of one run over a window, north up, as float32: (lead, rows, cols), or (rows, cols) for one lead."""
    array = dataset[variable].isel({"init_time": init, "lead_time": lead, window.y_dim: window.rows, window.x_dim: window.cols})
    if array.dims[-2:] != (window.y_dim, window.x_dim) or array.ndim != (3 if isinstance(lead, slice) else 2):
        raise ValueError(f"{variable} has dimensions {dataset[variable].dims} besides the grid (e.g. ensemble members)")
    values = np.asarray(array.values, dtype=np.float32)
    return values[..., ::-1, :] if window.flip else values


@define
class DynamicalRunReader:
    """One run of a dynamical.org dataset (see `RunReader`)."""

    dataset: xr.Dataset
    window: GridWindow
    init: int
    name: str
    _cache: FieldCache = field(factory=FieldCache)

    @property
    def grid(self) -> RegionGrid:
        return RegionGrid(self.window.crs, self.window.transform, (self.window.rows.stop - self.window.rows.start, self.window.cols.stop - self.window.cols.start))

    def read(self, name: str) -> UpstreamField:
        if (cached := self._cache.get(name)) is not None:
            return cached
        if name not in self.dataset:
            raise MissingField(f"{self.name} has no variable {name!r}")
        with log_duration(logger, f"read {name}"):
            values = read_region(self.dataset, self.window, name, self.init)
        units = str(self.dataset[name].attrs.get("units", ""))
        return self._cache.put(name, UpstreamField(_lead_hours(self.dataset), values, units))

    def close(self) -> None:
        self._cache.clear()


@define
class DynamicalRunSource:
    """Runs from dynamical.org's catalog (see `ArchiveSource`).

    Attributes:
        open: Opens a catalog dataset by id; defaults to the live catalog (tests pass in-memory datasets).
    """

    open: Callable[[str], xr.Dataset] = field(factory=lambda: partial(open_dataset, client=httpx.Client(timeout=CATALOG_TIMEOUT)))
    _lock: threading.Lock = field(factory=threading.Lock, init=False)
    # one lock per dataset, held while it's opened, so models reading other datasets don't wait for it
    _opening: dict[str, threading.Lock] = field(factory=dict, init=False)
    _datasets: dict[str, xr.Dataset] = field(factory=dict, init=False)

    def dataset(self, model: ArchiveModel) -> xr.Dataset:
        """The model's dataset, opened once per check."""
        dataset_id = _dataset_id(model)
        with self._lock:
            if (dataset := self._datasets.get(dataset_id)) is not None:
                return dataset
            opening = self._opening.setdefault(dataset_id, threading.Lock())
        with opening:
            with self._lock:
                if (dataset := self._datasets.get(dataset_id)) is not None:
                    return dataset
            with log_duration(logger, f"opened {dataset_id}"):
                dataset = self.open(dataset_id)
            with self._lock:
                self._datasets[dataset_id] = dataset
            return dataset

    def is_complete(self, model: ArchiveModel, initialized: datetime) -> bool:
        """Whether the reference variable has data over the region at the run's last lead time the model keeps."""
        dataset = self.dataset(model)
        init = _init_index(dataset, initialized)
        if init is None:
            return False
        leads = _lead_hours(dataset)
        kept = [i for i, lead in enumerate(leads) if lead in set(model.lead_hours)]
        if not kept:
            raise ValueError(f"none of {model.id}'s lead times are lead times of {_dataset_id(model)}")
        values = read_region(dataset, grid_window(dataset, model.region), REFERENCE_VARIABLE, init, kept[-1])
        return bool(np.isfinite(values).any())

    def open_run(self, model: ArchiveModel, initialized: datetime) -> DynamicalRunReader:
        dataset = self.dataset(model)
        init = _init_index(dataset, initialized)
        if init is None:
            raise MissingField(f"{_dataset_id(model)} has no run {initialized:%Y-%m-%d %HZ}")
        return DynamicalRunReader(dataset, grid_window(dataset, model.region), init, _dataset_id(model))

    def end_check(self) -> None:
        with self._lock:
            self._opening.clear()
            self._datasets.clear()
