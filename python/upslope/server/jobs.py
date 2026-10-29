"""Scheduled background work: the periodic forecast export."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from upslope.clients.annotations import AnnotationStore
from upslope.clients.forecasts import ForecastStore
from upslope.clients.storage import StorageClient
from upslope.logging import get_logger
from upslope.models.config import ModelsConfig
from upslope.models.driver import ExportFailed, ForecastDriver
from upslope.models.image_client import ImageClient

logger = get_logger(__name__)


def build_driver(config: ModelsConfig, storage: StorageClient) -> ForecastDriver:
    """Create the export driver, reading annotations from and exporting to `storage`."""
    return ForecastDriver(config=config, client=ImageClient(), store=ForecastStore(storage), annotations=AnnotationStore(storage))


# How often to check for new frames by default. A run takes hours to publish, so this sets how far
# behind the in-progress run shown on the map can be; a check with nothing new costs one small request per layer.
DEFAULT_INTERVAL = timedelta(minutes=15)


def seconds_until_next(now: datetime, interval: timedelta) -> float:
    """Seconds from `now` until the next multiple of `interval` past midnight (e.g. :00, :15, :30, :45)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = now - midnight
    return (interval - elapsed % interval).total_seconds()


async def run_periodically(
    driver: ForecastDriver,
    interval: timedelta = DEFAULT_INTERVAL,
    on_export: Callable[[], Awaitable[None]] | None = None,
    run_immediately: bool = False,
) -> None:
    """Run the driver every `interval`, on the clock, forever.

    A failed run is logged and the schedule continues. A run that lasts past the next slot makes the
    job skip ahead to the one after, so runs never overlap.

    Args:
        driver: Driver to run.
        interval: Time between runs; should divide a day evenly.
        on_export: Called after each run, so cached status picks up what it wrote.
        run_immediately: Also run once right away, before waiting for the first slot.
    """
    skip_wait = run_immediately
    while True:
        if skip_wait:
            skip_wait = False
            logger.info("starting startup forecast export")
        else:
            await asyncio.sleep(seconds_until_next(datetime.now(), interval))
            logger.debug("starting scheduled forecast export")
        try:
            # the driver is blocking (HTTP, rasterio, S3), so keep it off the event loop
            manifests = await asyncio.to_thread(driver.run)
            if manifests:
                logger.info("forecast export wrote %d run manifest(s)", len(manifests))
        except ExportFailed as e:
            # each model's failure was logged by the driver; the others may still have exported
            logger.error("scheduled forecast export failed for %s", ", ".join(e.models))
        except Exception:
            logger.exception("scheduled forecast export failed")
        if on_export is not None:
            try:
                await on_export()
            except Exception:
                logger.exception("after-export hook failed")
