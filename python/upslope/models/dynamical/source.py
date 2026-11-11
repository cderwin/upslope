"""dynamical.org as a forecast source: frames rendered from gridded model data in its Icechunk/Zarr datasets.

Each check opens every dataset once, as one consistent snapshot. A frame is the model's region of one forecast
hour: read from the dataset's native grid, combined into the layer's field, converted to display units, warped
to Web Mercator and colored with the layer's scale. The numbers come back too, for the map's value tooltip.

Virtual datasets fill in a run as the model publishes it. An hour counts as published once the reference
variable (`temperature_2m`) has data over the region; missing chunks read back as NaN.

Reading and rendering are blocking (Icechunk, xarray, GDAL), so the async methods run them in threads, a few
at a time; models are exported concurrently, so the per-check caches are shared under a lock. That lock is never
held while reading: opening a dataset can take minutes when the upstream is slow, so each dataset is opened under
its own lock, and only models reading that dataset wait for it.
"""

import asyncio
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
import numpy as np
import xarray as xr
from affine import Affine
from attrs import define, field
from pyproj import Transformer
from rasterio.warp import Resampling

from upslope.logging import get_logger, log_duration
from upslope.models.config import AnyLayerConfig, DynamicalLayerConfig, DynamicalSourceConfig, ModelConfig
from upslope.models.dynamical.catalog import open_dataset
from upslope.models.dynamical.scales import NODATA_INDEX, SCALES, convert
from upslope.models.source import NotPublished, RenderedFrame
from upslope.raster import footprint, warp_to_web_mercator, write_geotiff

logger = get_logger(__name__)

# Present in every forecast dataset, and never missing over land: whether it has data marks an hour as published
REFERENCE_VARIABLE = "temperature_2m"
# Grid cells read beyond the region on each side, so warping and interpolation have data at the edges
REGION_MARGIN_CELLS = 2
# Coarse grids are upsampled to about this many pixels across, so bilinear interpolation shows smooth fields
MIN_FRAME_WIDTH = 600
# Region reads kept per check, so a field read for one layer or interval is reused by the next
FIELD_CACHE_SIZE = 64
# Blocking calls (rendering a frame, finding the latest run) at once across models: bounds memory and CPU, and
# keeps a slow upstream from occupying every worker thread, while letting reads overlap
RENDER_CONCURRENCY = 3
# The catalog lookup is one small JSON document
CATALOG_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@define(frozen=True)
class GridWindow:
    """The part of a dataset's grid that covers a model's region, ready to place on a map.

    Attributes:
        y_dim: Name of the grid's row dimension (e.g. `latitude` or `y`).
        x_dim: Name of the grid's column dimension.
        rows: Row indices of the window.
        cols: Column indices of the window.
        flip: Rows run south to north in the dataset, so reads are reversed to put north up.
        transform: Corner-based (col, row) -> `crs` coordinates, for the north-up window.
        crs: The grid's CRS, as WKT.
    """

    y_dim: str
    x_dim: str
    rows: slice
    cols: slice
    flip: bool
    transform: Affine
    crs: str


def _spacing(coords: np.ndarray, name: str) -> float:
    steps = np.diff(coords)
    if not steps.size or not np.allclose(steps, steps[0], rtol=1e-4):
        raise ValueError(f"grid coordinate {name} isn't regularly spaced")
    return float(steps[0])


def _span(coords: np.ndarray, low: float, high: float, name: str) -> slice:
    inside = np.nonzero((coords >= low) & (coords <= high))[0]
    if not inside.size:
        raise ValueError(f"the region is outside the grid along {name}")
    return slice(int(inside.min()), int(inside.max()) + 1)


