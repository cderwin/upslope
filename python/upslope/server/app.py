"""Litestar app: healthcheck, forecast proxy, admin API, webapp frontend, and the hourly forecast export."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from litestar import Litestar, get
from litestar.datastructures import State
from litestar.exceptions import HTTPException
from litestar.types import Scope

from upslope.annotation_service import AnnotationError, AnnotationService
from upslope.cli import DEFAULT_MODELS_CONFIG, PROJECT_BASE
from upslope.clients.annotations import AnnotationStore
from upslope.clients.storage import StorageClient
from upslope.logging import configure_logging, get_logger
from upslope.models.config import ModelsConfig
from upslope.server.admin import AdminGeoregistrationController
from upslope.server.forecast import ForecastController, LatestRun
from upslope.server.frontend import create_frontend_router
from upslope.server.jobs import build_driver, run_hourly

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
    run_jobs: bool = True,
    export_on_startup: bool = False,
    admin_token: str | None = None,
) -> Litestar:
    """Build the app.

    Args:
        storage: Storage to read exports from and export to. Built from the environment at startup if omitted.
        webapp_dist: Built webapp to serve at `/`. Defaults to `$UPSLOPE_WEBAPP_DIST` or `<repo>/webapp/dist`.
        run_jobs: Run the hourly forecast export. Disable for tests.
        export_on_startup: Also run the export once as soon as the app starts (in the background,
            so the server is up meanwhile). Ignored if `run_jobs` is False.
        admin_token: Bearer token for the admin API. Defaults to `$ADMIN_TOKEN`; empty disables the API.
    """
    configure_logging()

    if admin_token is None:
        admin_token = os.environ.get("ADMIN_TOKEN", "")
    if not admin_token:
        logger.warning("ADMIN_TOKEN is not set; the admin API (/admin) is disabled")

    if webapp_dist is None:
        webapp_dist = Path(os.environ.get("UPSLOPE_WEBAPP_DIST", DEFAULT_WEBAPP_DIST))

    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncIterator[None]:
        # build up front so missing config or credentials fail at startup, not on first use
        config = ModelsConfig.load(DEFAULT_MODELS_CONFIG)
        app.state.storage = storage or StorageClient.build()
        app.state.annotations = AnnotationService(AnnotationStore(app.state.storage), config)
        # the webapp reads one manifest, so serve the first model's runs
        latest_run = app.state.latest_run = LatestRun(app.state.storage, config.models[0].id)
        try:
            await latest_run.refresh()
        except Exception:
            logger.exception("could not look up the latest run at startup; will retry on request")

        task = None
        if run_jobs:
            driver = build_driver(config, app.state.storage)
            task = asyncio.create_task(
                run_hourly(driver, on_export=latest_run.refresh, run_immediately=export_on_startup)
            )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    route_handlers = [health, ForecastController, AdminGeoregistrationController]
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
    )
