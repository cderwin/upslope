"""Georegistration annotations in storage, and the on-disk snapshot format.

Bucket layout, under `georegistration/`:

    draft.json                              the document the admin API edits
    published.json                          the document the WRF export reads
    history/<YYYYMMDDTHHMMSS>Z-r<rev>.json  every published version; never deleted
    images/<maptype>/<image_id>.gif         annotated images; never changed or deleted

A snapshot directory (e.g. the repo's `dev/georegistration/`) holds a published document as
`document.json` plus its images at `images/<maptype>/<image_id>.gif`.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from attrs import define

from upslope.clients.storage import PreconditionFailed, StorageClient, StoredObject
from upslope.models.uw_wrf.georegistration_document import AnnotationDocument, converter

PREFIX = "georegistration"
DRAFT_KEY = f"{PREFIX}/draft.json"
PUBLISHED_KEY = f"{PREFIX}/published.json"
HISTORY_PREFIX = f"{PREFIX}/history/"
IMAGES_PREFIX = f"{PREFIX}/images"

JSON = "application/json"
IMAGE_CONTENT_TYPE = "image/gif"


def image_key(maptype: str, image_id: str) -> str:
    return f"{IMAGES_PREFIX}/{maptype}/{image_id}.gif"


@define
class HistoryEntry:
    """A published version, as recorded in history.

    Attributes:
        id: History ID, e.g. `20261021T130000Z-r40`.
        published_at: When it was published.
        note: Optional note given at publish time.
        document: The published document.
    """

    id: str
    published_at: datetime
    note: str | None
    document: AnnotationDocument


@define
class HistorySummary:
    """A history entry without its document, as listed.

    Attributes:
        id: History ID.
        published_at: When it was published.
        revision: Revision of the published document.
    """

    id: str
    published_at: datetime
    revision: int


def _history_id(published_at: datetime, revision: int) -> str:
    return f"{published_at.astimezone(UTC):%Y%m%dT%H%M%S}Z-r{revision}"


def _parse_history_id(history_id: str) -> HistorySummary | None:
    try:
        stamp, revision = history_id.split("-r")
        published_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        return HistorySummary(id=history_id, published_at=published_at, revision=int(revision))
    except ValueError:
        return None


@define
class AnnotationStore:
    """Reads and writes the annotation documents and images in storage.

    Attributes:
        storage: Storage client for the bucket.
    """

    storage: StorageClient

    def load_draft(self) -> tuple[AnnotationDocument, str | None] | None:
        """Load the draft and its ETag (for a conditional save), or None if there is no draft."""
        stored = self.storage.get_bytes(DRAFT_KEY)
        if stored is None:
            return None
        return AnnotationDocument.from_json(stored.data), stored.etag

    def save_draft(self, document: AnnotationDocument, if_match: str | None = None, create: bool = False) -> str | None:
        """Save the draft.

        Args:
            document: Draft to save.
            if_match: Only save if the stored draft still has this ETag.
            create: Only save if there is no draft yet.

        Returns:
            The new ETag. Raises PreconditionFailed if a condition does not hold.
        """
        return self.storage.put_bytes(DRAFT_KEY, document.to_json(), JSON, if_match=if_match, if_none_match=create)

    def load_published(self) -> AnnotationDocument | None:
        """Load the published document, or None if nothing has been published."""
        stored = self.storage.get_bytes(PUBLISHED_KEY)
        return None if stored is None else AnnotationDocument.from_json(stored.data)

    def publish(self, document: AnnotationDocument, note: str | None = None, published_at: datetime | None = None) -> HistoryEntry:
        """Record a document in history, then make it the published document.

        History is written first, so every published version has a history entry.

        Args:
            document: Document to publish.
            note: Optional note to keep with the history entry.
            published_at: Publish time; defaults to now.

        Returns:
            The history entry.
        """
        published_at = published_at or datetime.now(UTC)
        entry = HistoryEntry(
            id=_history_id(published_at, document.revision), published_at=published_at, note=note, document=document
        )
        body = converter.dumps(entry, indent=2).encode()
        self.storage.put_bytes(f"{HISTORY_PREFIX}{entry.id}.json", body, JSON)
        self.storage.put_bytes(PUBLISHED_KEY, document.to_json(), JSON)
        return entry

    def list_history(self) -> list[HistorySummary]:
        """List published versions, newest first."""
        summaries = []
        for key in self.storage.list_keys(HISTORY_PREFIX):
            summary = _parse_history_id(key.removeprefix(HISTORY_PREFIX).removesuffix(".json"))
            if summary is not None:
                summaries.append(summary)
        return sorted(summaries, key=lambda s: (s.published_at, s.revision), reverse=True)

    def load_history(self, history_id: str) -> HistoryEntry | None:
        """Load one published version, or None if the ID is unknown."""
        if _parse_history_id(history_id) is None:
            return None
        stored = self.storage.get_bytes(f"{HISTORY_PREFIX}{history_id}.json")
        return None if stored is None else converter.loads(stored.data, HistoryEntry)

    def put_image(self, maptype: str, image_id: str, data: bytes) -> None:
        """Store an image. Images are immutable, so an existing one is left as is."""
        try:
            self.storage.put_bytes(image_key(maptype, image_id), data, IMAGE_CONTENT_TYPE, if_none_match=True)
        except PreconditionFailed:
            pass

    def get_image(self, maptype: str, image_id: str, if_none_match: str | None = None) -> StoredObject | None:
        """Fetch an image for streaming, or None if it does not exist."""
        return self.storage.get_object(image_key(maptype, image_id), if_none_match)

    def read_image(self, maptype: str, image_id: str) -> bytes | None:
        """Read an image fully into memory, or None if it does not exist."""
        stored = self.storage.get_bytes(image_key(maptype, image_id))
        return None if stored is None else stored.data


# --- snapshots on disk


def snapshot_image_path(directory: Path, maptype: str, image_id: str) -> Path:
    return directory / "images" / maptype / f"{image_id}.gif"


def read_snapshot(directory: Path) -> AnnotationDocument:
    """Read a snapshot's document, checking that every image it lists is present.

    Args:
        directory: Snapshot directory.

    Returns:
        The document. Raises FileNotFoundError if the document or an image is missing.
    """
    document = AnnotationDocument.from_json((directory / "document.json").read_bytes())
    for maptype, image_id in iter_images(document):
        path = snapshot_image_path(directory, maptype, image_id)
        if not path.is_file():
            raise FileNotFoundError(f"snapshot image missing: {path}")
    return document


def iter_images(document: AnnotationDocument) -> Iterator[tuple[str, str]]:
    """Yield every (maptype, image_id) in a document."""
    for maptype, annotations in document.maptypes.items():
        for image_id in annotations.images:
            yield maptype, image_id