def grid_window(dataset: xr.Dataset, region: tuple[float, float, float, float]) -> GridWindow:
    """Find the window of a dataset's grid covering a region, with a margin of a few cells.

    Works for any grid with regularly spaced 1-D coordinates in the CRS of its `spatial_ref`: latitude and
    longitude, or projected x and y (e.g. HRRR's Lambert conformal grid).

    Args:
        dataset: Forecast dataset.
        region: (west, south, east, north) in degrees.

    Returns:
        The window. Raises ValueError if the grid is irregular or doesn't cover the region.
    """
    y_dim, x_dim = dataset[REFERENCE_VARIABLE].dims[-2:]
    xs = dataset[x_dim].values.astype(np.float64)
    ys = dataset[y_dim].values.astype(np.float64)
    dx, dy = _spacing(xs, x_dim), _spacing(ys, y_dim)
    if dx <= 0:
        raise ValueError(f"grid coordinate {x_dim} must increase")
    crs = dataset["spatial_ref"].attrs["crs_wkt"]

    # the region's outline, densified so curved edges in the grid's projection are covered
    west, south, east, north = region
    t = np.linspace(0.0, 1.0, 64)
    lons = np.concatenate([west + (east - west) * t, np.full_like(t, east), east - (east - west) * t, np.full_like(t, west)])
    lats = np.concatenate([np.full_like(t, south), south + (north - south) * t, np.full_like(t, north), north - (north - south) * t])
    px, py = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lons, lats)
    margin_x, margin_y = REGION_MARGIN_CELLS * abs(dx), REGION_MARGIN_CELLS * abs(dy)
    cols = _span(xs, np.min(px) - margin_x, np.max(px) + margin_x, x_dim)
    rows = _span(ys, np.min(py) - margin_y, np.max(py) + margin_y, y_dim)

    left = xs[cols.start] - dx / 2
    if dy < 0:  # north to south already
        transform = Affine(dx, 0.0, left, 0.0, dy, ys[rows.start] - dy / 2)
        return GridWindow(y_dim, x_dim, rows, cols, False, transform, crs)
    transform = Affine(dx, 0.0, left, 0.0, -dy, ys[rows.stop - 1] + dy / 2)
    return GridWindow(y_dim, x_dim, rows, cols, True, transform, crs)


def _lead_hours(dataset: xr.Dataset) -> list[float]:
    return list(dataset["lead_time"].values / np.timedelta64(1, "h"))


