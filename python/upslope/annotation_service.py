"""Editing, checking and publishing the georegistration annotations.

Every change goes to the shared draft through a read-change-write cycle: changes are serialized by
a lock (the server runs on one machine), each one increases the draft's revision, and the write is
conditional on the stored draft being unchanged, so a write from another process is never lost.
"""

import asyncio
import io
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TypeVar

import httpx
from PIL import Image, UnidentifiedImageError

from attrs import define

from upslope.clients.annotations import AnnotationStore, HistoryEntry, HistorySummary
from upslope.clients.storage import PreconditionFailed, StoredObject
from upslope.logging import get_logger
from upslope.models.uw_wrf.georegistration import MaptypeFit, fit_report
from upslope.models.uw_wrf.driver import WrfDriver
from upslope.models.uw_wrf.georegistration_document import (
    AnnotatedImage,
    AnnotationDocument,
    GeographicPoint,
    ImageSource,
    MaptypeAnnotations,
    PixelAnnotation,
    is_valid_key,
)
from upslope.models.uw_wrf.wrf_client import LayerConfig, RunStatusError, WrfClient, WrfConfig

logger = get_logger(__name__)

T = TypeVar("T")


class AnnotationError(Exception):
    """Base class for errors the admin API reports to the client."""


class NotFound(AnnotationError):
    """The point, maptype, image, annotation or history entry does not exist."""


