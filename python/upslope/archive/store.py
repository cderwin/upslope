"""Archived runs in the private bucket.

```
archive/status.json                                   the archiver's health (ArchiveStatus)
archive/<model>/index.json                            the model's archived runs, newest first (ArchiveIndex)
archive/<model>/runs/<YYYYMMDDTHH>.zarr/...           one run (see `upslope.archive.writer`)
archive/dem/index.json                                the current elevation model build (DemIndex)
archive/dem/<YYYYMMDDTHHMM>.zarr/...                  one build (see `upslope.archive.dem`)
```

Only the archiver (`upslope archive-loop`, one process) writes here. A run's chunks are uploaded before its
metadata, and its root `zarr.json` (with the consolidated metadata) last; the index lists a run only once it's
fully uploaded and stops listing it before it's deleted, so a reader that follows the index never finds a
partial run. The elevation model is built by hand (`upslope archive-dem`) and published the same way: uploaded,
then named in its index, and only then are older builds deleted.
"""

from datetime import UTC, datetime
from pathlib import Path

from attrs import define, field
from cattrs.preconf.json import make_converter

from upslope.clients.storage import StorageClient
from upslope.logging import get_logger

logger = get_logger(__name__)

PREFIX = "archive"
INDEX = "index.json"
STATUS = f"{PREFIX}/status.json"
RUN_FORMAT = "%Y%m%dT%H"
DEM_PREFIX = f"{PREFIX}/dem"
DEM_BUILD_FORMAT = "%Y%m%dT%H%M"

converter = make_converter()


@define
class ArchivedRun:
    """One archived run.

    Attributes:
        init_time: When the run started.
        fingerprint: The config's fingerprint for the model when the run was archived (see `ArchiveConfig.fingerprint`).
        archived_at: When it was archived.
        variables: The standard variables it has.
    """

    init_time: datetime
    fingerprint: str
    archived_at: datetime
    variables: list[str] = field(factory=list)


@define
class ArchiveIndex:
    """A model's archived runs.

    Attributes:
        model: Model id.
        runs: Archived runs, newest first. The first is the newest complete run.
        updated_at: When the index was last written.
    """

    model: str
    runs: list[ArchivedRun] = field(factory=list)
    updated_at: datetime | None = None

    def run(self, initialized: datetime) -> ArchivedRun | None:
        """The archived run started at `initialized`, if there is one."""
        return next((run for run in self.runs if run.init_time == initialized), None)


@define
class ModelArchiveStatus:
    """What happened to one model in a check.

    Attributes:
        model: Model id.
        state: `ok` (archived runs, or nothing new), `failed`, or `skipped` (still busy with an earlier check).
        runs: Runs archived in the check, as `YYYYMMDDTHH`.
        error: Why it failed.
        warnings: What was left out of runs it archived (variables the upstream lacks, lead times off the axis).
        seconds: Time taken.
    """

    model: str
    state: str
    runs: list[str] = field(factory=list)
    error: str | None = None
    warnings: list[str] = field(factory=list)
    seconds: float = 0.0


@define
class ArchiveStatus:
    """The archiver's latest check.

    Attributes:
        state: `running`, `ok` or `failed` (a model failed).
        check: Checks since the archiver started counting.
        started_at: When the check started.
        finished_at: When it finished, unless running.
        next_check_at: When the next check is due.
        last_success_at: When a check last finished without a failure.
        models: Each model's outcome in the last finished check.
    """

    state: str
    check: int
    started_at: datetime
    finished_at: datetime | None = None
    next_check_at: datetime | None = None
    last_success_at: datetime | None = None
    models: list[ModelArchiveStatus] = field(factory=list)


@define
class DemIndex:
    """The current elevation model build.

    Attributes:
        build: Its folder under `archive/dem/`, e.g. `20261125T1200.zarr`.
        built_at: When it was built.
        bounds: (west, south, east, north) in degrees.
        cells: Cells by source: `3dep`, `glo30` and `none`.
    """

    build: str
    built_at: datetime
    bounds: list[float]
    cells: dict[str, int] = field(factory=dict)


def run_key(initialized: datetime) -> str:
    """A run's folder name, e.g. `20261123T12.zarr`."""
    return f"{initialized.astimezone(UTC):{RUN_FORMAT}}.zarr"


