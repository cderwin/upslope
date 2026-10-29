"""Exported runs in storage: the forecasts/ layout, pointers, listing and retention."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from upslope.clients.forecasts import ForecastStore, parse_run_path
from upslope.models.manifest import Frame, RunLayer, RunManifest, RunPointer, frame_name, run_path

MODEL = "uwwrf_4km"
INITIALIZED = datetime(2026, 10, 29, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 29, 6, tzinfo=UTC)


def manifest(initialized: datetime = INITIALIZED, hours: list[int] | None = None) -> RunManifest:
    hours = [0, 3] if hours is None else hours
    frames = [Frame(h, initialized + timedelta(hours=h), f"surface-temp/{frame_name(initialized + timedelta(hours=h))}") for h in hours]
    layer = RunLayer("surface-temp", "Surface temperature", "washington", "f1", [0, 3], (2, 1), [(0.0, 0.0)] * 4, frames)
    return RunManifest(MODEL, "UW WRF 4km", initialized, 3, NOW, [layer])


def export(store: ForecastStore, tmp_path: Path, run: RunManifest) -> None:
    png = tmp_path / "frame.png"
    png.write_bytes(b"png")
    for layer in run.layers:
        for frame in layer.frames:
            store.upload_frame(run.model, run.initialized, frame.path, png)
    store.write_manifest(run)


@pytest.fixture
def store(memory_storage) -> ForecastStore:
    return ForecastStore(memory_storage)


def test_layout_matches_open_meteo_style(store: ForecastStore, memory_storage, tmp_path: Path) -> None:
    export(store, tmp_path, manifest())
    store.write_latest(RunPointer.to(manifest(), NOW))
    assert sorted(memory_storage.objects) == [
        "forecasts/uwwrf_4km/2026/10/29/0000Z/manifest.json",
        "forecasts/uwwrf_4km/2026/10/29/0000Z/surface-temp/2026-10-29T0000.png",
        "forecasts/uwwrf_4km/2026/10/29/0000Z/surface-temp/2026-10-29T0300.png",
        "forecasts/uwwrf_4km/latest.json",
    ]
    assert memory_storage.objects["forecasts/uwwrf_4km/2026/10/29/0000Z/surface-temp/2026-10-29T0300.png"][1] == "image/png"


def test_manifest_and_pointer_round_trip(store: ForecastStore, tmp_path: Path) -> None:
    run = manifest()
    export(store, tmp_path, run)
    assert store.read_manifest(MODEL, INITIALIZED) == run
    assert store.read_manifest(MODEL, INITIALIZED - timedelta(hours=12)) is None

    assert store.read_latest(MODEL) is None
    store.write_latest(RunPointer.to(run, NOW))
    assert store.read_latest(MODEL) == RunPointer(MODEL, INITIALIZED, "2026/10/29/0000Z", NOW)


def test_manifest_json_shape(store: ForecastStore, memory_storage, tmp_path: Path) -> None:
    """The webapp reads this format (webapp/src/forecast/types.ts)."""
    import json

    export(store, tmp_path, manifest())
    data = json.loads(memory_storage.objects["forecasts/uwwrf_4km/2026/10/29/0000Z/manifest.json"][0])
    assert data["model"] == MODEL and data["initialized"] == "2026-10-29T00:00:00+00:00"
    layer = data["layers"][0]
    assert {"id", "name", "maptype", "fingerprint", "hours", "dimensions", "coordinates", "frames"} <= layer.keys()
    assert layer["frames"][1] == {"hour": 3, "valid": "2026-10-29T03:00:00+00:00", "path": "surface-temp/2026-10-29T0300.png"}


def test_run_paths() -> None:
    assert run_path(datetime(2026, 1, 2, 12, tzinfo=UTC)) == "2026/01/02/1200Z"
    assert parse_run_path("2026/01/02/1200Z") == datetime(2026, 1, 2, 12, tzinfo=UTC)
    for path in ("2026/01/02", "2026/01/02/1200", "2026/13/02/1200Z", "latest.json", "x/2026/01/02/1200Z"):
        assert parse_run_path(path) is None


def test_list_runs_newest_first(store: ForecastStore, tmp_path: Path) -> None:
    older = INITIALIZED - timedelta(hours=12)
    export(store, tmp_path, manifest(older))
    export(store, tmp_path, manifest(INITIALIZED))
    store.write_latest(RunPointer.to(manifest(), NOW))
    # frames without a manifest (an export in progress or interrupted) aren't a run
    store.upload_frame(MODEL, INITIALIZED + timedelta(hours=12), "surface-temp/x.png", tmp_path / "frame.png")
    assert store.list_runs(MODEL) == [INITIALIZED, older]
    assert store.list_runs("other") == []


def test_prune_deletes_runs_past_retention(store: ForecastStore, memory_storage, tmp_path: Path) -> None:
    runs = [INITIALIZED - timedelta(hours=12 * i) for i in range(20)]
    for run in runs:
        export(store, tmp_path, manifest(run))
    store.write_latest(RunPointer.to(manifest(), NOW))

    deleted = store.prune(MODEL, retention_days=7, now=NOW)
    cutoff = NOW - timedelta(days=7)
    assert deleted == sorted(run for run in runs if run < cutoff)
    assert store.list_runs(MODEL) == [run for run in runs if run >= cutoff]
    assert "forecasts/uwwrf_4km/latest.json" in memory_storage.objects
    assert not any(run_path(run) in key for run in deleted for key in memory_storage.objects)


def test_prune_keeps_protected_runs(store: ForecastStore, tmp_path: Path) -> None:
    """A model that stopped publishing keeps its latest run, however old."""
    stale = INITIALIZED - timedelta(days=30)
    export(store, tmp_path, manifest(stale))
    assert store.prune(MODEL, retention_days=7, now=NOW, keep={stale}) == []
    assert store.list_runs(MODEL) == [stale]


def test_prune_leaves_other_models(store: ForecastStore, memory_storage, tmp_path: Path) -> None:
    old = INITIALIZED - timedelta(days=30)
    other = manifest(old)
    other.model = "uwwrf_4km2"
    export(store, tmp_path, other)
    store.prune(MODEL, retention_days=7, now=NOW)
    assert store.list_runs("uwwrf_4km2") == [old]


def test_delete_legacy_only_touches_old_prefixes(store: ForecastStore, memory_storage, tmp_path: Path) -> None:
    for key in (
        "web_exports/20261021T130000/manifest.json",
        "web_exports/20261021T130000/uwwrf_4km/surface-temp/hour_06.png",
        "model_runs/uwwrf/20261021T1200Z.json",
        "georegistration/published.json",
        "georegistration/history/x.json",
        "georegistration/images/washington/a.gif",
    ):
        memory_storage.put_bytes(key, b"x", "application/octet-stream")
    export(store, tmp_path, manifest())

    assert store.delete_legacy() == 3
    assert not any(key.startswith(("web_exports/", "model_runs/")) for key in memory_storage.objects)
    assert sum(key.startswith("georegistration/") for key in memory_storage.objects) == 3
    assert store.list_runs(MODEL) == [INITIALIZED]
