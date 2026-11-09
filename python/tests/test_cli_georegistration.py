"""`upslope georegistration upload`, `snapshot` and `promote-examples`, against in-memory storage."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import pytest
from click.testing import CliRunner

from upslope.cli import DEV_GEOREGISTRATION
from upslope.cli.main import main
from upslope.clients.annotations import AnnotationStore, read_snapshot


@pytest.fixture
def store(memory_storage):
    with patch("upslope.cli.georegistration.StorageClient.build", return_value=memory_storage):
        yield AnnotationStore(memory_storage)


def run(*args: str) -> str:
    result = CliRunner().invoke(main, ["georegistration", *map(str, args)])
    assert result.exit_code == 0, result.output
    return result.output


def test_upload_then_snapshot_round_trips(store: AnnotationStore, tmp_path: Path) -> None:
    snapshot = read_snapshot(DEV_GEOREGISTRATION)
    assert "uploaded r1" in run("upload", DEV_GEOREGISTRATION)
    assert store.load_published() == snapshot
    assert store.load_draft()[0] == snapshot
    assert len(store.list_history()) == 1

    run("snapshot", tmp_path)
    assert read_snapshot(tmp_path) == snapshot
    original = DEV_GEOREGISTRATION / "images/washington/legacy-hour06.gif"
    assert (tmp_path / "images/washington/legacy-hour06.gif").read_bytes() == original.read_bytes()


def test_upload_leaves_existing_draft_unless_forced(store: AnnotationStore) -> None:
    run("upload", DEV_GEOREGISTRATION)
    draft, _ = store.load_draft()
    draft.revision = 7
    store.save_draft(draft)

    assert "not uploading" in run("upload", DEV_GEOREGISTRATION)
    assert store.load_draft()[0].revision == 7

    assert "uploaded r8" in run("upload", DEV_GEOREGISTRATION, "--force")
    assert store.load_draft()[0].revision == store.load_published().revision == 8


def test_snapshot_without_published_fails(store: AnnotationStore, tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["georegistration", "snapshot", str(tmp_path)])
    assert result.exit_code != 0 and "no published annotations" in result.output


PNW_HR = "pacific-northwest-hr"


@pytest.fixture
def wrf_client(store: AnnotationStore):
    """A draft missing the pacific-northwest-hr maptype, and a fake UW server to promote its frame from."""
    document = read_snapshot(DEV_GEOREGISTRATION)
    image = next(iter(document.maptypes.pop(PNW_HR).images))
    store.save_draft(document, create=True)
    client = AsyncMock()
    client.latest_complete_run.return_value = datetime(2026, 11, 5, tzinfo=UTC)
    client.download_image.return_value = (DEV_GEOREGISTRATION / f"images/{PNW_HR}/{image}.gif").read_bytes()
    with patch("upslope.annotation_service.ImageClient", return_value=client):
        yield client


def test_promote_examples_adds_missing_maptypes(store: AnnotationStore, wrf_client: MagicMock) -> None:
    output = run("promote-examples", "--dry-run")
    assert f"would promote {PNW_HR}: uwwrf_1.33km/integrated-cloud hour 6" in output
    assert PNW_HR not in store.load_draft()[0].maptypes
    wrf_client.download_image.assert_not_called()

    output = run("promote-examples")
    image = "uwwrf_1.33km-integrated-cloud-h06-20261105T00Z"
    assert f"promoted {PNW_HR}: uwwrf_1.33km/integrated-cloud hour 6 as {image}" in output
    draft = store.load_draft()[0]
    assert list(draft.maptypes[PNW_HR].images) == [image]
    assert store.read_image(PNW_HR, image) == wrf_client.download_image.return_value

    # the new image is waiting to be annotated, so nothing more is promoted
    assert "every configured maptype has an image" in run("promote-examples")
    assert store.load_draft()[0].revision == draft.revision


def test_promote_examples_reports_failures(store: AnnotationStore, wrf_client: MagicMock) -> None:
    wrf_client.download_image.side_effect = httpx.ConnectError("down")
    result = CliRunner().invoke(main, ["georegistration", "promote-examples"])
    assert result.exit_code != 0
    assert f"could not promote an image for {PNW_HR}" in result.output
    assert PNW_HR not in store.load_draft()[0].maptypes
