"""The shared steps of one-off storage migrations (`python/migrations/common.py`), against an in-memory bucket."""

from datetime import UTC, datetime, timedelta

import pytest

from migrations.common import Summary, kept_runs, migrate_runs, rewrite_manifest
from upslope.clients.forecasts import ForecastStore
from upslope.models.config import HourRange, LayerConfig, ModelConfig, ModelsConfig
from upslope.models.manifest import Frame, RunLayer, RunManifest, RunPointer

OLDER = datetime(2026, 11, 10, 0, tzinfo=UTC)
LATEST = datetime(2026, 11, 10, 12, tzinfo=UTC)
NOW = datetime(2026, 11, 10, 20, tzinfo=UTC)
MODEL = ModelConfig(
    "uwwrf_4km", "UW WRF 4km", [0, 12],
    [LayerConfig("surface-temp", "Surface temperature", "https://example/{init:%Y%m%d%H}/{hour}.gif", HourRange(last=3), "washington")],
)
CONFIG = ModelsConfig([MODEL])


def manifest(initialized: datetime, hours: list[int] | None = None) -> RunManifest:
    frames = [Frame(hour, initialized + timedelta(hours=hour), f"surface-temp/{hour}.webp") for hour in (0, 3)]
    layer = RunLayer("surface-temp", "Surface temperature", "washington", "f1", hours or [0, 3], (8, 6), [(0.0, 0.0)] * 4, frames)
    return RunManifest(MODEL.id, MODEL.name, initialized, 3, NOW, [layer])


@pytest.fixture
def store(memory_storage) -> ForecastStore:
    store = ForecastStore(memory_storage)
    for initialized in (OLDER, LATEST):
        store.write_manifest(manifest(initialized))
    store.write_latest(RunPointer(MODEL.id, LATEST, "2026/11/10/1200Z", NOW))
    return store


def test_kept_runs_marks_the_runs_the_exporter_follows(store: ForecastStore) -> None:
    runs = kept_runs(store, CONFIG)
    assert [(run.initialized, run.followed) for run in runs] == [(LATEST, True), (OLDER, False)]


def test_migrate_runs_skips_followed_runs_unless_asked_and_carries_on_past_failures(store: ForecastStore) -> None:
    seen = []

    def migrate(run, summary: Summary) -> None:
        seen.append(run.initialized)
        if run.initialized == LATEST:
            raise RuntimeError("upstream is down")
        summary.runs += 1

    summary = migrate_runs(store, kept_runs(store, CONFIG), migrate, include_followed=False)
    assert (seen, summary.runs, summary.skipped_followed) == ([OLDER], 1, 1)
    summary = migrate_runs(store, kept_runs(store, CONFIG), migrate, include_followed=True)
    assert summary.runs == 1 and len(summary.failed) == 1 and "upstream is down" in summary.failed[0]


def test_rewrite_manifest_loses_to_a_change_or_a_prune(store: ForecastStore, memory_storage) -> None:
    """The exporter (or a prune) may touch a run between a migration's read and write: the migration loses."""
    uploaded = f"{store.run_prefix(MODEL.id, OLDER)}/surface-temp/new.webp"
    migrated, etag = store.read_manifest_versioned(MODEL.id, OLDER)
    migrated.layers[0].frames[0].path = "surface-temp/new.webp"

    store.write_manifest(manifest(OLDER, hours=[0, 3, 6]))  # the exporter writes meanwhile
    memory_storage.put_bytes(uploaded, b"webp", "image/webp")
    assert not rewrite_manifest(store, migrated, etag, [uploaded])
    assert uploaded not in memory_storage.objects  # its uploads are cleaned up
    assert store.read_manifest(MODEL.id, OLDER).layers[0].hours == [0, 3, 6]

    memory_storage.delete_keys([f"{store.run_prefix(MODEL.id, OLDER)}/manifest.json"])  # pruned
    assert not rewrite_manifest(store, migrated, etag, [])
    assert store.read_manifest(MODEL.id, OLDER) is None  # not recreated

    migrated, etag = store.read_manifest_versioned(MODEL.id, LATEST)
    assert rewrite_manifest(store, migrated, etag, [])
