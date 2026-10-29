"""AnnotationService: editing the shared draft, conflicts, and publishing."""

import asyncio
import io
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
from PIL import Image

import pytest

from upslope.annotation_service import AnnotationService, Conflict, Invalid, NotFound, UpstreamError, to_key
from upslope.cli import DEFAULT_MODELS_CONFIG, DEV_GEOREGISTRATION
from upslope.clients.annotations import AnnotationStore, read_snapshot
from upslope.models.uw_wrf.georegistration_document import AnnotationDocument
from upslope.models.config import ModelsConfig

WA = "washington"
IMG = "legacy-hour06"


@pytest.fixture
def store(memory_storage) -> AnnotationStore:
    store = AnnotationStore(memory_storage)
    document = read_snapshot(DEV_GEOREGISTRATION)
    store.publish(document)
    store.save_draft(document, create=True)
    return store


@pytest.fixture
def service(store: AnnotationStore) -> AnnotationService:
    return AnnotationService(store, ModelsConfig.load(DEFAULT_MODELS_CONFIG))


def run(coroutine):
    return asyncio.run(coroutine)


def test_to_key() -> None:
    assert to_key("Cape Flattery, WA") == "cape_flattery_wa"
    assert to_key("  ---  ") == "point"


def test_draft_reports_published_revision(service: AnnotationService) -> None:
    state = run(service.draft())
    assert state.document.revision == state.published_revision == 1


def test_draft_starts_from_published_when_missing(memory_storage) -> None:
    store = AnnotationStore(memory_storage)
    store.publish(read_snapshot(DEV_GEOREGISTRATION))
    service = AnnotationService(store, ModelsConfig([]))
    state, key = run(service.create_point("Mount Rainier", 46.853, -121.760))
    assert key == "mount_rainier" and state.document.revision == 2
    assert store.load_draft()[0].geographic_points[key].name == "Mount Rainier"


def test_create_point_picks_unique_key(service: AnnotationService) -> None:
    _, first = run(service.create_point("Cape Flattery", 48.0, -124.0))
    _, second = run(service.create_point("Cape Flattery", 48.1, -124.1))
    assert (first, second) == ("cape_flattery_2", "cape_flattery_3")


@pytest.mark.parametrize(("name", "lat", "lon"), [("", 1, 1), ("x", 91, 0), ("x", 0, -181), ("x", float("nan"), 0)])
def test_create_point_validates(service: AnnotationService, name: str, lat: float, lon: float) -> None:
    with pytest.raises(Invalid):
        run(service.create_point(name, lat, lon))


def test_update_and_delete_point(service: AnnotationService) -> None:
    state = run(service.update_point("cape_flattery", "Cape Flattery", 48.38, -124.72))
    assert state.document.geographic_points["cape_flattery"].name == "Cape Flattery"
    before = state.document.usage_count("cape_flattery")
    state, removed = run(service.delete_point("cape_flattery"))
    assert removed == before > 0
    assert state.document.usage_count("cape_flattery") == 0
    with pytest.raises(NotFound):
        run(service.delete_point("cape_flattery"))
    with pytest.raises(NotFound):
        run(service.update_point("nope", "x", 0, 0))


def test_put_annotation_upserts_and_replaces(service: AnnotationService) -> None:
    state = run(service.put_annotation(WA, IMG, "cape_flattery", 10, 20))
    assert state.document.maptypes[WA].images[IMG].annotations["cape_flattery"].pixel_x == 10

    # switch the annotation to another landmark in one change
    state = run(service.put_annotation(WA, IMG, "snoqualmie_pass_county_line", 10, 20, replaces="cape_flattery"))
    annotations = state.document.maptypes[WA].images[IMG].annotations
    assert "cape_flattery" not in annotations and "snoqualmie_pass_county_line" in annotations


def test_put_annotation_validates(service: AnnotationService) -> None:
    with pytest.raises(NotFound):
        run(service.put_annotation(WA, "nope", "cape_flattery", 1, 1))
    with pytest.raises(Invalid, match="no point"):
        run(service.put_annotation(WA, IMG, "nope", 1, 1))
    image = run(service.draft()).document.maptypes[WA].images[IMG]
    with pytest.raises(Invalid, match="outside"):
        run(service.put_annotation(WA, IMG, "cape_flattery", image.width, 0))


