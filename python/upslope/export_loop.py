"""The exporter: checks every model for new frames on a schedule, in its own process (`upslope export-loop`).

It runs apart from the web server (its own Fly process group and machine), so exporting never slows down
requests. The two only share the bucket: the exporter writes `forecasts/`, including `export-status.json`,
which the server reads for `/fx/status`.

Nothing that goes wrong in a check stops the loop. A model's failure is confined to that model, a check that
runs too long is stopped (what it exported is kept, and the next check resumes), and any other error is
logged and recorded in the status. Every log line of a check is tagged `[check N]`, and a model's lines also
`[<model id>]`, so a check can be followed through the interleaved logs of models exported concurrently.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from attrs import define, evolve, field

from upslope.clients.annotations import AnnotationStore
from upslope.clients.forecasts import ForecastStore
from upslope.clients.storage import StorageClient
from upslope.logging import get_logger, log_context
from upslope.models.config import ModelsConfig
from upslope.models.driver import CheckReport, ForecastDriver, describe
from upslope.models.dynamical.source import DynamicalSource
from upslope.models.manifest import ExportStatus, ModelCheckStatus
from upslope.models.source import ForecastSource
from upslope.models.uw_wrf.image_client import ImageClient
from upslope.models.uw_wrf.source import UwWrfSource

logger = get_logger(__name__)

# How often to check for new frames by default. A run takes hours to publish, so this sets how far
# behind the in-progress run shown on the map can be; a check with nothing new costs a few small requests per layer.
DEFAULT_INTERVAL = timedelta(minutes=15)
# Most frames a model exports per check. A new run of every model fits; a backlog (a new model or layer, a
# config change, an outage) takes a few checks rather than one that runs for an hour and holds up the others.
FRAMES_PER_MODEL = 400
# Most time one model may take in a check, and the check as a whole (which also reads annotations and opens
# datasets). Every request has its own timeout; these catch anything that still hangs.
MODEL_TIMEOUT = timedelta(minutes=40)
CHECK_TIMEOUT = timedelta(minutes=45)
# How late a check may start or finish before the status calls the exporter overdue
OVERDUE_GRACE = timedelta(minutes=10)


def build_sources(storage: StorageClient) -> dict[str, ForecastSource]:
    """Every kind of forecast source, by `SourceConfig.kind`; UW WRF reads its annotations from `storage`."""
    return {"uw_wrf": UwWrfSource(ImageClient(), AnnotationStore(storage)), "dynamical": DynamicalSource()}


def build_driver(
    config: ModelsConfig,
    storage: StorageClient,
    public_storage: StorageClient,
    model_timeout: timedelta | None = MODEL_TIMEOUT,
) -> ForecastDriver:
    """Create the export driver: annotations come from the private `storage`, and forecasts go to `public_storage`."""
    return ForecastDriver(
        config=config,
        sources=build_sources(storage),
        store=ForecastStore(public_storage),
        frames_per_model=FRAMES_PER_MODEL,
        model_timeout=model_timeout.total_seconds() if model_timeout is not None else None,
    )


def seconds_until_next(now: datetime, interval: timedelta) -> float:
    """Seconds from `now` until the next multiple of `interval` past midnight (e.g. :00, :15, :30, :45)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = now - midnight
    return (interval - elapsed % interval).total_seconds()


def model_statuses(report: CheckReport) -> list[ModelCheckStatus]:
    """Each model's outcome, as the export status records it."""
    return [
        ModelCheckStatus(model.model, model.state, model.frames, model.error, model.layer_errors, round(model.seconds, 1))
        for model in report.models
    ]


