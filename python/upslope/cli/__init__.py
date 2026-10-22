from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from upslope.models.uw_wrf.georegistration_document import AnnotationDocument


# upslope/python/upslope/cli/__init__.py -> repo root
PROJECT_BASE = Path(__file__).resolve().parents[3]
DEFAULT_WRF_CONFIG = PROJECT_BASE / "data/wrf_codes.yaml"
# Annotation snapshot for local development and tests; production reads annotations from storage
DEV_GEOREGISTRATION = PROJECT_BASE / "dev/georegistration"


def load_annotations(snapshot: Path | None, draft: bool = False) -> "AnnotationDocument":
    """Load annotations for a command: from a snapshot directory, or else from storage.

    Args:
        snapshot: Snapshot directory to read instead of storage.
        draft: Read the draft rather than the published document (storage only).

    Returns:
        The document. Raises click.ClickException if there is none.
    """
    import click

    from upslope.clients.annotations import AnnotationStore, read_snapshot
    from upslope.clients.storage import StorageClient

    if snapshot is not None:
        return read_snapshot(snapshot)
    store = AnnotationStore(StorageClient.build())
    if draft:
        loaded = store.load_draft()
        document = None if loaded is None else loaded[0]
    else:
        document = store.load_published()
    if document is None:
        raise click.ClickException(f"no {'draft' if draft else 'published'} annotations in storage")
    return document
