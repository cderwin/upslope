"""Forecast API for the webapp: `GET /fx/status`, each model's latest run, in-progress run (with each layer's
progress) and retained runs, so the webapp can find manifests and show how a run is coming along; the exporter's
health; and `data_url`, where browsers read the forecast files from the public bucket.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from attrs import define
from cattrs.preconf.json import make_converter
from litestar import Controller, Response, get
from litestar.datastructures import State

from upslope.clients.forecasts import EXPORT_STATUS, INDEX, ForecastStore
from upslope.logging import get_logger
from upslope.models.config import ModelConfig, ModelsConfig
from upslope.models.manifest import ExportStatus, ForecastIndex, InProgress, ModelIndex, run_path

logger = get_logger(__name__)

converter = make_converter()

STATUS_CACHE_CONTROL = "no-cache"

# A layer that hasn't gained a frame for this long, while incomplete, is reported as stalled
STALL_AFTER = timedelta(hours=2)

LayerState = Literal["complete", "waiting", "stalled", "error"]


@define
class RunRef:
    """A run of a model, and where its files are.

    Attributes:
        initialized: Initialization time (UTC).
        path: Folder under the model's data prefix: `<data_url>/<model>/<path>/manifest.json`.
    """

    initialized: datetime
    path: str


@define
class LayerStatus:
    """How far one layer of the in-progress run has got.

    Attributes:
        id: Layer id.
        name: Display name.
        state: `complete`; `waiting` for the next hour to be published; `stalled` (no new hour for
            STALL_AFTER); or `error` (the last attempt to get the next hour failed, and will be retried).
        available_hour: Newest hour exported, or None before the first.
        last_hour: The layer's last forecast hour.
        progressed_at: When `available_hour` last advanced, if known.
        error: The last error, if `state` is `error`.
    """

    id: str
    name: str
    state: LayerState
    available_hour: int | None
    last_hour: int
    progressed_at: datetime | None
    error: str | None


@define
class InProgressStatus:
    """The newest run that has started publishing.

    Attributes:
        initialized: Initialization time (UTC).
        path: Folder under the model's data prefix.
        checked_at: When the exporter last checked it for new hours.
        complete: Whether every layer is complete (it is then also the latest run).
        layers: Each configured layer's progress.
    """

    initialized: datetime
    path: str
    checked_at: datetime
    complete: bool
    layers: list[LayerStatus]


@define
class ModelStatus:
    """What the webapp needs to know about one model's exported runs.

    Attributes:
        id: Model id.
        name: Display name.
        latest: The newest complete run, if any has been exported.
        in_progress: The newest run that has started, if any, and how far it has got.
        runs: Every retained run with a manifest, newest first (complete or not).
    """

    id: str
    name: str
    latest: RunRef | None
    in_progress: InProgressStatus | None
    runs: list[RunRef]


def in_progress_status(model: ModelConfig, progress: InProgress, now: datetime) -> InProgressStatus:
    """Summarize `in-progress.json` for display: name each configured layer and say what state it's in."""
    started = min((layer.progressed_at for layer in progress.layers if layer.progressed_at), default=None)
    layers = []
    for config in model.layers:
        layer = progress.layer(config.id)
        if layer is None:  # added to the config since the last check
            layers.append(LayerStatus(config.id, config.name, "waiting", None, config.hours.last, None, None))
            continue
        # a layer with no frames yet is measured from when the run's first frame arrived
        since = layer.progressed_at or started
        state: LayerState
        if layer.complete:
            state = "complete"
        elif layer.error is not None:
            state = "error"
        elif since is not None and now - since > STALL_AFTER:
            state = "stalled"
        else:
            state = "waiting"
        layers.append(
            LayerStatus(config.id, config.name, state, layer.available_hour, layer.last_hour, layer.progressed_at, layer.error)
        )
    return InProgressStatus(
        progress.initialized, progress.path, progress.checked_at, all(l.state == "complete" for l in layers), layers
    )


@define
class ForecastStatus:
    """Every model's status, as served at `/fx/status`.

    Attributes:
        models: Status of each configured model, in config order.
        data_url: Base URL of the forecast files (`forecasts/` in the public bucket): a run's manifest is at
            `<data_url>/<model>/<run path>/manifest.json`, and its frames' paths are relative to the run's folder.
        export: The exporter's last written status (`export-status.json`): whether its last check failed, each
            model's outcome and errors; None if it has never run.
        export_overdue: The exporter should have written its status again by now: it's stuck or down.
    """

    models: list[ModelStatus]
    data_url: str
    export: ExportStatus | None = None
    export_overdue: bool = False


