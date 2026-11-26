"""Each model's newest archived run, and the elevation model, kept on the server's local disk for point forecasts.

Runs are small (the newest of every model is ~350 MB) and change a few times a day, so the server keeps a copy
rather than reading the bucket per request: a point then reads a few chunks from local disk. In the background,
every `refresh_interval` seconds, each model's `index.json` and the DEM's are read with their ETags (an unchanged
index costs one small request). A new run is downloaded beside the current one, under a `.partial` name, renamed
into place once complete, opened, and only then swapped in, so requests always read a complete run. The run before
the current one is kept until the next swap, so a request that started reading it just before a swap finishes
safely; older ones are deleted. A downloaded copy that can't be opened (a corrupt shard index, say) is deleted so
the next refresh downloads it again, and the previous run stays in use meanwhile.

The server's disk is wiped when its machine restarts, so a fresh start downloads everything (~1.2 GB with the
DEM). Requests wait up to `ready_timeout` for that; after it, a model still downloading is reported as loading.
Only the server reads this folder.

```
<directory>/<model>/<YYYYMMDDTHH>.zarr/          a model's run, as stored under archive/<model>/runs/
<directory>/dem/<YYYYMMDDTHHMM>.zarr/            the DEM, as stored under archive/dem/
<directory>/.../*.partial/                       a download in progress (removed at startup)
```
"""

import asyncio
import shutil
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from attrs import define, field

from upslope.archive.config import ArchiveConfig, ArchiveModel
from upslope.archive.point import (
    Elevation,
    OpenRun,
    PointForecast,
    RunPoint,
    build_point_forecast,
    read_elevation,
    read_run_point,
)
from upslope.archive.store import DEM_PREFIX, INDEX, PREFIX, ArchiveIndex, ArchiveStore, DemIndex, converter, run_key
from upslope.clients.storage import StorageClient
from upslope.logging import get_logger, log_duration

if TYPE_CHECKING:
    import zarr

logger = get_logger(__name__)

PARTIAL = ".partial"
# Objects downloaded at once per run; the storage client keeps 32 connections
DOWNLOAD_THREADS = 8
# Models (or the DEM) downloaded at once
DOWNLOAD_CONCURRENCY = 2
# How long a request waits for the first downloads after a start
READY_TIMEOUT = 15.0


def download_prefix(storage: StorageClient, prefix: str, folder: Path) -> int:
    """Download every object under `prefix` into `folder`, keeping their relative paths (blocking).

    Returns:
        Bytes downloaded.
    """
    keys = storage.list_keys(prefix)
    if not keys:
        raise FileNotFoundError(f"nothing stored under {prefix}")

    def one(key: str) -> int:
        stored = storage.get_object(key)
        if stored is None or stored.body is None:
            raise FileNotFoundError(f"{key} disappeared while downloading")
        path = folder / key.removeprefix(prefix)
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with path.open("wb") as out:
            for chunk in stored.body:
                out.write(chunk)
                size += len(chunk)
        return size

    with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool:
        return sum(pool.map(one, keys))


def _replace_with_download(storage: StorageClient, prefix: str, target: Path) -> int:
    """Download a store to `target` through a partial folder renamed once complete (blocking)."""
    partial = target.with_name(target.name + PARTIAL)
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    try:
        size = download_prefix(storage, prefix, partial)
        partial.rename(target)
        return size
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise


def _remove_others(folder: Path, keep: set[str]) -> None:
    """Delete the stores in `folder` other than those named."""
    if not folder.is_dir():
        return
    for path in folder.iterdir():
        if path.name not in keep:
            logger.info("removing cached %s", path)
            shutil.rmtree(path, ignore_errors=True)


@define
class _Current:
    """What's in use for a model (or the DEM), and what it replaced (kept until the next swap)."""

    name: str
    previous: str | None = None


@define
class PointTimings:
    """How long a point forecast's phases took, in seconds.

    Attributes:
        read: Reading every model's point and the point's elevation (concurrently).
        build: Putting them on one hourly axis (`build_point_forecast`).
    """

    read: float = 0.0
    build: float = 0.0


