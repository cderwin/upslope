"""Shared logging setup for upslope commands."""

import logging
from time import perf_counter
from collections.abc import Iterator
from contextlib import contextmanager

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that are chatty at INFO/DEBUG (e.g. httpx logs every request).
_NOISY_LOGGERS = ("httpx", "httpcore", "botocore", "boto3", "s3transfer", "urllib3", "rasterio")


def get_logger(name: str) -> logging.Logger:
    """Get a logger; pass `__name__` so loggers nest under `upslope`."""
    return logging.getLogger(name)


def configure_logging(verbose: bool = False) -> None:
    """Send log records to stderr with a timestamped format.

    Args:
        verbose: Log upslope at DEBUG instead of INFO. Third-party libraries stay at WARNING either way.
    """
    logging.basicConfig(format=LOG_FORMAT, datefmt=DATE_FORMAT, level=logging.WARNING, force=True)
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