@define
class DynamicalSource:
    """Renders frames from dynamical.org forecast datasets.

    Attributes:
        open: Opens a catalog dataset by id; defaults to the live catalog (tests pass in-memory datasets).
        concurrency: Most blocking calls (frames rendered, latest runs found) at once.
    """

    open: Callable[[str], xr.Dataset] = field(factory=lambda: partial(open_dataset, client=httpx.Client(timeout=CATALOG_TIMEOUT)))
    concurrency: int = RENDER_CONCURRENCY
    _checking: bool = field(default=False, init=False)
    # guards the caches below; never held while a dataset is opened or read
    _lock: threading.RLock = field(factory=threading.RLock, init=False)
    _renders: asyncio.Semaphore = field(init=False)
    # incremented as each check ends, so an open that outlives its check doesn't leave its snapshot for the next
    _generation: int = field(default=0, init=False)
    # one lock per dataset, held while it's opened, so callers wait only for the dataset they need
    _opening: dict[str, threading.Lock] = field(factory=dict, init=False)
    _datasets: dict[str, xr.Dataset] = field(factory=dict, init=False)
    _windows: dict[str, GridWindow] = field(factory=dict, init=False)
    _fields: OrderedDict[tuple, tuple[np.ndarray, str]] = field(factory=OrderedDict, init=False)
    _published: dict[tuple, bool] = field(factory=dict, init=False)

    def __attrs_post_init__(self) -> None:
        self._renders = asyncio.Semaphore(self.concurrency)

    @asynccontextmanager
    async def check(self) -> AsyncIterator[None]:
        self.begin_check()
        try:
            yield
        finally:
            self.end_check()

    def begin_check(self) -> None:
        """Start a check (what `check` does, for blocking callers such as the CLI)."""
        self._checking = True

    def end_check(self) -> None:
        """End a check; the next one sees the datasets' newest snapshots."""
        with self._lock:
            self._checking = False
            self._generation += 1
            # a thread still opening a dataset (its model timed out) keeps its old lock and doesn't block the next check
            self._opening.clear()
            self._datasets.clear()
            self._windows.clear()
            self._fields.clear()
            self._published.clear()

    def revision(self, model: ModelConfig) -> int | None:
        # frames depend only on the config (fingerprinted) and the data
        return None

    async def latest_complete_run(self, model: ModelConfig, now: datetime) -> datetime | None:
        async with self._renders:
            return await asyncio.to_thread(self.find_latest_complete_run, model, now)

    def find_latest_complete_run(self, model: ModelConfig, now: datetime) -> datetime | None:
        """The newest run with the last hour of every layer published (blocking)."""
        last = max(layer.hours.last for layer in model.layers)
        for initialized in model.initializations(now):
            if self.is_published(model, initialized, last):
                return initialized
        return None

    async def render_frame(self, model: ModelConfig, layer: AnyLayerConfig, initialized: datetime, hour: int, out: Path) -> RenderedFrame:
        async with self._renders:
            return await asyncio.to_thread(self.render, model, layer, initialized, hour, out)

    def render(self, model: ModelConfig, layer: AnyLayerConfig, initialized: datetime, hour: int, out: Path) -> RenderedFrame:
        """Render a frame (blocking); see `ForecastSource.render_frame`."""
        layer = self._dynamical(layer)
        scale = SCALES[layer.scale]
        values = self.field_values(model, layer, initialized, hour)
        window = self._window(model)
        warped, transform = warp_to_web_mercator(
            values[np.newaxis], window.crs, window.transform, nodata=np.nan, resampling=Resampling.bilinear,
            min_width=MIN_FRAME_WIDTH,
        )
        extent = footprint(window.crs, window.transform, values.shape[1], values.shape[0])
        write_geotiff(out, scale.colorize(warped[0])[np.newaxis], transform, NODATA_INDEX, scale.colormap(), extent=extent)
        return RenderedFrame(values=warped[0], legend=scale.legend())

    # --- fields

    def field_values(self, model: ModelConfig, layer: DynamicalLayerConfig, initialized: datetime, hour: int) -> np.ndarray:
        """One forecast hour of a layer's field over the model's region, on the dataset's grid, in display units.

        Raises NotPublished if an hour it needs isn't published yet, and ValueError if the config doesn't fit
        the dataset (an unknown variable, an hour that isn't a lead time, units the scale can't convert).
        """
        spec = layer.field
        variable = spec.variables[0]
        if spec.derive == "change":
            # a run total's growth over the interval, e.g. 3-hour precipitation
            self._require(model, initialized, [hour - spec.hours, hour])
            now, units = self._read(model, variable, initialized, hour)
            before, _ = self._read(model, variable, initialized, hour - spec.hours)
            values = now - before
        elif spec.derive == "rate_sum":
            # each step's rate is averaged since the previous step, so the interval's total is rate x step length
            leads = _lead_hours(self._dataset(model))
            steps = [(lead, previous) for previous, lead in zip(leads, leads[1:]) if hour - spec.hours < lead <= hour]
            if not steps or steps[0][1] != hour - spec.hours:
                raise ValueError(f"{model.id}/{layer.id}: lead times don't divide hours {hour - spec.hours}-{hour}")
            self._require(model, initialized, [lead for lead, _ in steps])
            values = np.zeros_like(self._read(model, variable, initialized, steps[0][0])[0])
            for lead, previous in steps:
                rate, units = self._read(model, variable, initialized, lead)
                values = values + rate * (lead - previous) * 3600
            if not units.endswith(" s-1"):
                raise ValueError(f"{model.id}/{layer.id}: {variable} is in {units!r}, not a rate")
            units = units.removesuffix(" s-1")
        elif spec.derive == "wind_speed":
            self._require(model, initialized, [hour])
            u, units = self._read(model, variable, initialized, hour)
            v, _ = self._read(model, spec.variables[1], initialized, hour)
            values = np.hypot(u, v)
        else:
            self._require(model, initialized, [hour])
            values, units = self._read(model, variable, initialized, hour)
            if spec.derive == "freezing_level":
                # models mark a freezing level at or below the surface as NaN, 0 or a negative height; show all as 0
                values = np.where(np.isfinite(values) & (values > 0), values, 0.0).astype(np.float32)
        return convert(values, units, SCALES[layer.scale])

    def is_published(self, model: ModelConfig, initialized: datetime, hour: float) -> bool:
        """Whether a forecast hour of a run has data over the model's region yet."""
        key = (model.source.dataset, initialized, hour)
        with self._lock:
            known = self._published.get(key)
        if known is not None:
            return known
        if self._init_index(model, initialized) is None:
            return False  # not cached: the run may start before the next check
        values, _ = self._read(model, REFERENCE_VARIABLE, initialized, hour)
        published = bool(np.isfinite(values).any())
        with self._lock:
            self._published[key] = published
        return published

    def _require(self, model: ModelConfig, initialized: datetime, hours: list[float]) -> None:
        for hour in hours:
            if not self.is_published(model, initialized, hour):
                raise NotPublished(f"{model.id} {initialized:%Y-%m-%d %HZ} hour {hour:g}")

    # --- reading the dataset

    @staticmethod
    def _dynamical(layer: AnyLayerConfig) -> DynamicalLayerConfig:
        if not isinstance(layer, DynamicalLayerConfig):
            raise TypeError(f"layer {layer.id} isn't a dynamical.org layer")
        return layer

    def _dataset(self, model: ModelConfig) -> xr.Dataset:
        if not self._checking:
            raise RuntimeError("datasets are read inside DynamicalSource.check()")
        if not isinstance(model.source, DynamicalSourceConfig):
            raise TypeError(f"model {model.id} isn't a dynamical.org model")
        dataset_id = model.source.dataset
        with self._lock:
            if (dataset := self._datasets.get(dataset_id)) is not None:
                return dataset
            opening = self._opening.setdefault(dataset_id, threading.Lock())
            generation = self._generation
        with opening:
            with self._lock:
                if (dataset := self._datasets.get(dataset_id)) is not None:
                    return dataset  # opened while this caller waited
            with log_duration(logger, f"opened {dataset_id}"):
                dataset = self.open(dataset_id)
            with self._lock:
                if self._generation == generation:
                    self._datasets[dataset_id] = dataset
            return dataset

    def _window(self, model: ModelConfig) -> GridWindow:
        with self._lock:
            if (window := self._windows.get(model.id)) is not None:
                return window
        window = grid_window(self._dataset(model), model.region)
        with self._lock:
            return self._windows.setdefault(model.id, window)

    def _init_index(self, model: ModelConfig, initialized: datetime) -> int | None:
        target = np.datetime64(initialized.astimezone(UTC).replace(tzinfo=None), "ns")
        matches = np.nonzero(self._dataset(model)["init_time"].values == target)[0]
        return int(matches[0]) if matches.size else None

    def _lead_index(self, model: ModelConfig, hour: float) -> int:
        dataset = self._dataset(model)
        matches = np.nonzero(dataset["lead_time"].values == np.timedelta64(round(hour * 3600), "s"))[0]
        if not matches.size:
            raise ValueError(f"hour {hour:g} isn't a lead time of {model.source.dataset}")
        return int(matches[0])

    def _read(self, model: ModelConfig, variable: str, initialized: datetime, hour: float) -> tuple[np.ndarray, str]:
        """A variable over the model's region at one forecast hour, north up, as float32 with its units."""
        key = (model.id, variable, initialized, hour)
        with self._lock:
            if key in self._fields:
                self._fields.move_to_end(key)
                return self._fields[key]
        dataset = self._dataset(model)
        if variable not in dataset:
            raise ValueError(f"{model.source.dataset} has no variable {variable!r}")
        init = self._init_index(model, initialized)
        if init is None:
            raise NotPublished(f"{model.id} {initialized:%Y-%m-%d %HZ} hasn't started")
        window = self._window(model)
        array = dataset[variable].isel(
            {"init_time": init, "lead_time": self._lead_index(model, hour), window.y_dim: window.rows, window.x_dim: window.cols}
        )
        if array.ndim != 2:
            raise ValueError(f"{variable} has dimensions {array.dims} besides the grid (e.g. ensemble members)")
        values = np.asarray(array.values, dtype=np.float32)
        if window.flip:
            values = values[::-1]
        result = (values, str(dataset[variable].attrs.get("units", "")))
        with self._lock:
            self._fields[key] = result
            if len(self._fields) > FIELD_CACHE_SIZE:
                self._fields.popitem(last=False)
        return result

    # --- checking the config against the data

    def layer_problems(self, model: ModelConfig, layer: DynamicalLayerConfig, initialized: datetime) -> list[str]:
        """What keeps a layer from exporting every hour of a run: unknown variables, missing lead times or units
        the scale can't convert, else the hours not published yet. Empty if it would export fully."""
        dataset = self._dataset(model)
        problems = [f"{model.source.dataset} has no variable {v!r}" for v in layer.field.variables if v not in dataset]
        leads = set(_lead_hours(dataset))
        if missing := [hour for hour in layer.hours.hours if hour not in leads]:
            problems.append(f"hours {missing} aren't lead times of {model.source.dataset}")
        if problems:
            return problems
        units = str(dataset[layer.field.variables[0]].attrs.get("units", ""))
        if layer.field.derive == "rate_sum":
            units = units.removesuffix(" s-1")
        try:
            convert(np.zeros(1, dtype=np.float32), units, SCALES[layer.scale])
        except ValueError as e:
            return [str(e)]
        waiting = [hour for hour in layer.hours.hours if not self.is_published(model, initialized, hour)]
        return [f"hours {waiting} aren't published yet"] if waiting else []