@define
class ExportLoop:
    """Runs export checks on the clock and records each one's outcome in `export-status.json`.

    Attributes:
        driver: The export pipeline.
        store: Where the status is written (the driver's store).
        interval: Time between checks; should divide a day evenly.
        check_timeout: Most time a check may take before it's stopped.
        now: Clock, for tests.
        sleep: Sleep function, for tests.
    """

    driver: ForecastDriver
    store: ForecastStore
    interval: timedelta = DEFAULT_INTERVAL
    check_timeout: timedelta = CHECK_TIMEOUT
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    checks: int = field(default=0, init=False)
    status: ExportStatus | None = field(default=None, init=False)

    def next_check(self, after: datetime) -> datetime:
        """When the next check is due after `after`: the next slot on the clock."""
        return after + timedelta(seconds=seconds_until_next(after, self.interval))

    async def run_forever(self, run_immediately: bool = False, on_check: Callable[[ExportStatus], Awaitable[None]] | None = None) -> None:
        """Run a check at every slot (e.g. :00, :15, ...), forever. Only cancellation stops it.

        A check that runs past the next slot makes the loop skip to the one after, so checks never overlap.

        Args:
            run_immediately: Also run a check right away, before waiting for the first slot.
            on_check: Called with the status after each check.
        """
        self.status = await self._read_status()
        logger.info("exporter started: checking every %s, each check limited to %s", self.interval, self.check_timeout)
        skip_wait = run_immediately
        while True:
            if skip_wait:
                skip_wait = False
            else:
                now = self.now()
                due = self.next_check(now)
                logger.info("next check at %s", f"{due:%H:%M:%S}Z")
                await self.sleep((due - now).total_seconds())
            try:
                status = await self.run_check()
                if on_check is not None:
                    await on_check(status)
            except Exception:
                # run_check records its own failures; this is only a backstop so the loop never dies
                logger.exception("unexpected error in the export loop; continuing at the next check")

    async def run_check(self, force: bool = False) -> ExportStatus:
        """Run one check and record its outcome. Never raises, except when cancelled.

        Args:
            force: Export every layer again.

        Returns:
            The status written at the end of the check.
        """
        self.checks += 1
        with log_context(f"check {self.checks}"):
            started = self.now()
            start = time.monotonic()
            previous = self.status
            logger.info("check started")
            self.status = ExportStatus(
                state="running",
                check=self.checks,
                started_at=started,
                overdue_after=started + self.check_timeout + OVERDUE_GRACE,
                last_success_at=previous.last_success_at if previous else None,
                consecutive_failures=previous.consecutive_failures if previous else 0,
                models=previous.models if previous else [],
            )
            await self._write_status(self.status)

            report: CheckReport | None = None
            error: str | None = None
            try:
                async with asyncio.timeout(self.check_timeout.total_seconds()):
                    report = await self.driver.run(force=force)
            except TimeoutError:
                error = f"check stopped after {self.check_timeout}; resumes at the next check"
                logger.error("%s", error)
            except Exception as e:
                error = f"check failed: {describe(e)}"
                logger.exception("check failed")

            finished = self.now()
            failed = error is not None or bool(report and report.failed)
            next_check = self.next_check(finished)
            self.status = evolve(
                self.status,
                state="failed" if failed else "ok",
                finished_at=finished,
                next_check_at=next_check,
                overdue_after=next_check + OVERDUE_GRACE,
                last_success_at=self.status.last_success_at if failed else finished,
                consecutive_failures=self.status.consecutive_failures + 1 if failed else 0,
                error=error,
                models=model_statuses(report) if report is not None else self.status.models,
            )
            self._log_summary(report, time.monotonic() - start)
            await self._write_status(self.status)
            return self.status

    def _log_summary(self, report: CheckReport | None, seconds: float) -> None:
        status = self.status
        if report is None:
            logger.error("check finished in %.1fs without exporting (%d failed in a row)", seconds, status.consecutive_failures)
            return
        skipped = [model.model for model in report.models if model.state == "skipped"]
        retries = sum(len(model.layer_errors) for model in report.models)
        logger.log(
            logging.WARNING if status.state == "failed" else logging.INFO,
            "check finished in %.1fs: %d frames from %d models%s%s%s",
            seconds,
            report.frames,
            len(report.models),
            f"; failed: {', '.join(report.failed)}" if report.failed else "",
            f"; skipped: {', '.join(skipped)}" if skipped else "",
            f"; {retries} frames to retry" if retries else "",
        )

    async def _read_status(self) -> ExportStatus | None:
        try:
            return await asyncio.to_thread(self.store.read_export_status)
        except Exception:
            logger.exception("could not read the previous export status; starting afresh")
            return None

    async def _write_status(self, status: ExportStatus) -> None:
        # the status is for watching the exporter; failing to write it must not stop an export
        try:
            await asyncio.to_thread(self.store.write_export_status, status)
        except Exception:
            logger.exception("could not write the export status")
