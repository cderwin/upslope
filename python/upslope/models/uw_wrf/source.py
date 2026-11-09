"""UW WRF as a forecast source: map images downloaded from UW and placed with the georegistration annotations."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx
from attrs import define, field

from upslope.clients.annotations import AnnotationStore
from upslope.logging import get_logger, log_duration
from upslope.models.config import LayerConfig, ModelConfig
from upslope.models.source import NotPublished, RenderedFrame, SourceUnavailable
from upslope.models.uw_wrf.georegistration import Registration
from upslope.models.uw_wrf.georegistration_document import AnnotationDocument
from upslope.models.uw_wrf.image_client import ImageClient

logger = get_logger(__name__)


@define
class UwWrfSource:
    """Downloads UW WRF images and georegisters them with the published annotations.

    The annotations are read once per check, so a publish takes effect at the next one. Every UW model
    shares the client, and so its throttle.

    Attributes:
        client: Client for UW's image server.
        annotations: Source of the published georegistration annotations.
    """

    client: ImageClient
    annotations: AnnotationStore
    _published: AnnotationDocument | None = field(default=None, init=False)
    _registration: Registration | None = field(default=None, init=False)
    _registration_lock: asyncio.Lock = field(factory=asyncio.Lock, init=False)

    @asynccontextmanager
    async def check(self) -> AsyncIterator[None]:
        self._published = await asyncio.to_thread(self.annotations.load_published)
        self._registration = None
        if self._published is None:
            logger.error("no georegistration annotations have been published; skipping UW WRF models")
        else:
            logger.info("using georegistration annotations revision %d", self._published.revision)
        try:
            yield
        finally:
            self._published = self._registration = None

    def _require_published(self) -> AnnotationDocument:
        if self._published is None:
            raise SourceUnavailable("no georegistration annotations have been published")
        return self._published

    async def latest_complete_run(self, model: ModelConfig, now: datetime) -> datetime | None:
        self._require_published()
        return await self.client.latest_complete_run(model, now)

    def revision(self, model: ModelConfig) -> int | None:
        return self._require_published().revision

    async def _registration_for(self, published: AnnotationDocument) -> Registration:
        # fitting is only needed once something is downloaded, and only once for every model
        async with self._registration_lock:
            if self._registration is None:
                with log_duration(logger, "fitted the georegistration"):
                    self._registration = await asyncio.to_thread(Registration.build, published)
            return self._registration

    async def render_frame(self, model: ModelConfig, layer: LayerConfig, initialized: datetime, hour: int, out: Path) -> RenderedFrame:
        published = self._require_published()
        try:
            data = await self.client.download_image(layer, initialized, hour)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise NotPublished(f"{model.id}/{layer.id} hour {hour}") from e
            raise
        registration = await self._registration_for(published)
        await asyncio.to_thread(self._georegister, registration, data, out, layer.maptype)
        # UW's images carry their own legend, and have no numbers to read back
        return RenderedFrame()

    @staticmethod
    def _georegister(registration: Registration, data: bytes, out: Path, maptype: str) -> None:
        gif = out.with_suffix(".gif")
        try:
            gif.write_bytes(data)
            registration.georegister(gif, out, maptype)
        finally:
            gif.unlink(missing_ok=True)
