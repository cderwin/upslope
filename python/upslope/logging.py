"""Shared logging setup for upslope commands."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(log_context)s%(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that are chatty at INFO/DEBUG (e.g. httpx logs every request).
_NOISY_LOGGERS = ("httpx", "httpcore", "botocore", "boto3", "s3transfer", "urllib3", "rasterio")

# Tags prefixed to every record logged in the current context, e.g. ("check 12", "gfs_0.25"). Asyncio tasks
# and `asyncio.to_thread` copy the context, so work done for a model in parallel with others stays labelled.
_context: ContextVar[tuple[str, ...]] = ContextVar("upslope_log_context", default=())


def get_logger(name: str) -> logging.Logger:
    """Get a logger; pass `__name__` so loggers nest under `upslope`."""
    return logging.getLogger(name)


@contextmanager
def log_context(tag: str) -> Iterator[None]:
    """Label every record logged inside the block (in this task/thread and ones it starts) with `[tag]`."""
    token = _context.set((*_context.get(), tag))
    try:
        yield
    finally:
        _context.reset(token)


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        tags = _context.get()
        record.log_context = "".join(f"[{tag}] " for tag in tags)
        return True


def configure_logging(verbose: bool = False) -> None:
    """Send log records to stderr with a timestamped format.

    Args:
        verbose: Log upslope at DEBUG instead of INFO. Third-party libraries stay at WARNING either way.
    """
    logging.basicConfig(format=LOG_FORMAT, datefmt=DATE_FORMAT, level=logging.WARNING, force=True)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_ContextFilter())
    logging.getLogger("upslope").setLevel(logging.DEBUG if verbose else logging.INFO)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # server startup and access logs
    logging.getLogger("uvicorn").setLevel(logging.INFO)


@contextmanager
def log_duration(logger: logging.Logger, message: str, level: int = logging.INFO) -> Iterator[None]:
    """Log `message` with the elapsed time once the block finishes."""
    start = perf_counter()
    yield
    logger.log(level, "%s in %.1fs", message, perf_counter() - start)
