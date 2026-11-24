"""Running a job on the clock: the shared schedule of the exporter (`export-loop`) and archiver (`archive-loop`).

Both run checks at fixed slots past midnight UTC (e.g. :00, :15, :30, :45). A check that runs past the next slot
skips to the one after, so checks never overlap, and nothing a check raises stops the loop.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from upslope.logging import get_logger

logger = get_logger(__name__)


def seconds_until_next(now: datetime, interval: timedelta) -> float:
    """Seconds from `now` until the next multiple of `interval` past midnight (e.g. :00, :15, :30, :45)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = now - midnight
    return (interval - elapsed % interval).total_seconds()


async def run_on_schedule(
    check: Callable[[], Awaitable[object]],
    interval: timedelta,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    run_immediately: bool = False,
) -> None:
    """Run `check` at every slot, forever. Only cancellation stops it.

    Args:
        check: One check; it should record its own failures, but anything it raises is logged and the loop goes on.
        interval: Time between slots; should divide a day evenly.
        now: Clock.
        sleep: Sleep function.
        run_immediately: Also run a check right away, before waiting for the first slot.
    """
    skip_wait = run_immediately
    while True:
        if skip_wait:
            skip_wait = False
        else:
            current = now()
            due = current + timedelta(seconds=seconds_until_next(current, interval))
            logger.info("next check at %s", f"{due:%H:%M:%S}Z")
            await sleep((due - current).total_seconds())
        try:
            await check()
        except Exception:
            logger.exception("unexpected error in the loop; continuing at the next check")
