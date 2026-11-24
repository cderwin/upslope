"""What the archive needs from a source of model runs: which runs are complete, and each run's fields."""

from collections import OrderedDict
from datetime import datetime
from typing import Protocol

from upslope.archive.config import ArchiveModel
from upslope.archive.conversions import UpstreamField
from upslope.archive.grid import RegionGrid

# Upstream fields kept per run, so a field two variables need (a wind's u and v) is read once
FIELD_CACHE_SIZE = 4


class RunReader(Protocol):
    """One run of a model, read field by field (blocking)."""

    def read(self, name: str) -> UpstreamField:
        """One upstream field over the model's region, at every lead time the run has.

        Raises MissingField if the source doesn't publish it.
        """
        ...

    @property
    def grid(self) -> RegionGrid:
        """The region's grid; valid once a field has been read."""
        ...

    def close(self) -> None:
        """Release what the reader holds (temporary files, cached fields)."""
        ...


class ArchiveSource(Protocol):
    """A kind of source (dynamical.org, Open-Meteo). Its methods block; callers run them in threads.

    A source may cache what it opens for the length of a check; `end_check` drops it, so the next check sees
    newly published runs.
    """

    def is_complete(self, model: ArchiveModel, initialized: datetime) -> bool:
        """Whether a run is fully published upstream."""
        ...

    def open_run(self, model: ArchiveModel, initialized: datetime) -> RunReader:
        """A reader for one run."""
        ...

    def end_check(self) -> None:
        """Drop anything cached during a check."""
        ...


class FieldCache:
    """The few upstream fields of a run read most recently."""

    def __init__(self, size: int = FIELD_CACHE_SIZE) -> None:
        self.size = size
        self._fields: OrderedDict[str, UpstreamField] = OrderedDict()

    def get(self, name: str) -> UpstreamField | None:
        field = self._fields.get(name)
        if field is not None:
            self._fields.move_to_end(name)
        return field

    def put(self, name: str, field: UpstreamField) -> UpstreamField:
        self._fields[name] = field
        while len(self._fields) > self.size:
            self._fields.popitem(last=False)
        return field

    def clear(self) -> None:
        self._fields.clear()