def test_delete_annotation_and_image(service: AnnotationService) -> None:
    state = run(service.delete_annotation(WA, IMG, "cape_flattery"))
    assert "cape_flattery" not in state.document.maptypes[WA].images[IMG].annotations
    with pytest.raises(NotFound):
        run(service.delete_annotation(WA, IMG, "cape_flattery"))
    state = run(service.delete_image(WA, IMG))
    assert IMG not in state.document.maptypes[WA].images
    with pytest.raises(NotFound):
        run(service.delete_image(WA, IMG))


def test_fit_threshold(service: AnnotationService) -> None:
    assert run(service.set_fit_threshold(WA, 500)).document.maptypes[WA].fit_threshold_m == 500
    assert run(service.set_fit_threshold(WA, None)).document.maptypes[WA].fit_threshold_m is None
    with pytest.raises(Invalid):
        run(service.set_fit_threshold(WA, -1))
    with pytest.raises(NotFound):
        run(service.set_fit_threshold("atlantis", 5))


def test_stale_revision_conflicts(service: AnnotationService) -> None:
    run(service.create_point("A", 1, 1, expected_revision=1))
    with pytest.raises(Conflict) as error:
        run(service.create_point("B", 1, 1, expected_revision=1))
    assert error.value.current_revision == 2


def test_write_from_another_process_conflicts(service: AnnotationService, store: AnnotationStore) -> None:
    original_load = AnnotationStore.load_draft
    calls = []

    def load_then_someone_else_writes(self):
        loaded = original_load(self)
        calls.append(1)
        if len(calls) == 1:
            document, etag = loaded
            other = AnnotationDocument.from_json(document.to_json())
            other.revision = 99
            self.save_draft(other, if_match=etag)
        return loaded

    with patch.object(AnnotationStore, "load_draft", load_then_someone_else_writes), pytest.raises(Conflict) as error:
        run(service.create_point("A", 1, 1))
    assert error.value.current_revision == 99


def test_concurrent_changes_are_serialized(service: AnnotationService) -> None:
    async def many() -> None:
        await asyncio.gather(*(service.create_point(f"P{i}", 1, 1) for i in range(10)))

    run(many())
    state = run(service.draft())
    assert state.document.revision == 11
    assert {f"p{i}" for i in range(10)} <= state.document.geographic_points.keys()


def test_publish_and_history(service: AnnotationService, store: AnnotationStore) -> None:
    with pytest.raises(Invalid, match="no unpublished changes"):
        run(service.publish(1))

    run(service.create_point("Mount Rainier", 46.853, -121.760))
    with pytest.raises(Conflict):
        run(service.publish(1))
    result = run(service.publish(2, note="  add rainier "))
    assert result.history.revision == 2 and result.warnings == []
    assert store.load_published().revision == 2
    assert run(service.draft()).published_revision == 2

    history = run(service.history())
    assert [h.revision for h in history] == [2, 1]
    assert run(service.history_entry(history[0].id)).note == "add rainier"


def test_publish_warns_over_threshold(service: AnnotationService) -> None:
    state = run(service.set_fit_threshold(WA, 1))
    result = run(service.publish(state.document.revision))
    assert len(result.warnings) == 1 and result.warnings[0].startswith("washington: mean fit error")


def test_publish_blocks_unfittable_maptype(service: AnnotationService) -> None:
    for image in ("legacy-hour06", "legacy-hour36"):
        run(service.delete_image(WA, image))
    state = run(service.draft())
    with pytest.raises(Invalid) as error:
        run(service.publish(state.document.revision))
    assert any(problem.startswith("washington:") for problem in error.value.problems)


