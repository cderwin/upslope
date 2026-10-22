"""Scheduled background work: the hourly WRF export."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from upslope.cli import DEFAULT_GEOREGISTRATION_CONFIG, DEFAULT_WRF_CONFIG
from upslope.clients.storage import StorageClient
from upslope.logging import get_logger
from upslope.models.uw_wrf.driver import WrfDriver
from upslope.models.uw_wrf.georegistration import Registration, RegistrationConfig
from upslope.models.uw_wrf.wrf_client import WrfClient, WrfConfig

logger = get_logger(__name__)


def build_driver(storage: StorageClient) -> WrfDriver:
    """Create a WrfDriver from the default configs, exporting to `storage`."""
    wrf_config = WrfConfig.load(DEFAULT_WRF_CONFIG)
    return WrfDriver(
        config=wrf_config,
        client=WrfClient(wrf_config),
        registration=Registration.build(RegistrationConfig.load(DEFAULT_GEOREGISTRATION_CONFIG)),
        storage=storage,
    )


def seconds_until_next_hour(now: datetime) -> float:
    """Seconds from `now` until the top of the next hour."""
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return (next_hour - now).total_seconds()


async def run_hourly(
    driver: WrfDriver,
    on_export: Callable[[], Awaitable[None]] | None = None,
    run_immediately: bool = False,
) -> None:
    """Run the driver at the top of every hour, forever.

    A failed run is logged and the schedule continues. A run that lasts past the hour
    makes the job skip ahead to the next top of the hour, so runs never overlap.

    Args:
        driver: Driver to run.
        on_export: Called after a run that exported a new manifest.
        run_immediately: Also run once right away, before waiting for the first top of the hour.
    """
    skip_wait = run_immediately
    while True:
        if skip_wait:
            skip_wait = False
            logger.info("starting startup WRF export")
        else:
            await asyncio.sleep(seconds_until_next_hour(datetime.now()))
            logger.info("starting scheduled WRF export")
        try:
            # the driver is blocking (HTTP, rasterio, S3), so keep it off the event loop
            manifest = await asyncio.to_thread(driver.run)
            if manifest is None:
                continue
            logger.info("WRF export finished with %d layers", len(manifest.layers))
            if on_export is not None:
                await on_export()
        except Exception:
            logger.exception("scheduled WRF export failed")
