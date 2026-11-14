"""One-off migrations of the exported forecasts (`python/migrations/`), against an in-memory bucket."""

import asyncio
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from PIL import Image

from migrations import m001_webp_frames
from migrations.common import Summary, kept_runs
from upslope.clients.forecasts import IMMUTABLE_CACHE_CONTROL, ForecastStore
from upslope.models.config import HourRange, LayerConfig, ModelConfig, ModelsConfig
from upslope.models.manifest import Frame, RunLayer, RunManifest, RunPointer, content_digest

OLDER = datetime(2026, 11, 10, 0, tzinfo=UTC)
LATEST = datetime(2026, 11, 10, 12, tzinfo=UTC)
NOW = datetime(2026, 11, 10, 20, tzinfo=UTC)
MODEL = ModelConfig(
    "uwwrf_4km", "UW WRF 4km", [0, 12],
    [LayerConfig("surface-temp", "Surface temperature", "https://example/{init:%Y%m%d%H}/{hour}.gif", HourRange(last=3), "washington")],
)
CONFIG = ModelsConfig([MODEL])


def paletted_png(seed: int) -> bytes:
    """A frame as the exporter used to write them: paletted, with a see-through nodata index."""
    image = Image.fromarray(np.random.default_rng(seed).integers(0, 4, (6, 8), dtype=np.uint8), mode="P")
    image.putpalette([31, 120, 180, 254, 224, 139, 215, 48, 39, 0, 0, 0] + [0] * (256 - 4) * 3)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", transparency=3)
    return buffer.getvalue()