def test_discard_and_restore(service: AnnotationService) -> None:
    run(service.delete_point("cape_flattery"))
    state = run(service.discard_draft())
    assert "cape_flattery" in state.document.geographic_points
    assert state.document.revision == 3  # revisions keep increasing

    run(service.delete_point("cape_flattery"))
    published = run(service.publish(run(service.draft()).document.revision))
    first = run(service.history())[-1]
    state = run(service.restore(first.id))
    assert "cape_flattery" in state.document.geographic_points
    assert state.published_revision == published.history.revision
    with pytest.raises(NotFound):
        run(service.restore("20200101T000000Z-r1"))


def test_image_lookup(service: AnnotationService, store: AnnotationStore) -> None:
    store.put_image(WA, IMG, b"gif")
    assert b"".join(run(service.image(WA, IMG)).body) == b"gif"
    with pytest.raises(NotFound):
        run(service.image(WA, "../secret"))
    with pytest.raises(NotFound):
        run(service.image(WA, "missing"))


def test_fit_reports_draft(service: AnnotationService) -> None:
    report = run(service.fit())
    assert report[WA].mean_error_m == pytest.approx(779, abs=1)


# --- promoting forecast frames


def gif_bytes(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("P", size).save(buffer, format="GIF")
    return buffer.getvalue()


@pytest.fixture
def wrf_client() -> MagicMock:
    client = MagicMock()
    client.latest_complete_run.return_value = datetime(2026, 10, 21, 12, tzinfo=UTC)
    client.download_image.return_value = (DEV_GEOREGISTRATION / "images/washington/legacy-hour06.gif").read_bytes()
    return client


@pytest.fixture
def promoting(store: AnnotationStore, wrf_client: MagicMock) -> AnnotationService:
    return AnnotationService(store, ModelsConfig.load(DEFAULT_MODELS_CONFIG), wrf_client)


def test_sources_list_configured_layers(service: AnnotationService) -> None:
    sources = {(s.model, s.variable): s for s in service.sources()}
    source = sources[("uwwrf_4km", "surface-temp")]
    assert source.maptype == WA and source.hours


def test_promote_adds_image(promoting: AnnotationService, store: AnnotationStore, wrf_client: MagicMock) -> None:
    source = next(s for s in promoting.sources() if s.maptype == WA)
    hour = source.hours[0]
    result = run(promoting.promote(source.model, source.variable, hour))

    assert result.maptype == WA
    assert result.image == f"uwwrf_4km-{source.variable}-h{hour:02d}-20261021T12Z"
    image = result.draft.document.maptypes[WA].images[result.image]
    assert (image.source.model, image.source.hour, image.annotations) == (source.model, hour, {})
    assert store.read_image(WA, result.image) == wrf_client.download_image.return_value
    # the frame comes from the model's complete run, which it's labeled with
    assert wrf_client.latest_complete_run.call_args.args[0].id == source.model
    assert wrf_client.download_image.call_args.args[1:] == (datetime(2026, 10, 21, 12, tzinfo=UTC), hour)

    # promoting the same frame of the same run again changes nothing
    again = run(promoting.promote(source.model, source.variable, hour))
    assert again.image == result.image and again.draft.document.revision == result.draft.document.revision


def test_promote_validates(promoting: AnnotationService, wrf_client: MagicMock) -> None:
    source = next(s for s in promoting.sources() if s.maptype == WA)
    with pytest.raises(NotFound):
        run(promoting.promote("uwwrf_99km", source.variable, source.hours[0]))
    with pytest.raises(Invalid, match="forecast hours"):
        run(promoting.promote(source.model, source.variable, 7))

    wrf_client.download_image.return_value = gif_bytes((10, 10))
    with pytest.raises(Invalid, match="pixel grid"):
        run(promoting.promote(source.model, source.variable, source.hours[0]))

    wrf_client.download_image.return_value = b"<html>not found</html>"
    with pytest.raises(UpstreamError):
        run(promoting.promote(source.model, source.variable, source.hours[0]))

    wrf_client.download_image.side_effect = httpx.ConnectError("down")
    with pytest.raises(UpstreamError, match="down"):
        run(promoting.promote(source.model, source.variable, source.hours[0]))

    wrf_client.latest_complete_run.return_value = None
    with pytest.raises(UpstreamError, match="no complete run"):
        run(promoting.promote(source.model, source.variable, source.hours[0]))
