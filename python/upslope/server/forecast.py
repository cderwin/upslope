"""Forecast API for the webapp.

- `GET /fx/status`: each model's latest run and its retained runs, so the webapp can find manifests.
- `GET /fx/data/{path}`: objects under `forecasts/` in storage (manifests, frames, pointers), passed through.
"""

import asyncio
import time
from datetime import datetime
from email.utils import format_datetime

from attrs import define
from cattrs.preconf.json import make_converter
from litestar import Controller, Request, Response, get
from litestar.datastructures import State
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.response import Stream

from upslope.clients.forecasts import PREFIX, ForecastStore
from upslope.logging import get_logger
from upslope.models.config import ModelsConfig
from upslope.models.manifest import run_path

logger = get_logger(__name__)

converter = make_converter()

# Pointers and manifests change in place, so always revalidate them (cheap with ETags). Frames keep their
# URL unless a run is re-exported (after an annotation publish), so cache them briefly.
JSON_CACHE_CONTROL = "no-cache"
FRAME_CACHE_CONTROL = "public, max-age=300"
STATUS_CACHE_CONTROL = "no-cache"


@define
class RunRef:
    """A run of a model, and where its files are.

    Attributes:
        initialized: Initialization time (UTC).
        path: Folder under the model's data prefix: `/fx/data/<model>/<path>/manifest.json`.
    """

    initialized: datetime
    path: str


@define
class ModelStatus:
    """What the webapp needs to know about one model's exported runs.

    Attributes:
        id: Model id.
        name: Display name.
        latest: The newest complete run, if any has been exported.
        runs: Every retained run with a manifest, newest first (complete or not).
    """

    id: str
    name: str
    latest: RunRef | None
    runs: list[RunRef]


@define
class ForecastStatus:
    """Every model's status, as served at `/fx/status`.

    Attributes:
        models: Status of each configured model, in config order.
    """

    models: list[ModelStatus]


class StatusCache:
    """Cached `ForecastStatus`. Listing runs takes a few S3 requests, so it's refreshed when older than
    `max_age` seconds, or on demand via `refresh` (e.g. right after this process exports a run).
    """

    def __init__(self, store: ForecastStore, config: ModelsConfig, max_age: float = 60.0) -> None:
        self._store = store
        self._config = config
        self._max_age = max_age
        self._status: ForecastStatus | None = None
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    def _stale(self) -> bool:
        return self._fetched_at is None or time.monotonic() - self._fetched_at > self._max_age

    def _read(self) -> ForecastStatus:
        models = []
        for model in self._config.models:
            latest = self._store.read_latest(model.id)
            runs = [RunRef(initialized, run_path(initialized)) for initialized in self._store.list_runs(model.id)]
            models.append(
                ModelStatus(model.id, model.name, None if latest is None else RunRef(latest.initialized, latest.path), runs)
            )
        return ForecastStatus(models)

    async def _fetch(self) -> ForecastStatus:
        # callers hold self._lock
        status = await asyncio.to_thread(self._read)
        self._status, self._fetched_at = status, time.monotonic()
        return status

    async def refresh(self) -> ForecastStatus:
        """Read the status from storage now."""
        async with self._lock:
            return await self._fetch()

    async def get(self) -> ForecastStatus:
        """The status, refreshing first if the cached copy is stale.

        If a refresh fails but a status is already cached, the cached one is returned and the error logged.
        """
        if not self._stale() and self._status is not None:
            return self._status
        try:
            async with self._lock:
                # requests that queued behind another's refresh reuse its result
                return await self._fetch() if self._stale() or self._status is None else self._status
        except Exception:
            if self._status is None:
                raise
            logger.exception("could not refresh forecast status; serving cached status")
            return self._status


def _validate_path(path: str) -> None:
    """Reject paths that could step outside `forecasts/`."""
    for segment in path.split("/"):
        if segment in ("", ".", "..") or "\\" in segment:
            raise NotFoundException()


class ForecastController(Controller):
    """Serves forecast status and exported runs.

    Expects `state.storage` (StorageClient) and `state.forecast_status` (StatusCache).
    """

    path = "/fx"

    @get("/status")
    async def status(self, state: State) -> Response:
        status = await state.forecast_status.get()
        return Response(converter.unstructure(status), headers={"Cache-Control": STATUS_CACHE_CONTROL})

    @get("/data/{path:path}")
    async def data(self, request: Request, state: State, path: FromPath[str]) -> Response:
        path = path.lstrip("/")
        _validate_path(path)
        obj = await asyncio.to_thread(state.storage.get_object, f"{PREFIX}/{path}", request.headers.get("if-none-match"))
        if obj is None:
            raise NotFoundException()

        headers = {"Cache-Control": JSON_CACHE_CONTROL if path.endswith(".json") else FRAME_CACHE_CONTROL}
        if obj.etag:
            headers["ETag"] = obj.etag
        if obj.not_modified:
            return Response(content=None, status_code=304, headers=headers)
        if obj.last_modified:
            headers["Last-Modified"] = format_datetime(obj.last_modified, usegmt=True)
        if obj.content_length is not None:
            headers["Content-Length"] = str(obj.content_length)
        # boto's body is a blocking iterator; Litestar pulls each chunk in a worker thread
        return Stream(obj.body, media_type=obj.content_type or "application/octet-stream", headers=headers)