def rgba(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as image:
        return np.asarray(image.convert("RGBA"))


def write_png_run(store: ForecastStore, initialized: datetime) -> RunManifest:
    frames = []
    for hour in (0, 3):
        valid = initialized + timedelta(hours=hour)
        data = paletted_png(hour)
        path = f"surface-temp/{valid:%Y-%m-%dT%H%M}-{content_digest(data)}.png"
        store.storage.put_bytes(f"{store.run_prefix(MODEL.id, initialized)}/{path}", data, "image/png", cache_control=IMMUTABLE_CACHE_CONTROL)
        frames.append(Frame(hour, valid, path))
    layer = RunLayer("surface-temp", "Surface temperature", "washington", "f1", [0, 3], (8, 6), [(0.0, 0.0)] * 4, frames)
    manifest = RunManifest(MODEL.id, MODEL.name, initialized, 3, NOW, [layer])
    store.write_manifest(manifest)
    return manifest


@pytest.fixture
def store(memory_storage) -> ForecastStore:
    store = ForecastStore(memory_storage)
    for initialized in (OLDER, LATEST):
        write_png_run(store, initialized)
    store.write_latest(RunPointer(MODEL.id, LATEST, "2026/11/10/1200Z", NOW))
    return store


def run_keys(memory_storage, initialized: datetime, suffix: str, model_id: str = MODEL.id) -> list[str]:
    prefix = ForecastStore.run_prefix(model_id, initialized)
    return sorted(key for key in memory_storage.objects if key.startswith(prefix) and key.endswith(suffix))


def migrate(store: ForecastStore, include_followed: bool = False, dry_run: bool = False) -> Summary:
    return m001_webp_frames.migrate(store, kept_runs(store, CONFIG), include_followed, dry_run, workers=2)


def test_kept_runs_marks_the_runs_the_exporter_follows(store: ForecastStore) -> None:
    runs = kept_runs(store, CONFIG)
    assert [(run.initialized, run.followed) for run in runs] == [(LATEST, True), (OLDER, False)]


def test_m001_converts_older_runs_to_identical_webps(store: ForecastStore, memory_storage) -> None:
    before = store.read_manifest(MODEL.id, OLDER)
    pngs = {frame.hour: memory_storage.objects[f"{store.run_prefix(MODEL.id, OLDER)}/{frame.path}"][0] for frame in before.layers[0].frames}

    summary = migrate(store)
    assert (summary.runs, summary.skipped_followed, summary.counts["frames converted"]) == (1, 1, 2)

    after = store.read_manifest(MODEL.id, OLDER)
    assert run_keys(memory_storage, OLDER, ".png") == []
    for frame in after.layers[0].frames:
        key = f"{store.run_prefix(MODEL.id, OLDER)}/{frame.path}"
        data, content_type = memory_storage.objects[key]
        assert (content_type, memory_storage.cache_control[key]) == ("image/webp", IMMUTABLE_CACHE_CONTROL)
        assert frame.path == f"surface-temp/{frame.valid:%Y-%m-%dT%H%M}-{content_digest(data)}.webp"
        old, new = rgba(pngs[frame.hour]), rgba(data)
        np.testing.assert_array_equal(new[..., 3], old[..., 3])
        visible = old[..., 3] > 0
        np.testing.assert_array_equal(new[visible], old[visible])
    # everything else about the run is unchanged
    assert [(f.hour, f.valid) for f in after.layers[0].frames] == [(f.hour, f.valid) for f in before.layers[0].frames]
    # the followed run is left to the exporter
    assert len(run_keys(memory_storage, LATEST, ".png")) == 2


def test_m001_includes_followed_runs_only_when_asked_and_reruns_do_nothing(store: ForecastStore, memory_storage) -> None:
    assert migrate(store, include_followed=True).runs == 2
    assert run_keys(memory_storage, LATEST, ".png") == []
    puts = len(memory_storage.puts)
    assert migrate(store, include_followed=True).runs == 0
    assert len(memory_storage.puts) == puts


def test_m001_dry_run_writes_nothing(store: ForecastStore, memory_storage) -> None:
    objects = dict(memory_storage.objects)
    summary = migrate(store, include_followed=True, dry_run=True)
    assert (summary.runs, summary.counts["frames to convert"]) == (2, 4)
    assert memory_storage.objects == objects


def test_m001_leaves_a_run_that_changes_or_is_pruned_meanwhile(store: ForecastStore, memory_storage, monkeypatch) -> None:
    """The exporter (or a prune) may touch a run between the migration's read and write: the migration loses."""
    objects = dict(memory_storage.objects)
    upload = memory_storage.put_bytes

    def put_bytes(key: str, data: bytes, content_type: str, **kwargs):
        if key.endswith("manifest.json"):
            memory_storage.delete_keys([key])  # pruned meanwhile
        return upload(key, data, content_type, **kwargs)

    monkeypatch.setattr(memory_storage, "put_bytes", put_bytes)
    summary = migrate(store)
    assert (summary.runs, summary.skipped_changed) == (0, 1)
    # no manifest recreated, no WebPs left behind, the PNGs untouched
    objects.pop(f"{store.run_prefix(MODEL.id, OLDER)}/manifest.json")
    assert memory_storage.objects == objects


def test_m001_a_missing_frame_fails_the_run_without_changing_it(store: ForecastStore, memory_storage) -> None:
    (missing,) = [key for key in run_keys(memory_storage, OLDER, ".png") if "T0300" in key]
    memory_storage.delete_keys([missing])
    objects = dict(memory_storage.objects)
    with ThreadPoolExecutor(2) as pool:
        summary = Summary()
        (run,) = [run for run in kept_runs(store, CONFIG) if run.initialized == OLDER]
        with pytest.raises(FileNotFoundError):
            m001_webp_frames.migrate_run(store, run, summary, pool, dry_run=False)
    assert memory_storage.objects == objects


# --- m002: int16 values on the model's grid

from attrs import evolve  # noqa: E402

from migrations import m002_int16_values  # noqa: E402
from tests.test_dynamical import PREVIOUS, RUN, TEMPERATURE, lat_lon_dataset, make_model, make_source  # noqa: E402
from upslope.models.driver import ForecastDriver  # noqa: E402
from upslope.raster import decode_lookup, decode_values  # noqa: E402

DYNAMICAL = make_model(TEMPERATURE)  # stores values as the config now says
OLD = make_model(evolve(TEMPERATURE, values=None))  # exported before values were stored this way
DYNAMICAL_CONFIG = ModelsConfig([DYNAMICAL])


@pytest.fixture
def old_runs(memory_storage) -> ForecastStore:
    """Two runs as the old exporter left them, with RGB PNG value images; the newer one is still followed."""
    store = ForecastStore(memory_storage)
    dataset = lat_lon_dataset({PREVIOUS: 6, RUN: 3})
    driver = ForecastDriver(ModelsConfig([OLD]), {"dynamical": make_source(dataset)}, store, now=lambda: RUN + timedelta(hours=4))
    asyncio.run(driver.run())
    for initialized in (PREVIOUS, RUN):
        key = f"{store.run_prefix(OLD.id, initialized)}/manifest.json"
        data = json.loads(memory_storage.objects[key][0])
        for layer in data["layers"]:
            layer["values"] = {"scale": 0.01, "offset": -100_000.0}
            for frame in layer["frames"]:
                frame["values"] = frame["path"].replace(".webp", ".values.png")
                memory_storage.put_bytes(f"{store.run_prefix(OLD.id, initialized)}/{frame['values']}", b"png", "image/png")
        memory_storage.put_bytes(key, json.dumps(data).encode(), "application/json")
    store.write_latest(RunPointer(OLD.id, RUN, "2026/11/08/0000Z", NOW))
    return store


def strip(store: ForecastStore, include_followed: bool = False, dry_run: bool = False) -> Summary:
    return m002_int16_values.strip(store, kept_runs(store, DYNAMICAL_CONFIG), include_followed, dry_run)


def backfill(store: ForecastStore, config: ModelsConfig = DYNAMICAL_CONFIG, include_followed: bool = False) -> Summary:
    source = make_source(lat_lon_dataset({PREVIOUS: 6, RUN: 3}))
    return m002_int16_values.backfill(store, source, kept_runs(store, config), include_followed, dry_run=False, workers=2)


def test_m002_strip_removes_old_value_images(old_runs: ForecastStore, memory_storage) -> None:
    assert strip(old_runs, dry_run=True).counts == {"value images removed": 3}
    assert len([key for key in memory_storage.objects if key.endswith(".values.png")]) == 5

    summary = strip(old_runs)
    assert (summary.runs, summary.skipped_followed, summary.counts) == (1, 1, {"value images removed": 3})
    manifest = old_runs.read_manifest(OLD.id, PREVIOUS)  # the new format reads it
    assert manifest.layers[0].values is None and all(frame.values is None for frame in manifest.layers[0].frames)
    assert len(run_keys(memory_storage, PREVIOUS, ".values.png", OLD.id)) == 0
    assert len(run_keys(memory_storage, RUN, ".values.png", OLD.id)) == 2  # followed: left alone

    assert strip(old_runs, include_followed=True).runs == 1
    assert strip(old_runs, include_followed=True).runs == 0
    assert not [key for key in memory_storage.objects if key.endswith(".values.png")]


def test_m002_backfill_restores_values_on_the_model_grid(old_runs: ForecastStore, memory_storage) -> None:
    strip(old_runs, include_followed=True)
    summary = backfill(old_runs)
    assert (summary.runs, summary.skipped_followed, summary.counts["frames backfilled"]) == (1, 1, 3)
    assert old_runs.read_manifest(OLD.id, RUN).layers[0].values is None  # followed: left for later

    layer = old_runs.read_manifest(OLD.id, PREVIOUS).layers[0]
    assert layer.fingerprint == DYNAMICAL.layer_fingerprint(TEMPERATURE)  # the exporter now sees it as current
    assert (layer.values.scale, layer.values.offset) == (0.1, 30.0)
    prefix = old_runs.run_prefix(OLD.id, PREVIOUS)
    lookup = decode_lookup(memory_storage.objects[f"{prefix}/{layer.values.lookup}"][0])
    assert lookup.shape[0] == 2 and np.isfinite(lookup).all()
    for frame, expected in zip(layer.frames, [32.0, 86.0, 104.0]):  # 0, 30 and 40 °C at hours 0, 3 and 6
        assert frame.values.endswith(".values.tif")
        values, _, _ = decode_values(memory_storage.objects[f"{prefix}/{frame.values}"][0])
        np.testing.assert_allclose(values, expected)

    assert backfill(old_runs, include_followed=True).counts["frames backfilled"] == 2
    puts = len(memory_storage.puts)
    assert backfill(old_runs, include_followed=True).runs == 0
    assert len(memory_storage.puts) == puts


def test_m002_backfill_leaves_layers_whose_config_changed(old_runs: ForecastStore) -> None:
    strip(old_runs, include_followed=True)
    changed = make_model(evolve(TEMPERATURE, hours=HourRange(last=3)))
    summary = backfill(old_runs, ModelsConfig([changed]), include_followed=True)
    assert summary.runs == 0 and summary.counts["layers left to the exporter"] == 2
    assert old_runs.read_manifest(OLD.id, PREVIOUS).layers[0].values is None
