"""AnnotationStore's bucket layout, the document format, and the dev snapshot."""

from datetime import UTC, datetime

import pytest

from upslope.cli import DEFAULT_WRF_CONFIG, DEV_GEOREGISTRATION
from upslope.clients.annotations import AnnotationStore, read_snapshot
from upslope.clients.storage import PreconditionFailed
from upslope.models.uw_wrf.georegistration_document import (
    AnnotatedImage,
    AnnotationDocument,
    GeographicPoint,
    MaptypeAnnotations,
    PixelAnnotation,
    is_valid_key,
)
from upslope.models.uw_wrf.wrf_client import WrfConfig


def small_document() -> AnnotationDocument:
    return AnnotationDocument(
        revision=3,
        geographic_points={"a": GeographicPoint("A", 47.0, -122.0)},
        maptypes={"washington": MaptypeAnnotations(images={"img": AnnotatedImage(10, 10, annotations={"a": PixelAnnotation(1, 2)})})},
    )


def test_document_json_round_trips() -> None:
    document = small_document()
    assert AnnotationDocument.from_json(document.to_json()) == document


def test_document_rejects_unknown_keys() -> None:
    with pytest.raises(Exception):
        AnnotationDocument.from_json(b'{"revision": 1, "domains": {}}')


def test_document_helpers() -> None:
    document = small_document()
    document.maptypes["washington"].images["img"].annotations["gone"] = PixelAnnotation(3, 4)
    assert document.dangling_references() == ["washington/img/gone"]
    assert document.usage_count("a") == 1
    assert [point.name for _, point in document.control_points("washington")] == ["A"]
    assert document.control_points("atlantis") == []


@pytest.mark.parametrize(("key", "valid"), [("cape_flattery", True), ("legacy-hour06", True), ("a.b", True), ("", False), ("../x", False), ("a/b", False), (".hidden", False), ("a..b", False)])
def test_is_valid_key(key: str, valid: bool) -> None:
    assert is_valid_key(key) is valid


def test_draft_conditional_saves(memory_storage) -> None:
    store = AnnotationStore(memory_storage)
    assert store.load_draft() is None
    etag = store.save_draft(small_document(), create=True)
    with pytest.raises(PreconditionFailed):
        store.save_draft(small_document(), create=True)
    document, loaded_etag = store.load_draft()
    assert document == small_document() and loaded_etag == etag
    store.save_draft(document, if_match=etag)
    with pytest.raises(PreconditionFailed):
        store.save_draft(document, if_match='"stale"')


def test_publish_writes_history_then_published(memory_storage) -> None:
    store = AnnotationStore(memory_storage)
    assert store.load_published() is None
    first = store.publish(small_document(), note="first", published_at=datetime(2026, 10, 21, 13, tzinfo=UTC))
    later = AnnotationDocument(revision=5)
    store.publish(later, published_at=datetime(2026, 10, 22, 9, tzinfo=UTC))

    assert memory_storage.puts[:2] == ["georegistration/history/20261021T130000Z-r3.json", "georegistration/published.json"]
    assert store.load_published() == later
    assert [(s.id, s.revision) for s in store.list_history()] == [("20261022T090000Z-r5", 5), ("20261021T130000Z-r3", 3)]
    entry = store.load_history(first.id)
    assert entry.note == "first" and entry.document == small_document()
    assert store.load_history("nope") is None
    assert store.load_history("20261021T130000Z-r99") is None


def test_images_are_immutable(memory_storage) -> None:
    store = AnnotationStore(memory_storage)
    store.put_image("washington", "img", b"one")
    store.put_image("washington", "img", b"two")
    assert store.read_image("washington", "img") == b"one"
    assert b"".join(store.get_image("washington", "img").body) == b"one"
    assert store.get_image("washington", "nope") is None


def test_dev_snapshot_is_complete() -> None:
    """The repo snapshot loads, references only known points, and covers every configured maptype."""
    document = read_snapshot(DEV_GEOREGISTRATION)
    assert not document.dangling_references()
    maptypes = {layer.maptype for layer in WrfConfig.load(DEFAULT_WRF_CONFIG).layers}
    assert maptypes <= document.maptypes.keys()
    for maptype in maptypes:
        assert len(document.control_points(maptype)) >= 2
        assert document.maptypes[maptype].fit_threshold_m
