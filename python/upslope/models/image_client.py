"""HTTP client for downloading a model run's forecast images, using each layer's URL template."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
from attrs import define, field

from upslope.logging import get_logger
from upslope.models.config import LayerConfig, ModelConfig

logger = get_logger(__name__)


def raw_image_name(hour: int) -> str:
    """File name of a downloaded image in a layer's working directory."""
    return f"hour{hour:02d}.gif"


@define
class ImageClient:
    """Downloads forecast images from a specific model run.

    Layer URLs point into the run's own directory. For UW WRF that matters: UW also serves the newest
    images under `timeindep/`, but overwrites them in place while a run is in progress, so they mix
    two runs for hours at a time. Each run's own directory is never mixed, and UW keeps them for at
    least two weeks.
    """

    _client: httpx.Client = field(factory=lambda: httpx.Client(timeout=60.0))

    def download_image(self, layer: LayerConfig, initialized: datetime, hour: int) -> bytes:
        """Download one forecast hour of a layer.

        Args:
            layer: Layer to download.
            initialized: Initialization time of the model run to download from.
            hour: Forecast hour.

        Returns:
            The image's bytes. Raises httpx.HTTPError if the download fails.
        """
        response = self._client.get(layer.image_url(initialized, hour))
        _ = response.raise_for_status()
        return response.content

    def download_layer(self, layer: LayerConfig, initialized: datetime, directory: Path) -> None:
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
            _ = path.write_bytes(self.download_image(layer, initialized, hour))

    def image_exists(self, layer: LayerConfig, initialized: datetime, hour: int) -> bool:
        """Check whether one forecast image has been published.

        Args:
            layer: Layer to check.
            initialized: Initialization time of the model run.
            hour: Forecast hour.

        Returns:
            True if it exists, False on a 404. Raises httpx.HTTPError on other errors, so an outage
            is never mistaken for an image that isn't published yet.
        """
        response = self._client.head(layer.image_url(initialized, hour))
        if response.status_code == 404:
            return False
        _ = response.raise_for_status()
        return True

    def run_complete(self, model: ModelConfig, initialized: datetime) -> bool:
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
            if not self.image_exists(layer, initialized, layer.hours.last):
                logger.debug("%s run %s is incomplete: %s has no hour %d", model.id, f"{initialized:%Y-%m-%d %HZ}", layer.id, layer.hours.last)
                return False
        return True

    def latest_complete_run(self, model: ModelConfig, now: datetime | None = None) -> datetime | None:
        """Find the newest run of a model that has been fully published.

        Args:
            model: Model to check.
            now: Current time; defaults to now. Only runs initialized at or before it are considered.

        Returns:
            The run's initialization time (UTC), or None if no run in the model's lookback window is complete.
        """
        for initialized in model.initializations(now or datetime.now(UTC)):
            if self.run_complete(model, initialized):
                return initialized
        return None
