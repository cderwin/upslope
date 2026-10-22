"""Proxy for the latest exported forecast: `/fx/web/...` -> `<latest export prefix>/...` in storage."""

import asyncio
import time
from email.utils import format_datetime

from litestar import Controller, Request, Response, get
from litestar.datastructures import State
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.response import Stream

from upslope.clients.storage import ModelRunMarker, StorageClient
from upslope.logging import get_logger

logger = get_logger(__name__)

# The manifest changes with every run; frames keep their URL across runs, so only cache them briefly.
MANIFEST_CACHE_CONTROL = "no-cache"
FRAME_CACHE_CONTROL = "public, max-age=300"


class LatestRun:
    """Cached pointer to a model's most recent exported run.

    The pointer refreshes when it is older than `max_age` seconds, or on demand via `refresh`
    (e.g. right after this process exports a run).
    """

    def __init__(self, storage: StorageClient, model: str, max_age: float = 300.0) -> None:
        self._storage = storage
        self._model = model
        self._max_age = max_age
        self._marker: ModelRunMarker | None = None
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    def _stale(self) -> bool:
        return self._fetched_at is None or time.monotonic() - self._fetched_at > self._max_age

    async def _fetch(self) -> ModelRunMarker | None:
        # callers hold self._lock
        marker = await asyncio.to_thread(self._storage.latest_model_run, self._model)
        if marker != self._marker:
            logger.info("latest %s run is now %s", self._model, marker.export_prefix if marker else None)
        self._marker, self._fetched_at = marker, time.monotonic()
        return marker

    async def refresh(self) -> ModelRunMarker | None:
        """Look up the latest run in storage now."""
        async with self._lock:
            return await self._fetch()

    async def get(self) -> ModelRunMarker | None:
        """The latest run, refreshing first if the cached pointer is stale.

        If a refresh fails but a run is already cached, the cached run is returned and the error logged.
        """
        if not self._stale():
            return self._marker
        try:
            async with self._lock:
                # requests that queued behind another's refresh reuse its result
                return await self._fetch() if self._stale() else self._marker
        except Exception:
            if self._marker is None:
                raise
            logger.exception("could not refresh latest %s run; serving cached run", self._model)
            return self._marker


def _validate_parts(*parts: str) -> None:
    """Reject path segments that could step outside the export prefix."""
    for part in parts:
        for segment in part.split("/"):
            if segment in ("", ".", "..") or "\\" in segment:
                raise NotFoundException()


async def _proxy(request: Request, latest_run: LatestRun, storage: StorageClient, relative_key: str, cache_control: str) -> Response:
    marker = await latest_run.get()
    if marker is None:
        raise NotFoundException("no forecast has been exported yet")

    key = f"{marker.export_prefix}/{relative_key}"
    obj = await asyncio.to_thread(storage.get_object, key, request.headers.get("if-none-match"))
    if obj is None:
        raise NotFoundException()

    headers = {"Cache-Control": cache_control}
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


class ForecastController(Controller):
    """Serves the latest exported run's manifest and frames.

    Expects `state.storage` (StorageClient) and `state.latest_run` (LatestRun).
    """

    path = "/fx/web"

    @get("/manifest.json")
    async def manifest(self, request: Request, state: State) -> Response:
        return await _proxy(request, state.latest_run, state.storage, "manifest.json", MANIFEST_CACHE_CONTROL)

    @get("/{model:str}/{variable:str}/{path:path}")
    async def frame(
        self, request: Request, state: State, model: FromPath[str], variable: FromPath[str], path: FromPath[str]
    ) -> Response:
        path = path.lstrip("/")
        _validate_parts(model, variable, path)
        return await _proxy(request, state.latest_run, state.storage, f"{model}/{variable}/{path}", FRAME_CACHE_CONTROL)
