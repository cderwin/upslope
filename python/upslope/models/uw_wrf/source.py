"""UW WRF as a forecast source: map images downloaded from UW and placed with the georegistration annotations."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import httpx
from attrs import define, field

from upslope.clients.annotations import AnnotationStore
from upslope.logging import get_logger
from upslope.models.config import LayerConfig, ModelConfig
from upslope.models.source import NotPublished, SourceUnavailable
from upslope.models.uw_wrf.georegistration import Registration
from upslope.models.uw_wrf.georegistration_document import AnnotationDocument
from upslope.models.uw_wrf.image_client import ImageClient

logger = get_logger(__name__)


@define
class UwWrfSource:
    """Downloads UW WRF images and georegisters them with the published annotations.

    The annotations are read once per check, so a publish takes effect at the next one.

    Attributes:
        client: Client for UW's image server.
        annotations: Source of the published georegistration annotations.
    """

    client: ImageClient
    annotations: AnnotationStore
    _published: AnnotationDocument | None = field(default=None, init=False)
    _registration: Registration | None = field(default=None, init=False)

    @contextmanager
    def check(self) -> Iterator[None]:
        self._published = self.annotations.load_published()
        self._registration = None
        if self._published is None:
            logger.error("no georegistration annotations have been published; skipping UW WRF models")
        try:
            yield
        finally:
            self._published = self._registration = None

    def _require_published(self) -> AnnotationDocument:
        if self._published is None:
            raise SourceUnavailable("no georegistration annotations have been published")
        return self._published

    def latest_complete_run(self, model: ModelConfig, now: datetime) -> datetime | None:
        self._require_published()
        return self.client.latest_complete_run(model, now)

    def revision(self, model: ModelConfig) -> int | None:
        return self._require_published().revision

    def render_frame(self, model: ModelConfig, layer: LayerConfig, initialized: datetime, hour: int, out: Path) -> None:
        published = self._require_published()
        try:
            data = self.client.download_image(layer, initialized, hour)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise NotPublished(f"{model.id}/{layer.id} hour {hour}") from e
            raise
        if self._registration is None:
            # fitting is only needed once something is downloaded
            self._registration = Registration.build(published)
        gif = out.with_suffix(".gif")
        try:
            gif.write_bytes(data)
            self._registration.georegister(gif, out, layer.maptype)
        finally:
            gif.unlink(missing_ok=True)
