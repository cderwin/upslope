"""The exporter process: scheduling, resilience to failures, the export status it records, and log tagging."""

import asyncio
import io
import logging
from datetime import UTC, datetime, timedelta

import pytest

from upslope.clients.forecasts import EXPORT_STATUS, ForecastStore
from upslope.export_loop import OVERDUE_GRACE, ExportLoop, seconds_until_next
from upslope.logging import LOG_FORMAT, _ContextFilter, log_context
from upslope.models.driver import CheckReport, ModelReport

NOW = datetime(2026, 11, 8, 14, 7, tzinfo=UTC)


class FakeDriver:
    """Stands in for ForecastDriver.run: returns the queued outcomes in turn (a report, or an exception to raise)."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def run(self, force: bool = False) -> CheckReport:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "hang":
            await asyncio.Event().wait()
        return outcome


OK = CheckReport([ModelReport("gfs_0.25", frames=12), ModelReport("uwwrf_4km", "skipped", error="no annotations")])
ONE_FAILED = CheckReport([ModelReport("gfs_0.25", "failed", error="HTTP 503"), ModelReport("uwwrf_4km", layer_errors=["snow hour 3: HTTP 503"])])


def make_loop(memory_storage, driver: FakeDriver, **kwargs) -> ExportLoop:
    return ExportLoop(driver, ForecastStore(memory_storage), now=lambda: NOW, **kwargs)


def test_seconds_until_next() -> None:
    quarter = timedelta(minutes=15)
    assert seconds_until_next(datetime(2026, 10, 21, 13, 59, 30), quarter) == 30
    assert seconds_until_next(datetime(2026, 10, 21, 14, 0, 0), quarter) == 900
    assert seconds_until_next(datetime(2026, 10, 21, 14, 7, 0), quarter) == 480
    assert seconds_until_next(datetime(2026, 10, 21, 13, 59, 30), timedelta(hours=1)) == 30


def test_check_records_its_outcome(memory_storage) -> None:
    loop = make_loop(memory_storage, FakeDriver(OK))
    status = asyncio.run(loop.run_check())
    assert (status.state, status.check, status.error, status.consecutive_failures) == ("ok", 1, None, 0)
    assert (status.started_at, status.finished_at, status.last_success_at) == (NOW, NOW, NOW)
    assert status.next_check_at == datetime(2026, 11, 8, 14, 15, tzinfo=UTC)
    assert status.overdue_after == status.next_check_at + OVERDUE_GRACE
    assert [(m.id, m.state, m.frames, m.error) for m in status.models] == [
        ("gfs_0.25", "ok", 12, None), ("uwwrf_4km", "skipped", 0, "no annotations"),
    ]
    assert ForecastStore(memory_storage).read_export_status() == status


def test_check_writes_a_running_status_first(memory_storage) -> None:
    """A check that hangs or crashes the process still leaves a status saying it was running, and until when to expect it."""
    store = ForecastStore(memory_storage)
    seen = []

    class Watching(FakeDriver):
        async def run(self, force: bool = False) -> CheckReport:
            seen.append(store.read_export_status())
            return await super().run(force)

    loop = make_loop(memory_storage, Watching(OK, OK))
    asyncio.run(loop.run_check())
    asyncio.run(loop.run_check())
    running = seen[1]
    assert (running.state, running.check, running.finished_at) == ("running", 2, None)
    assert running.overdue_after == NOW + loop.check_timeout + OVERDUE_GRACE
    # what the last finished check did is kept while the next runs
    assert running.last_success_at == NOW and [m.id for m in running.models] == ["gfs_0.25", "uwwrf_4km"]


def test_failures_are_counted_until_a_check_succeeds(memory_storage) -> None:
    loop = make_loop(memory_storage, FakeDriver(ONE_FAILED, RuntimeError("boom"), OK))
    first = asyncio.run(loop.run_check())
    assert (first.state, first.consecutive_failures, first.last_success_at, first.error) == ("failed", 1, None, None)
    assert first.models[0].error == "HTTP 503" and first.models[1].layer_errors == ["snow hour 3: HTTP 503"]

    # an error escaping the driver fails the check, not the loop; the models' last outcomes are kept
    second = asyncio.run(loop.run_check())
    assert (second.state, second.consecutive_failures, second.error) == ("failed", 2, "check failed: RuntimeError: boom")
    assert second.models == first.models

    third = asyncio.run(loop.run_check())
    assert (third.state, third.consecutive_failures, third.last_success_at) == ("ok", 0, NOW)


def test_a_check_that_hangs_is_stopped(memory_storage) -> None:
    loop = make_loop(memory_storage, FakeDriver("hang"), check_timeout=timedelta(seconds=0.05))
    status = asyncio.run(loop.run_check())
    assert status.state == "failed" and status.error.startswith("check stopped after")


def test_status_write_failures_dont_stop_a_check(memory_storage) -> None:
    def broken(*args) -> None:
        raise RuntimeError("s3 down")

    memory_storage.put_bytes = broken
    status = asyncio.run(make_loop(memory_storage, FakeDriver(OK)).run_check())
    assert status.state == "ok"


def test_run_forever_keeps_going_after_failures(memory_storage) -> None:
    """Checks run at each slot whatever happens in them; only cancellation stops the loop."""
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    statuses = []

    async def on_check(status) -> None:
        statuses.append(status.state)
        if len(statuses) == 2:
            raise RuntimeError("hook failed")  # logged, not fatal

    driver = FakeDriver(RuntimeError("boom"), OK, OK)
    loop = make_loop(memory_storage, driver, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(loop.run_forever(run_immediately=True, on_check=on_check))
    assert driver.calls == 3 and statuses == ["failed", "ok", "ok"]
    assert sleeps[:2] == [480.0, 480.0]  # to 14:15 each time, by the fake clock


def test_run_forever_waits_for_the_first_slot(memory_storage) -> None:
    async def sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    driver = FakeDriver(OK)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(make_loop(memory_storage, driver, sleep=sleep).run_forever())
    assert driver.calls == 0


def test_run_forever_resumes_the_check_count_from_storage(memory_storage) -> None:
    """A restarted exporter carries on the failure count and last success, so the status doesn't reset."""
    store = ForecastStore(memory_storage)
    asyncio.run(make_loop(memory_storage, FakeDriver(ONE_FAILED)).run_check())

    async def stop(seconds: float) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(make_loop(memory_storage, FakeDriver(ONE_FAILED), sleep=stop).run_forever(run_immediately=True))
    status = store.read_export_status()
    assert (status.check, status.consecutive_failures) == (1, 2)
    assert EXPORT_STATUS in memory_storage.objects


def test_log_context_tags_records_in_tasks_and_threads() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(_ContextFilter())
    logger = logging.getLogger("upslope.test_log_context")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    async def model(model_id: str) -> None:
        with log_context(model_id):
            await asyncio.sleep(0)
            await asyncio.to_thread(logger.info, "rendered")

    async def check() -> None:
        with log_context("check 3"):
            await asyncio.gather(model("gfs"), model("hrrr"))
        logger.info("done")

    try:
        asyncio.run(check())
    finally:
        logger.removeHandler(handler)
    lines = sorted(line.split(": ", 1)[1] for line in stream.getvalue().splitlines())
    assert lines == ["[check 3] [gfs] rendered", "[check 3] [hrrr] rendered", "done"]