class Invalid(AnnotationError):
    """The request would make the annotations invalid."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


class UpstreamError(AnnotationError):
    """The forecast image could not be downloaded from UW."""


class Conflict(AnnotationError):
    """The draft changed since the revision the client based its request on."""

    def __init__(self, current_revision: int) -> None:
        super().__init__(f"the draft has changed (now r{current_revision}); reload and try again")
        self.current_revision = current_revision


@define
class DraftState:
    """The draft as the admin API returns it.

    Attributes:
        document: The draft document.
        published_revision: Revision of the published document, or None if nothing is published.
    """

    document: AnnotationDocument
    published_revision: int | None


@define
class PromotableLayer:
    """A configured forecast layer whose frames can be promoted for annotation.

    Attributes:
        model: Manifest model name, e.g. `uwwrf_4km` (as in the forecast map's URLs).
        variable: Forecast variable.
        maptype: Maptype the layer's images belong to.
        hours: Forecast hours available.
    """

    model: str
    variable: str
    maptype: str
    hours: list[int]


@define
class PromoteResult:
    """Outcome of promoting a forecast frame.

    Attributes:
        draft: The draft with the image added.
        maptype: The image's maptype.
        image: The new (or already present) image ID.
    """

    draft: DraftState
    maptype: str
    image: str


@define
class PublishResult:
    """Outcome of a publish.

    Attributes:
        history: The new history entry's summary.
        warnings: Maptypes whose fit error exceeds their threshold (publishing still happened).
    """

    history: HistorySummary
    warnings: list[str]


def copy_document(document: AnnotationDocument) -> AnnotationDocument:
    return AnnotationDocument.from_json(document.to_json())


def to_key(value: str) -> str:
    """Turn a display name into a point key, e.g. `Cape Flattery, WA` -> `cape_flattery_wa`."""
    key = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:96]
    return key or "point"


def _check_point(name: str, latitude: float, longitude: float) -> GeographicPoint:
    name = name.strip()
    if not name or len(name) > 200:
        raise Invalid("a point needs a name of 1-200 characters")
    if not (math.isfinite(latitude) and -90 <= latitude <= 90):
        raise Invalid("latitude must be between -90 and 90")
    if not (math.isfinite(longitude) and -180 <= longitude <= 180):
        raise Invalid("longitude must be between -180 and 180")
    return GeographicPoint(name=name, latitude=latitude, longitude=longitude)


def _check_keys(*keys: str) -> None:
    for key in keys:
        if not is_valid_key(key):
            raise NotFound(f"no such item: {key!r}")


class AnnotationService:
    """Edits the shared draft, and publishes it for the WRF export.

    Args:
        store: Where the annotations live.
        wrf_config: Configured layers; publishing requires every layer's maptype to be fitted.
        wrf_client: Client for downloading frames to promote. Built from `wrf_config` if omitted.
    """

    def __init__(self, store: AnnotationStore, wrf_config: WrfConfig, wrf_client: WrfClient | None = None) -> None:
        self.store = store
        self.wrf_config = wrf_config
        self.wrf_client = wrf_client or WrfClient(wrf_config)
        self._lock = asyncio.Lock()
        self._published_revision: int | None = None

    # --- reading

    def _load_draft(self) -> tuple[AnnotationDocument, str | None, bool]:
        """The draft, its ETag, and whether it exists; a missing draft starts from the published copy."""
        loaded = self.store.load_draft()
        if loaded is not None:
            return loaded[0], loaded[1], True
        published = self.store.load_published()
        return (copy_document(published) if published else AnnotationDocument()), None, False

    def _refresh_published_revision(self) -> int | None:
        published = self.store.load_published()
        self._published_revision = None if published is None else published.revision
        return self._published_revision

    async def draft(self) -> DraftState:
        """The current draft, and the published revision."""

        def load() -> DraftState:
            document, _, _ = self._load_draft()
            return DraftState(document, self._refresh_published_revision())

        return await asyncio.to_thread(load)

    async def published(self) -> AnnotationDocument:
        """The published document. Raises NotFound if nothing is published."""
        document = await asyncio.to_thread(self.store.load_published)
        if document is None:
            raise NotFound("nothing has been published")
        return document

    async def fit(self) -> dict[str, MaptypeFit]:
        """How well each maptype in the draft fits."""
        state = await self.draft()
        return await asyncio.to_thread(fit_report, state.document)

    async def image(self, maptype: str, image_id: str, if_none_match: str | None = None) -> StoredObject:
        """An annotated image, for streaming. Raises NotFound if it does not exist."""
        _check_keys(maptype, image_id)
        stored = await asyncio.to_thread(self.store.get_image, maptype, image_id, if_none_match)
        if stored is None:
            raise NotFound(f"no image {maptype}/{image_id}")
        return stored

    async def history(self) -> list[HistorySummary]:
        """Published versions, newest first."""
        return await asyncio.to_thread(self.store.list_history)

    async def history_entry(self, history_id: str) -> HistoryEntry:
        """One published version. Raises NotFound if the ID is unknown."""
        entry = await asyncio.to_thread(self.store.load_history, history_id)
        if entry is None:
            raise NotFound(f"no history entry {history_id!r}")
        return entry

    # --- changing the draft

    async def _mutate(self, change: Callable[[AnnotationDocument], T], expected_revision: int | None) -> tuple[DraftState, T]:
        """Apply `change` to the draft and save it as the next revision.

        Args:
            change: Edits the document in place; raises an AnnotationError to abort.
            expected_revision: If given, the change applies only if the draft is still at this revision.

        Returns:
            The new draft state, and what `change` returned.
        """

        def apply() -> tuple[DraftState, T]:
            document, etag, exists = self._load_draft()
            if expected_revision is not None and expected_revision != document.revision:
                raise Conflict(document.revision)
            value = change(document)
            document.revision += 1
            try:
                if exists:
                    self.store.save_draft(document, if_match=etag)
                else:
                    self.store.save_draft(document, create=True)
            except PreconditionFailed:
                # another process wrote the draft between our read and write
                current, _, _ = self._load_draft()
                raise Conflict(current.revision) from None
            return DraftState(document, self._published_revision), value

        async with self._lock:
            return await asyncio.to_thread(apply)

    async def create_point(self, name: str, latitude: float, longitude: float, expected_revision: int | None = None) -> tuple[DraftState, str]:
        """Add a landmark, with a key derived from its name. Returns the new draft and the key."""
        point = _check_point(name, latitude, longitude)

        def change(document: AnnotationDocument) -> str:
            base = key = to_key(point.name)
            suffix = 2
            while key in document.geographic_points:
                key = f"{base}_{suffix}"
                suffix += 1
            document.geographic_points[key] = point
            return key

        return await self._mutate(change, expected_revision)

    async def update_point(self, key: str, name: str, latitude: float, longitude: float, expected_revision: int | None = None) -> DraftState:
        """Rename or move a landmark."""
        point = _check_point(name, latitude, longitude)

        def change(document: AnnotationDocument) -> None:
            if key not in document.geographic_points:
                raise NotFound(f"no point {key!r}")
            document.geographic_points[key] = point

        state, _ = await self._mutate(change, expected_revision)
        return state

    async def delete_point(self, key: str, expected_revision: int | None = None) -> tuple[DraftState, int]:
        """Delete a landmark and every annotation of it. Returns the new draft and how many annotations went."""

        def change(document: AnnotationDocument) -> int:
            if document.geographic_points.pop(key, None) is None:
                raise NotFound(f"no point {key!r}")
            removed = 0
            for annotations in document.maptypes.values():
                for image in annotations.images.values():
                    if image.annotations.pop(key, None) is not None:
                        removed += 1
            return removed

        return await self._mutate(change, expected_revision)

    async def put_annotation(
        self,
        maptype: str,
        image_id: str,
        point: str,
        pixel_x: int,
        pixel_y: int,
        replaces: str | None = None,
        expected_revision: int | None = None,
    ) -> DraftState:
        """Set where a landmark appears in an image.

        Args:
            maptype: Maptype of the image.
            image_id: Image ID.
            point: Key of the landmark.
            pixel_x: Column; must be inside the image.
            pixel_y: Row; must be inside the image.
            replaces: Another landmark's annotation on this image to remove in the same change, for
                when an existing annotation is switched to a different landmark.
            expected_revision: Apply only if the draft is still at this revision.
        """

        def change(document: AnnotationDocument) -> None:
            annotations = document.maptypes.get(maptype)
            image = None if annotations is None else annotations.images.get(image_id)
            if image is None:
                raise NotFound(f"no image {maptype}/{image_id}")
            if point not in document.geographic_points:
                raise Invalid(f"no point {point!r}")
            if not (0 <= pixel_x < image.width and 0 <= pixel_y < image.height):
                raise Invalid(f"pixel ({pixel_x}, {pixel_y}) is outside the {image.width}x{image.height} image")
            if replaces is not None and replaces != point:
                image.annotations.pop(replaces, None)
            image.annotations[point] = PixelAnnotation(pixel_x, pixel_y)

        state, _ = await self._mutate(change, expected_revision)
        return state

    async def delete_annotation(self, maptype: str, image_id: str, point: str, expected_revision: int | None = None) -> DraftState:
        """Remove a landmark's annotation from an image."""

        def change(document: AnnotationDocument) -> None:
            annotations = document.maptypes.get(maptype)
            image = None if annotations is None else annotations.images.get(image_id)
            if image is None or image.annotations.pop(point, None) is None:
                raise NotFound(f"no annotation of {point!r} on {maptype}/{image_id}")

        state, _ = await self._mutate(change, expected_revision)
        return state

    async def delete_image(self, maptype: str, image_id: str, expected_revision: int | None = None) -> DraftState:
        """Remove an image, and its annotations, from the draft. The image file stays in storage."""

        def change(document: AnnotationDocument) -> None:
            annotations = document.maptypes.get(maptype)
            if annotations is None or annotations.images.pop(image_id, None) is None:
                raise NotFound(f"no image {maptype}/{image_id}")

        state, _ = await self._mutate(change, expected_revision)
        return state

    async def set_fit_threshold(self, maptype: str, meters: float | None, expected_revision: int | None = None) -> DraftState:
        """Set (or clear, with None) the mean fit error above which publishing warns."""
        if meters is not None and not (math.isfinite(meters) and meters > 0):
            raise Invalid("the threshold must be a positive number of meters")

        def change(document: AnnotationDocument) -> None:
            if maptype not in document.maptypes:
                raise NotFound(f"no maptype {maptype!r}")
            document.maptypes[maptype].fit_threshold_m = meters

        state, _ = await self._mutate(change, expected_revision)
        return state

    def _replace_contents(self, source: AnnotationDocument) -> Callable[[AnnotationDocument], None]:
        def change(document: AnnotationDocument) -> None:
            replacement = copy_document(source)
            document.geographic_points = replacement.geographic_points
            document.maptypes = replacement.maptypes

        return change

    async def discard_draft(self, expected_revision: int | None = None) -> DraftState:
        """Reset the draft's contents to the published document (keeping revisions increasing)."""
        published = await asyncio.to_thread(self.store.load_published)
        state, _ = await self._mutate(self._replace_contents(published or AnnotationDocument()), expected_revision)
        return state

    async def restore(self, history_id: str, expected_revision: int | None = None) -> DraftState:
        """Copy a published version into the draft. It goes live only when the draft is published."""
        entry = await self.history_entry(history_id)
        state, _ = await self._mutate(self._replace_contents(entry.document), expected_revision)
        return state

    # --- promoting forecast frames

    def sources(self) -> list[PromotableLayer]:
        """Configured layers that frames can be promoted from."""
        return [
            PromotableLayer(
                f"{WrfDriver.model}_{layer.domain}",
                layer.variable,
                layer.maptype,
                self.wrf_config.get_forecast_hours(layer.forecast_schedule),
            )
            for layer in self.wrf_config.layers
        ]

    def _find_layer(self, model: str, variable: str) -> LayerConfig:
        for layer in self.wrf_config.layers:
            if f"{WrfDriver.model}_{layer.domain}" == model and layer.variable == variable:
                return layer
        raise NotFound(f"no configured layer {model}/{variable}")

    async def promote(self, model: str, variable: str, hour: int, expected_revision: int | None = None) -> PromoteResult:
        """Download a forecast frame from UW's latest run and add it to the draft for annotation.

        Args:
            model: Manifest model name, e.g. `uwwrf_4km`.
            variable: Forecast variable.
            hour: Forecast hour; must be in the layer's schedule.
            expected_revision: Apply only if the draft is still at this revision.

        Returns:
            The new draft and the image's maptype and ID. If the same frame of the same run was
            promoted before, the existing image is returned and the draft is unchanged.
        """
        layer = self._find_layer(model, variable)
        if hour not in self.wrf_config.get_forecast_hours(layer.forecast_schedule):
            raise Invalid(f"hour {hour} is not in the {model}/{variable} forecast schedule")

        def download() -> tuple[datetime, bytes, tuple[int, int]]:
            try:
                initialized = self.wrf_client.current_run_initialization()
                data = self.wrf_client.download_image(layer, hour)
            except (httpx.HTTPError, RunStatusError) as e:
                raise UpstreamError(f"could not download the frame from UW: {e}") from e
            try:
                with Image.open(io.BytesIO(data)) as image:
                    if image.format != "GIF":
                        raise UpstreamError(f"UW returned a {image.format} image, not a GIF")
                    return initialized, data, image.size
            except UnidentifiedImageError as e:
                raise UpstreamError("UW did not return an image") from e

        initialized, data, (width, height) = await asyncio.to_thread(download)
        maptype = layer.maptype
        image_id = f"{layer.domain}-{variable}-h{hour:02d}-{initialized:%Y%m%dT%H}Z"
        await asyncio.to_thread(self.store.put_image, maptype, image_id, data)

        class AlreadyPresent(Exception):
            pass

        def change(document: AnnotationDocument) -> None:
            annotations = document.maptypes.setdefault(maptype, MaptypeAnnotations())
            if image_id in annotations.images:
                raise AlreadyPresent
            for other_id, other in annotations.images.items():
                if (other.width, other.height) != (width, height):
                    raise Invalid(
                        f"the frame is {width}x{height}, but {maptype} image {other_id} is {other.width}x{other.height}; "
                        "a maptype's images must share one pixel grid"
                    )
            source = ImageSource(model, variable, hour, initialized, datetime.now(UTC))
            annotations.images[image_id] = AnnotatedImage(width=width, height=height, source=source)

        try:
            state, _ = await self._mutate(change, expected_revision)
        except AlreadyPresent:
            state = await self.draft()
        logger.info("promoted %s/%s hour %d to %s/%s", model, variable, hour, maptype, image_id)
        return PromoteResult(state, maptype, image_id)

    # --- publishing

    def check(self, document: AnnotationDocument) -> tuple[list[str], list[str]]:
        """Check a document before publishing.

        Returns:
            Problems that block publishing (unknown points, maptypes that can't be fitted or are
            missing for a configured layer), and warnings (fit error over threshold).
        """
        problems = [f"annotation of unknown point: {ref}" for ref in document.dangling_references()]
        report = fit_report(document)
        for maptype, fit in report.items():
            if fit.error is not None:
                problems.append(f"{maptype}: {fit.error} (has {fit.control_points})")
        for maptype in sorted({layer.maptype for layer in self.wrf_config.layers} - report.keys()):
            problems.append(f"{maptype}: no annotations, but a configured layer uses it")
        warnings = [
            f"{maptype}: mean fit error {fit.mean_error_m:.0f} m exceeds the {fit.fit_threshold_m:.0f} m threshold"
            for maptype, fit in report.items()
            if fit.over_threshold
        ]
        return problems, warnings

    async def publish(self, expected_revision: int, note: str | None = None) -> PublishResult:
        """Make the draft the published annotations, which the next hourly export uses.

        Args:
            expected_revision: The draft revision the client reviewed; raises Conflict if it changed.
            note: Optional note kept in history.

        Returns:
            The history entry and any fit warnings. Raises Invalid if the draft can't be fitted.
        """

        def apply() -> PublishResult:
            document, _, _ = self._load_draft()
            if document.revision != expected_revision:
                raise Conflict(document.revision)
            if document.revision == self._refresh_published_revision():
                raise Invalid("the draft has no unpublished changes")
            problems, warnings = self.check(document)
            if problems:
                raise Invalid("the draft can't be published", problems)
            entry = self.store.publish(document, note=(note or "").strip() or None)
            self._published_revision = document.revision
            logger.info("published georegistration annotations r%d", document.revision)
            return PublishResult(HistorySummary(entry.id, entry.published_at, document.revision), warnings)

        async with self._lock:
            return await asyncio.to_thread(apply)
