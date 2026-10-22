"""`upslope georegistration upload` and `snapshot`, against in-memory storage."""

from pathlib import Path
from unittest.mock import patch

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