@define
class ArchiveCache:
    """Keeps each model's newest run and the DEM on local disk, and answers point forecasts from them.

    Attributes:
        storage: The private bucket.
        config: The archive config: which models to keep and their variables.
        directory: Local folder for the copies.
        refresh_interval: Seconds between checks for new runs.
        ready_timeout: Most seconds a request waits for the first downloads.
        sleep: Sleep function, for tests.
    """

    storage: StorageClient
    config: ArchiveConfig
    directory: Path
    refresh_interval: float = 60.0
    ready_timeout: float = READY_TIMEOUT
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _runs: dict[str, OpenRun] = field(factory=dict, init=False)
    _dem: "zarr.Group | None" = field(default=None, init=False)
    _current: dict[str, _Current] = field(factory=dict, init=False)
    _etags: dict[str, str | None] = field(factory=dict, init=False)
    _ready: asyncio.Event = field(factory=asyncio.Event, init=False)
    _limit: asyncio.Semaphore = field(factory=lambda: asyncio.Semaphore(DOWNLOAD_CONCURRENCY), init=False)
    _lock: asyncio.Lock = field(factory=asyncio.Lock, init=False)

    @property
    def ready(self) -> bool:
        """Whether the first refresh has finished (whether or not every download succeeded)."""
        return self._ready.is_set()

    def _clean_partials(self) -> None:
        if self.directory.is_dir():
            for partial in self.directory.glob(f"*/*{PARTIAL}"):
                shutil.rmtree(partial, ignore_errors=True)

    async def refresh(self) -> None:
        """Check every model's index and the DEM's, downloading what's new. A failure is logged and confined to
        that model (or the DEM); the next refresh tries again."""
        async with self._lock:
            if not self.ready:
                await asyncio.to_thread(self._clean_partials)
            await asyncio.gather(self._refresh_dem(), *(self._refresh_model(model) for model in self.config.models))
            self._ready.set()

    async def run_forever(self) -> None:
        """Refresh now, then every `refresh_interval` seconds, until cancelled."""
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("could not refresh the archive cache")
            await self.sleep(self.refresh_interval)

    async def _read_changed(self, key: str) -> tuple[bool, bytes | None]:
        """Read an object unless its ETag is the one last recorded; (changed, data), data None if missing."""
        stored = await asyncio.to_thread(self.storage.get_object, key, self._etags.get(key))
        if stored is None:
            return True, None
        if stored.not_modified:
            return False, None
        data = await asyncio.to_thread(lambda: b"".join(stored.body or ()))
        self._etags[key] = stored.etag
        return True, data

    async def _refresh_model(self, model: ArchiveModel) -> None:
        key = f"{PREFIX}/{model.id}/{INDEX}"
        try:
            changed, data = await self._read_changed(key)
            if not changed or data is None:
                return
            index = converter.loads(data, ArchiveIndex)
            if not index.runs:
                return
            newest = index.runs[0].init_time
            name = run_key(newest)
            if model.id in self._current and self._current[model.id].name == name:
                return
            folder = self.directory / model.id
            target = folder / name
            if not target.is_dir():
                async with self._limit:
                    with log_duration(logger, f"downloaded {model.id} {name}"):
                        size = await asyncio.to_thread(
                            _replace_with_download, self.storage, ArchiveStore.run_prefix(model.id, newest), target
                        )
                logger.info("cached %s %s: %.1f MB", model.id, name, size / 1e6)
            try:
                self._runs[model.id] = await asyncio.to_thread(OpenRun.open, model, target)
            except Exception:
                await self._discard(model.id, target)
                raise
            self._swap(model.id, folder, name)
        except Exception:
            self._etags.pop(key, None)  # read the index again next time
            logger.exception("could not cache %s's newest run", model.id)

    async def _refresh_dem(self) -> None:
        key = f"{DEM_PREFIX}/{INDEX}"
        try:
            changed, data = await self._read_changed(key)
            if not changed or data is None:
                if changed and not self.ready:
                    logger.warning("no elevation model published (%s); point elevations come only from requests", key)
                return
            index = converter.loads(data, DemIndex)
            if "dem" in self._current and self._current["dem"].name == index.build:
                return
            folder = self.directory / "dem"
            target = folder / index.build
            if not target.is_dir():
                async with self._limit:
                    with log_duration(logger, f"downloaded the DEM {index.build}"):
                        size = await asyncio.to_thread(
                            _replace_with_download, self.storage, f"{DEM_PREFIX}/{index.build}/", target
                        )
                logger.info("cached the DEM %s: %.0f MB", index.build, size / 1e6)
            try:
                self._dem = await asyncio.to_thread(_open_group, target)
            except Exception:
                await self._discard("dem", target)
                raise
            self._swap("dem", folder, index.build)
        except Exception:
            self._etags.pop(key, None)
            logger.exception("could not cache the elevation model")

    async def _discard(self, name: str, target: Path) -> None:
        """Delete a copy that failed to open (a corrupt shard index, truncated metadata, ...).

        A complete folder on disk is reused rather than downloaded again, so without this a bad copy would keep the
        model (or the DEM) on its previous run until a newer one is published. Only one download is tried per
        refresh; `run_forever` tries again after `refresh_interval`.
        """
        current = self._current.get(name)
        # Requests may still be reading the current copy or the one before it
        if current is not None and target.name in (current.name, current.previous):
            return
        logger.warning("removing %s, which could not be opened, to download it again", target)
        await asyncio.to_thread(shutil.rmtree, target, True)

    def _swap(self, name: str, folder: Path, current: str) -> None:
        """Record what's now in use and delete what's older than the one it replaced."""
        before = self._current.get(name)
        previous = None if before is None or before.name == current else before.name
        self._current[name] = _Current(current, previous)
        _remove_others(folder, {current} | ({previous} if previous else set()))

    async def point(
        self,
        lat: float,
        lon: float,
        elevation: float | None = None,
        interpolate: bool = False,
        models: list[str] | None = None,
        timings: PointTimings | None = None,
    ) -> PointForecast:
        """Every model's forecast at a point (see `upslope.archive.point.build_point_forecast`).

        Args:
            lat, lon: The point.
            elevation: The point's elevation in metres, instead of the DEM's.
            interpolate: Fill the hours between each model's values.
            models: Model ids to include; None for every model.
            timings: Filled in with how long reading and building took, if given.

        Returns:
            The forecast; models without a run or outside their region are listed in `missing`.
        """
        if not self.ready:
            try:
                await asyncio.wait_for(self._ready.wait(), self.ready_timeout)
            except TimeoutError:
                logger.warning("answering a point forecast before the archive cache is ready")
        selected = [m for m in self.config.models if models is None or m.id in models]
        variables = [v.id for v in self.config.variables]
        runs = {m.id: self._runs.get(m.id) for m in selected}
        dem = self._dem

        async def read(run: OpenRun | None) -> RunPoint | None:
            return None if run is None else await asyncio.to_thread(read_run_point, run, variables, lat, lon)

        async def height() -> Elevation | None:
            if elevation is not None:
                return Elevation(elevation, "request")
            return None if dem is None else await asyncio.to_thread(read_elevation, dem, lat, lon)

        started = time.perf_counter()
        point_elevation, read_points = await asyncio.gather(
            height(), asyncio.gather(*(read(run) for run in runs.values()))
        )
        read_done = time.perf_counter()
        points: list[RunPoint] = []
        missing: dict[str, str] = {}
        for model, got in zip(selected, read_points, strict=True):
            if got is not None:
                points.append(got)
            elif runs[model.id] is None:
                missing[model.id] = "no archived run" if self.ready else "loading"
            else:
                missing[model.id] = "outside the model's region"
        forecast = build_point_forecast(self.config, lat, lon, points, point_elevation, interpolate, missing)
        if timings is not None:
            timings.read, timings.build = read_done - started, time.perf_counter() - read_done
        return forecast


def _open_group(path: Path) -> "zarr.Group":
    import zarr
    import zarr.storage

    return zarr.open_group(
        zarr.storage.LocalStore(path, read_only=True), mode="r", zarr_format=3, use_consolidated=True
    )
