"""Async HTTP client for downloading a UW WRF run's forecast images, using each layer's URL template.

UW's image server is one modest machine that sometimes answers 503 under load, so every request goes
through a shared throttle (a few at a time, spaced out) and is retried with exponential backoff on
overload responses and connection errors. A 404 is never retried: it means "not published yet".
"""

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from attrs import define, field

from upslope.logging import get_logger
from upslope.models.config import LayerConfig, ModelConfig

logger = get_logger(__name__)

# Statuses that mean "try again later" rather than "this image doesn't exist"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Images are small; a response that takes longer than this is stuck
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def raw_image_name(hour: int) -> str:
    """File name of a downloaded image in a layer's working directory."""
    return f"hour{hour:02d}.gif"


@define
class Throttle:
    """Limits requests to one server: at most `concurrency` at once, each starting at least `interval` after the last.

    Attributes:
        concurrency: Most requests in flight.
        interval: Seconds between request starts.
        sleep: Sleep function, for tests.
    """

    concurrency: int = 2
    interval: float = 0.2
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    _semaphore: asyncio.Semaphore = field(init=False)
    _lock: asyncio.Lock = field(factory=asyncio.Lock, init=False)
    _next_start: float = field(default=0.0, init=False)

    def __attrs_post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.concurrency)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold a request slot for the block, waiting for one if needed."""
        async with self._semaphore:
            async with self._lock:
                wait = self._next_start - time.monotonic()
                if wait > 0:
                    logger.debug("throttling UW request for %.2fs", wait)
                    await self.sleep(wait)
                self._next_start = time.monotonic() + self.interval
            yield

    def pause(self, seconds: float) -> None:
        """Start no request for `seconds`: when the server says it's overloaded, every request backs off."""
        self._next_start = max(self._next_start, time.monotonic() + seconds)


@define
class RetryPolicy:
    """How to retry a request that failed for a reason that may pass.

    Attributes:
        attempts: Most tries per request, including the first.
        initial_delay: Seconds to wait before the first retry; doubles with each one.
        max_delay: Longest wait between tries.
    """

    attempts: int = 4
    initial_delay: float = 2.0
    max_delay: float = 30.0

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait after failed try number `attempt` (from 1), with jitter, honoring a Retry-After."""
        backoff = min(self.max_delay, self.initial_delay * 2 ** (attempt - 1)) * random.uniform(0.75, 1.0)
        return min(self.max_delay, max(backoff, retry_after or 0.0))


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None  # absent, or an HTTP date, which UW doesn't send


@define
class ImageClient:
    """Downloads forecast images from a specific model run.

    Layer URLs point into the run's own directory. For UW WRF that matters: UW also serves the newest
    images under `timeindep/`, but overwrites them in place while a run is in progress, so they mix
    two runs for hours at a time. Each run's own directory is never mixed, and UW keeps them for at
    least two weeks.

    Attributes:
        client: HTTP client; its timeouts apply to each try.
        throttle: Shared by every request this client makes.
        retry: When and how long to wait to retry a failed request.
        sleep: Sleep function for backoff, for tests.
    """

    _client: httpx.AsyncClient = field(factory=lambda: httpx.AsyncClient(timeout=DEFAULT_TIMEOUT))
    throttle: Throttle = field(factory=Throttle)
    retry: RetryPolicy = field(factory=RetryPolicy)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, url: str) -> httpx.Response:
        """Make a throttled request, retrying overload responses and connection errors.

        Returns:
            The response, which may still be an error (e.g. a 404, or a 503 after the last try). Raises
            httpx.TransportError if the last try couldn't connect or timed out.
        """
        for attempt in range(1, self.retry.attempts + 1):
            try:
                async with self.throttle.slot():
                    response = await self._client.request(method, url)
            except httpx.TransportError as e:
                if attempt == self.retry.attempts:
                    logger.warning("%s %s failed after %d tries: %s", method, url, attempt, e.__class__.__name__)
                    raise
                reason, delay = e.__class__.__name__, self.retry.delay(attempt)
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                if attempt == self.retry.attempts:
                    logger.warning("%s %s still answered HTTP %d after %d tries", method, url, response.status_code, attempt)
                    return response
                reason, delay = f"HTTP {response.status_code}", self.retry.delay(attempt, _retry_after(response))
                if response.status_code in (429, 503):
                    # the server is overloaded: hold back every request, not just this one
                    self.throttle.pause(delay)
            logger.warning("%s %s: %s; retrying in %.1fs (try %d of %d)", method, url, reason, delay, attempt + 1, self.retry.attempts)
            await self.sleep(delay)
        raise AssertionError("unreachable")

    async def download_image(self, layer: LayerConfig, initialized: datetime, hour: int) -> bytes:
        """Download one forecast hour of a layer.

        Args:
            layer: Layer to download.
            initialized: Initialization time of the model run to download from.
            hour: Forecast hour.

        Returns:
            The image's bytes. Raises httpx.HTTPError if the download fails.
        """
        response = await self.request("GET", layer.image_url(initialized, hour))
        _ = response.raise_for_status()
        return response.content

    async def download_layer(self, layer: LayerConfig, initialized: datetime, directory: Path) -> None:
        """Download every forecast hour of a layer, skipping files already on disk.

        Args:
            layer: Layer to download.
            initialized: Initialization time of the model run to download from.
            directory: Where to write the images, named by `raw_image_name`.
        """
        directory.mkdir(exist_ok=True, parents=True)
        for hour in layer.hours.hours:
            path = directory / raw_image_name(hour)
            if path.exists():
                logger.debug("skipping %s, already downloaded", path)
                continue
            logger.debug("downloading %s", layer.image_url(initialized, hour))
            _ = path.write_bytes(await self.download_image(layer, initialized, hour))

    async def image_exists(self, layer: LayerConfig, initialized: datetime, hour: int) -> bool:
        """Check whether one forecast image has been published.

        Args:
            layer: Layer to check.
            initialized: Initialization time of the model run.
            hour: Forecast hour.

        Returns:
            True if it exists, False on a 404. Raises httpx.HTTPError on other errors, so an outage
            is never mistaken for an image that isn't published yet.
        """
        response = await self.request("HEAD", layer.image_url(initialized, hour))
        if response.status_code == 404:
            return False
        _ = response.raise_for_status()
        return True

    async def run_complete(self, model: ModelConfig, initialized: datetime) -> bool:
        """Check whether every layer of a model run has been published.

        Models publish a run's forecast hours in order, so a run is complete once each layer's last
        hour exists.

        Args:
            model: Model to check.
            initialized: Initialization time of the run.

        Returns:
            True if every layer's last hour exists. Raises httpx.HTTPError on errors other than 404.
        """
        for layer in model.layers:
            if not await self.image_exists(layer, initialized, layer.hours.last):
                logger.debug("%s run %s is incomplete: %s has no hour %d", model.id, f"{initialized:%Y-%m-%d %HZ}", layer.id, layer.hours.last)
                return False
        return True

    async def latest_complete_run(self, model: ModelConfig, now: datetime | None = None) -> datetime | None:
        """Find the newest run of a model that has been fully published.

        Args:
            model: Model to check.
            now: Current time; defaults to now. Only runs initialized at or before it are considered.

        Returns:
            The run's initialization time (UTC), or None if no run in the model's lookback window is complete.
        """
        for initialized in model.initializations(now or datetime.now(UTC)):
            if await self.run_complete(model, initialized):
                return initialized
        return None
