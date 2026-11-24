"""The archiver: archives new runs of every model on a schedule, in its own process (`upslope archive-loop`).

It runs apart from the web server and the exporter (its own Fly process group and machine), so decoding model
data never slows either down, and it's the only writer under `archive/` in the private bucket. Each check's
outcome goes to `archive/status.json`. Nothing that goes wrong in a check stops the loop: a model's failure is
confined to that model and recorded in the status.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from attrs import define, evolve, field

from upslope.archive.config import ArchiveConfig
from upslope.archive.driver import ArchiveDriver
from upslope.archive.sources.base import ArchiveSource
from upslope.archive.store import ArchiveStatus, ArchiveStore
from upslope.clients.storage import StorageClient
from upslope.logging import get_logger, log_context
from upslope.schedule import run_on_schedule, seconds_until_next

logger = get_logger(__name__)

# How often to look for new runs. Upstreams publish a run over an hour or more, and only complete runs are archived,
# so this bounds how late a run is archived after it completes.
DEFAULT_INTERVAL = timedelta(minutes=15)


def build_sources() -> dict[str, ArchiveSource]:
    """Every kind of source, by `ArchiveSourceConfig.kind`."""
    from upslope.archive.sources.dynamical import DynamicalRunSource
    from upslope.archive.sources.openmeteo import OpenMeteoRunSource

    return {"dynamical": DynamicalRunSource(), "openmeteo": OpenMeteoRunSource()}


def build_driver(config: ArchiveConfig, storage: StorageClient) -> ArchiveDriver:
    """Create the archive driver, writing to the private bucket `storage`."""
    return ArchiveDriver(config=config, sources=build_sources(), store=ArchiveStore(storage))


@define
class ArchiveLoop:
    """Runs archive checks on the clock and records each one's outcome in `archive/status.json`.

    Attributes:
        driver: The archive pipeline.
        interval: Time between checks; should divide a day evenly.
        now: Clock, for tests.
        sleep: Sleep function, for tests.
    """

    driver: ArchiveDriver
    interval: timedelta = DEFAULT_INTERVAL
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    status: ArchiveStatus | None = field(default=None, init=False)

    @property
    def store(self) -> ArchiveStore:
        return self.driver.store

    async def run_forever(self, run_immediately: bool = False) -> None:
        """Run a check at every slot, forever. Only cancellation stops it.

        Args:
            run_immediately: Also run a check right away, before waiting for the first slot.
        """
        self.status = await self._call(self.store.read_status, "could not read the previous archive status")
        logger.info("archiver started: checking every %s", self.interval)
        await run_on_schedule(self.run_check, self.interval, self.now, self.sleep, run_immediately)

    async def run_check(self, models: list[str] | None = None, force: bool = False) -> ArchiveStatus:
        """Run one check and record its outcome. Never raises, except when cancelled.

        Args:
            models: Model ids to check; None for every model.
            force: Archive the newest runs again.

        Returns:
            The status written at the end of the check.
        """
        previous = self.status
        check = (previous.check if previous else 0) + 1
        with log_context(f"archive {check}"):
            start = time.monotonic()
            running = ArchiveStatus(
                state="running", check=check, started_at=self.now(),
                last_success_at=previous.last_success_at if previous else None, models=previous.models if previous else [],
            )
            self.status = running
            await self._call(lambda: self.store.write_status(running), "could not write the archive status")
            try:
                models_status = await self.driver.run(models, force=force)
            except Exception as e:
                # the driver confines failures to models; this is only a backstop
                logger.exception("check failed")
                models_status = previous.models if previous else []
                failed = [f"check failed: {type(e).__name__}: {e}"]
            else:
                failed = [status.model for status in models_status if status.state == "failed"]
            finished = self.now()
            done = evolve(
                running,
                state="failed" if failed else "ok",
                finished_at=finished,
                next_check_at=finished + timedelta(seconds=seconds_until_next(finished, self.interval)),
                last_success_at=running.last_success_at if failed else finished,
                models=models_status,
            )
            self.status = done
            archived = sum(len(status.runs) for status in models_status)
            logger.info("check finished in %.1fs: %d runs archived%s", time.monotonic() - start, archived,
                        f"; failed: {', '.join(failed)}" if failed else "")
            await self._call(lambda: self.store.write_status(done), "could not write the archive status")
            return done

    @staticmethod
    async def _call(function: Callable, message: str):
        # the status is for watching the archiver; failing to read or write it must not stop archiving
        try:
            return await asyncio.to_thread(function)
        except Exception:
            logger.exception("%s", message)
            return None