class StatusCache:
    """The forecast status, kept in memory and refreshed in the background, so a request never waits on storage.

    It's built from `forecasts/index.json`, which the exporter (another process) rewrites as each model's
    check finishes, and `export-status.json`. Both are re-read every `refresh_interval` seconds with their
    ETags, so an unchanged file costs one small request. Until the exporter has written the index (e.g. right
    after it's first deployed), every model's files are read instead, which is slow but still in the background.
    Only the first request, if the startup refresh failed, waits for storage.

    The parts that depend on the time (stalled layers, an overdue exporter) are worked out per request.
    """

    def __init__(
        self,
        store: ForecastStore,
        config: ModelsConfig,
        data_url: str,
        refresh_interval: float = 60.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._config = config
        self._data_url = data_url
        self._refresh_interval = refresh_interval
        self._now = now
        self._sleep = sleep
        self._index: ForecastIndex | None = None
        self._export: ExportStatus | None = None
        self._etags: dict[str, str | None] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    def _read_changed(self, key: str) -> tuple[bool, bytes | None, str | None]:
        """Read an object unless it's unchanged since the last read.

        Returns:
            Whether it changed, its data (None if missing or unchanged) and its ETag, to record once parsed.
        """
        obj = self._store.storage.get_object(key, self._etags.get(key))
        if obj is None:
            return True, None, None
        if obj.not_modified:
            return False, None, obj.etag
        return True, b"".join(obj.body or ()), obj.etag

    async def _read_index(self) -> ForecastIndex | None:
        changed, data, etag = await asyncio.to_thread(self._read_changed, INDEX)
        if not changed:
            return self._index
        if data is not None:
            index = ForecastIndex.from_json(data)
            self._etags[INDEX] = etag
            return index
        logger.warning("the exporter hasn't written %s yet; reading every model's runs instead", INDEX)
        entries = await asyncio.gather(
            *(asyncio.to_thread(self._store.read_model_index, model.id) for model in self._config.models)
        )
        self._etags.pop(INDEX, None)
        return ForecastIndex(list(entries), self._now())

    async def _read_export(self) -> ExportStatus | None:
        changed, data, etag = await asyncio.to_thread(self._read_changed, EXPORT_STATUS)
        if not changed:
            return self._export
        export = None if data is None else ExportStatus.from_json(data)
        self._etags[EXPORT_STATUS] = etag
        return export

    async def _refresh(self) -> None:
        # callers hold self._lock
        index, export = await asyncio.gather(self._read_index(), self._read_export())
        self._index, self._export, self._loaded = index, export, True
        if export is not None and self._now() > export.overdue_after:
            logger.warning("the exporter is overdue: its last status (check %d, %s) was due again by %s", export.check, export.state, export.overdue_after)

    async def refresh(self) -> None:
        """Read the index and the exporter's status from storage now. Raises if storage can't be read."""
        async with self._lock:
            await self._refresh()

    async def run_forever(self) -> None:
        """Refresh every `refresh_interval` seconds until cancelled; a failed refresh is logged and retried."""
        while True:
            await self._sleep(self._refresh_interval)
            try:
                await self.refresh()
            except Exception:
                logger.exception("could not refresh the forecast status; serving the last one read")

    async def get(self) -> ForecastStatus:
        """The status as of the last refresh. Reads storage first only if nothing has been read yet."""
        if not self._loaded:
            async with self._lock:
                # requests that queued behind another's first refresh reuse its result
                if not self._loaded:
                    await self._refresh()
        return self._status()

    def _status(self) -> ForecastStatus:
        now = self._now()
        index = self._index or ForecastIndex()
        models = []
        for model in self._config.models:
            entry = index.model(model.id) or ModelIndex(model.id)
            latest, progress = entry.latest, entry.in_progress
            models.append(
                ModelStatus(
                    model.id,
                    model.name,
                    None if latest is None else RunRef(latest.initialized, latest.path),
                    None if progress is None else in_progress_status(model, progress, now),
                    [RunRef(initialized, run_path(initialized)) for initialized in entry.runs],
                )
            )
        export = self._export
        return ForecastStatus(models, self._data_url, export, export is not None and now > export.overdue_after)


class ForecastController(Controller):
    """Serves the forecast status. Expects `state.forecast_status` (StatusCache)."""

    path = "/fx"

    @get("/status")
    async def status(self, state: State) -> Response:
        status = await state.forecast_status.get()
        return Response(converter.unstructure(status), headers={"Cache-Control": STATUS_CACHE_CONTROL})
