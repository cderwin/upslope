"""Scheduled background work: the hourly forecast export."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from upslope.clients.annotations import AnnotationStore
from upslope.clients.storage import StorageClient
from upslope.logging import get_logger
from upslope.models.config import ModelsConfig
from upslope.models.driver import ExportFailed, ForecastDriver
from upslope.models.image_client import ImageClient

logger = get_logger(__name__)


def build_driver(config: ModelsConfig, storage: StorageClient) -> ForecastDriver:
    """Create the export driver, reading annotations from and exporting to `storage`."""
    return ForecastDriver(config=config, client=ImageClient(), storage=storage, annotations=AnnotationStore(storage))


def seconds_until_next_hour(now: datetime) -> float:
    """Seconds from `now` until the top of the next hour."""
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return (next_hour - now).total_seconds()


async def run_hourly(
    driver: ForecastDriver,
    on_export: Callable[[], Awaitable[None]] | None = None,
    run_immediately: bool = False,
) -> None:
    """Run the driver at the top of every hour, forever.

    A failed run is logged and the schedule continues. A run that lasts past the hour
    makes the job skip ahead to the next top of the hour, so runs never overlap.

    Args:
        driver: Driver to run.
        on_export: Called after a run that exported at least one new manifest.
        run_immediately: Also run once right away, before waiting for the first top of the hour.
    """
    skip_wait = run_immediately
    while True:
        if skip_wait:
            skip_wait = False
            logger.info("starting startup forecast export")
        else:
            await asyncio.sleep(seconds_until_next_hour(datetime.now()))
            logger.info("starting scheduled forecast export")
        try:
            # the driver is blocking (HTTP, rasterio, S3), so keep it off the event loop
            manifests = await asyncio.to_thread(driver.run)
        except ExportFailed as e:
            # each model's failure was logged by the driver; the others may still have exported
            logger.error("scheduled forecast export failed for %s", ", ".join(e.models))
            manifests = e.manifests
        except Exception:
            logger.exception("scheduled forecast export failed")
            continue
        if not manifests:
            continue
        logger.info("forecast export finished: %d model(s) exported", len(manifests))
        if on_export is not None:
            try:
                await on_export()
            except Exception:
                logger.exception("after-export hook failed")
