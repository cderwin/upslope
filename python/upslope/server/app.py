"""Litestar app: healthcheck, forecast API, admin API and webapp frontend.

Exporting runs in a separate process (`upslope export-loop`, see `upslope.export_loop`), so it never competes
with requests; the server only reads what it writes.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from litestar import Litestar, get
from litestar.config.compression import CompressionConfig
from litestar.datastructures import State
from litestar.exceptions import HTTPException
from litestar.types import Scope

from upslope.annotation_service import AnnotationError, AnnotationService
from upslope.archive.cache import ArchiveCache
from upslope.archive.config import ArchiveConfig
from upslope.cli import DEFAULT_ARCHIVE_CONFIG, DEFAULT_MODELS_CONFIG, PROJECT_BASE
from upslope.clients.annotations import AnnotationStore
from upslope.clients.forecasts import ForecastStore
from upslope.clients.storage import StorageClient
from upslope.logging import configure_logging, get_logger
from upslope.models.config import ModelsConfig
from upslope.server.admin import AdminGeoregistrationController
from upslope.server.forecast import ForecastController, StatusCache
from upslope.server.frontend import create_frontend_router
from upslope.server.point import PointController

logger = get_logger(__name__)

DEFAULT_WEBAPP_DIST = PROJECT_BASE / "webapp/dist"


async def log_server_error(exc: Exception, scope: Scope) -> None:
    """Log unhandled errors with a traceback (logging_config=None turns off Litestar's own)."""
    if isinstance(exc, HTTPException) and exc.status_code < 500:
        return
    if isinstance(exc, AnnotationError):  # reported to the client as a 4xx/502 by the admin API
        return
    logger.error("error handling %s %s", scope.get("method", ""), scope.get("path", ""), exc_info=exc)


@get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app(
    storage: StorageClient | None = None,
    webapp_dist: Path | None = None,
    admin_token: str | None = None,
    config: ModelsConfig | None = None,
    public_storage: StorageClient | None = None,
    data_url: str | None = None,
    archive_cache_dir: Path | None = None,
    archive_config: ArchiveConfig | None = None,
) -> Litestar:
    """Build the app.

    Args:
        storage: The private bucket, for annotations. Built from the environment at startup if omitted.
        webapp_dist: Built webapp to serve at `/`. Defaults to `$UPSLOPE_WEBAPP_DIST` or `<repo>/webapp/dist`.
        admin_token: Bearer token for the admin API. Defaults to `$ADMIN_TOKEN`; empty disables the API.
        config: Models config. Loaded from `python/config/models.yaml` at startup if omitted.
        public_storage: The public bucket the exporter writes forecasts to. Defaults to `storage` if that's
            given, else built from the environment (`PUBLIC_BUCKET_NAME`) at startup.
        data_url: Where browsers load forecast files from (`forecasts/` in the public bucket), reported at
            `/fx/status`. Defaults to `$PUBLIC_DATA_URL`; raises RuntimeError without one.
        archive_cache_dir: Local folder for the archive's newest runs and the DEM, which point forecasts
            (`/fx/point`) are answered from. Defaults to `$ARCHIVE_CACHE_DIR`; without one, `/fx/point` is disabled.
        archive_config: Archive config. Loaded from `python/config/archive.yaml` at startup if omitted.
    """
    configure_logging()

    if data_url is None:
        data_url = os.environ.get("PUBLIC_DATA_URL", "")
    if not data_url:
        raise RuntimeError("PUBLIC_DATA_URL is not set: browsers need it to load forecasts from the public bucket")

    if admin_token is None:
        admin_token = os.environ.get("ADMIN_TOKEN", "")
    if not admin_token:
        logger.warning("ADMIN_TOKEN is not set; the admin API (/admin) is disabled")

    if webapp_dist is None:
        webapp_dist = Path(os.environ.get("UPSLOPE_WEBAPP_DIST", DEFAULT_WEBAPP_DIST))

    if archive_cache_dir is None and os.environ.get("ARCHIVE_CACHE_DIR"):
        archive_cache_dir = Path(os.environ["ARCHIVE_CACHE_DIR"])
    if archive_cache_dir is None:
        logger.warning("ARCHIVE_CACHE_DIR is not set; point forecasts (/fx/point) are disabled")

    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncIterator[None]:
        # build up front so missing config or credentials fail at startup, not on first use
        models = config or ModelsConfig.load(DEFAULT_MODELS_CONFIG)
        app.state.storage = storage or StorageClient.build()
        app.state.annotations = AnnotationService(AnnotationStore(app.state.storage), models)
        forecasts = ForecastStore(public_storage or storage or StorageClient.build_public())
        status = app.state.forecast_status = StatusCache(forecasts, models, data_url)
        try:
            await status.refresh()
        except Exception:
            logger.exception("could not read forecast status at startup; will retry on request")
        refresher = asyncio.create_task(status.run_forever(), name="forecast-status-refresh")
        tasks = [refresher]
        app.state.archive_cache = None
        if archive_cache_dir is not None:
            # downloads in the background: the first requests wait for it briefly (ArchiveCache.ready_timeout)
            archive = app.state.archive_cache = ArchiveCache(
                app.state.storage, archive_config or ArchiveConfig.load(DEFAULT_ARCHIVE_CONFIG), archive_cache_dir
            )
            tasks.append(asyncio.create_task(archive.run_forever(), name="archive-cache-refresh"))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError):
                    await task

    route_handlers = [health, ForecastController, PointController, AdminGeoregistrationController]
    if (webapp_dist / "index.html").is_file():
        route_handlers.append(create_frontend_router(webapp_dist, reserved_prefixes=("fx", "health", "admin")))
    else:
        logger.warning("no built webapp at %s; not serving the frontend (run `npm run build` in webapp/)", webapp_dist)

    # logging_config=None: keep the format from upslope.logging instead of Litestar's default
    # openapi_config=None: no /schema docs pages, which would also shadow webapp routes
    return Litestar(
        route_handlers=route_handlers,
        lifespan=[lifespan],
        state=State({"admin_token": admin_token}),
        after_exception=[log_server_error],
        logging_config=None,
        openapi_config=None,
        # point forecasts are a few hundred KB of JSON, mostly repeated digits
        compression_config=CompressionConfig(backend="gzip", minimum_size=1024),
    )