def _upload_order(path: Path) -> tuple[int, str]:
    # chunks first, then each array's metadata, then the group's (with the consolidated metadata) last
    relative = path.as_posix()
    if relative == "zarr.json":
        return 2, relative
    return (1 if path.name == "zarr.json" else 0), relative


@define
class ArchiveStore:
    """Reads and writes the archive in the private bucket.

    Attributes:
        storage: The private bucket.
    """

    storage: StorageClient

    @staticmethod
    def run_prefix(model: str, initialized: datetime) -> str:
        """Key prefix of a run's store, ending in `/`."""
        return f"{PREFIX}/{model}/runs/{run_key(initialized)}/"

    def upload_run(self, model: str, initialized: datetime, folder: Path) -> int:
        """Upload a run's store, replacing any earlier copy of it.

        Args:
            model: Model id.
            initialized: When the run started.
            folder: The local store written by `RunWriter`.

        Returns:
            Bytes uploaded.
        """
        self.delete_run(model, initialized)  # a run archived again (the config changed) may have fewer objects
        return self._upload_store(self.run_prefix(model, initialized), folder)

    def _upload_store(self, prefix: str, folder: Path) -> int:
        """Upload a local Zarr store under `prefix`, its root metadata last; returns the bytes uploaded."""
        files = sorted((p for p in folder.rglob("*") if p.is_file()), key=lambda p: _upload_order(p.relative_to(folder)))
        total = 0
        for path in files:
            relative = path.relative_to(folder).as_posix()
            content_type = "application/json" if path.name == "zarr.json" else "application/octet-stream"
            self.storage.upload_file(path, prefix + relative, content_type=content_type)
            total += path.stat().st_size
        return total

    def delete_run(self, model: str, initialized: datetime) -> None:
        """Delete every object of a run."""
        if keys := self.storage.list_keys(self.run_prefix(model, initialized)):
            self.storage.delete_keys(keys)

    def list_run_folders(self, model: str) -> list[datetime]:
        """Runs with objects in the bucket, listed or not (e.g. left by an archive that stopped part way)."""
        prefix = f"{PREFIX}/{model}/runs/"
        found = set()
        for key in self.storage.list_keys(prefix):
            name = key.removeprefix(prefix).split("/", 1)[0]
            try:
                found.add(datetime.strptime(name.removesuffix(".zarr"), RUN_FORMAT).replace(tzinfo=UTC))
            except ValueError:
                continue
        return sorted(found, reverse=True)

    def read_index(self, model: str) -> ArchiveIndex:
        """A model's index; empty if it has none yet."""
        stored = self.storage.get_bytes(f"{PREFIX}/{model}/{INDEX}")
        return ArchiveIndex(model) if stored is None else converter.loads(stored.data, ArchiveIndex)

    def write_index(self, index: ArchiveIndex) -> None:
        self.storage.put_bytes(f"{PREFIX}/{index.model}/{INDEX}", converter.dumps(index).encode(), "application/json")

    def read_status(self) -> ArchiveStatus | None:
        stored = self.storage.get_bytes(STATUS)
        return None if stored is None else converter.loads(stored.data, ArchiveStatus)

    def write_status(self, status: ArchiveStatus) -> None:
        self.storage.put_bytes(STATUS, converter.dumps(status).encode(), "application/json")

    def read_dem_index(self) -> DemIndex | None:
        """The current elevation model build, if one has been published."""
        stored = self.storage.get_bytes(f"{DEM_PREFIX}/{INDEX}")
        return None if stored is None else converter.loads(stored.data, DemIndex)

    def publish_dem(self, folder: Path, index: DemIndex) -> int:
        """Upload an elevation model build, make it the current one, then delete every other build.

        Args:
            folder: The local store written by `DemBuilder`.
            index: The build's index; `build` names its folder.

        Returns:
            Bytes uploaded.
        """
        total = self._upload_store(f"{DEM_PREFIX}/{index.build}/", folder)
        self.storage.put_bytes(f"{DEM_PREFIX}/{INDEX}", converter.dumps(index).encode(), "application/json")
        stale = [key for key in self.storage.list_keys(f"{DEM_PREFIX}/")
                 if key != f"{DEM_PREFIX}/{INDEX}" and not key.startswith(f"{DEM_PREFIX}/{index.build}/")]
        if stale:
            self.storage.delete_keys(stale)
        return total
