"""Web export pipeline: config, georegistration and frame conversion, using the repo's real configs and sample GIFs."""

import asyncio
import io
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pytest
import rasterio
from affine import Affine
from attrs import evolve
from click.testing import CliRunner
from PIL import Image

from upslope.cli import DEFAULT_MODELS_CONFIG, DEV_GEOREGISTRATION
from upslope.clients.annotations import AnnotationStore, read_snapshot, snapshot_image_path
from upslope.clients.forecasts import IMMUTABLE_CACHE_CONTROL, INDEX, ForecastStore
from upslope.cli.main import main
from upslope.models.config import ConfigError, DynamicalLayerConfig, DynamicalSourceConfig, FieldConfig, HourRange, LayerConfig, ModelConfig, ModelsConfig
from upslope.models.driver import CheckReport, ForecastDriver
from upslope.models.manifest import ForecastIndex, Legend, content_digest, LegendStop, ModelIndex, RunManifest, RunPointer, ValueEncoding
from upslope.models.source import NotPublished, RenderedFrame, SourceUnavailable
from upslope.models.uw_wrf.image_client import ImageClient, Throttle
from upslope.models.uw_wrf.source import UwWrfSource
from upslope.models.uw_wrf.georegistration import Registration, fit_report
from upslope.raster import OUTPUT_CRS, decode_values_png, write_geotiff
from upslope.models.uw_wrf.georegistration_document import AnnotationDocument

SAMPLE_GIF = snapshot_image_path(DEV_GEOREGISTRATION, "washington", "legacy-hour06")
INITIALIZED = datetime(2026, 10, 21, 12, tzinfo=UTC)


def check(driver: ForecastDriver, force: bool = False) -> CheckReport:
    """Run one export check."""
    return asyncio.run(driver.run(force=force))


def export(driver: ForecastDriver, force: bool = False) -> list[RunManifest]:
    """Run one export check that's expected to succeed for every model; the manifests it wrote."""
    report = check(driver, force)
    assert not report.failed, report
    return report.manifests


@pytest.fixture(scope="module")
def document() -> AnnotationDocument:
    return read_snapshot(DEV_GEOREGISTRATION)


@pytest.fixture(scope="module")
def registration(document: AnnotationDocument) -> Registration:
    return Registration.build(document)


# --- config

UW = "https://a.atmos.washington.edu/wrfrt/data/{init:%Y%m%d%H}/images_d3"


def make_layer(layer_id: str = "surface-temp", code: str = "wa_tsfc", hours: HourRange = HourRange(last=120), maptype: str = "washington") -> LayerConfig:
    return LayerConfig(id=layer_id, name=layer_id.title(), url=f"{UW}/{code}.{{hour:02d}}.0000.gif", hours=hours, maptype=maptype)


def make_model(*layers: LayerConfig, model_id: str = "uwwrf_4km") -> ModelConfig:
    return ModelConfig(id=model_id, name="UW WRF 4km", start_times=[0, 12], layers=list(layers) or [make_layer()])


def test_configured_models_load() -> None:
    """Catches mistakes in models.yaml: loading validates ids, templates and hours."""
    config = ModelsConfig.load(DEFAULT_MODELS_CONFIG)
    assert config.models and all(model.layers for model in config.models)


def test_configured_ids_match_existing_map_urls() -> None:
    """Map links (/map/<model>/<layer>) and stored annotation sources use these ids; renaming one breaks them."""
    model = ModelsConfig.load(DEFAULT_MODELS_CONFIG).model("uwwrf_4km")
    assert model is not None
    assert {layer.id for layer in model.layers} >= {
        "surface-temp", "10m-wind-speed", "integrated-cloud", "longwave", "3-hour-precip", "snow", "model-snow",
        "accumulated-snowfall", "freezing-level-terrain", "freezing-level", "snow-depth",
    }


def test_every_configured_maptype_has_a_transform(registration: Registration) -> None:
    missing = ModelsConfig.load(DEFAULT_MODELS_CONFIG).maptypes - registration.domain_transforms.keys()
    assert not missing


def test_image_url() -> None:
    layer = make_layer("3-hour-precip", "wa_pcp3", HourRange(first=6, last=120))
    assert layer.image_url(INITIALIZED, 9) == "https://a.atmos.washington.edu/wrfrt/data/2026102112/images_d3/wa_pcp3.09.0000.gif"
    assert layer.image_url(INITIALIZED, 120).endswith("/wa_pcp3.120.0000.gif")
    # converted to UTC: 5am PDT on the 21st is 12Z
    local = INITIALIZED.astimezone(timezone(timedelta(hours=-7)))
    assert layer.image_url(local, 9) == layer.image_url(INITIALIZED, 9)


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (HourRange(last=9), [0, 3, 6, 9]),
        (HourRange(first=6, last=12), [6, 9, 12]),
        (HourRange(first=24, last=180, step=12), [24, 36, 48, 60, 72, 84, 96, 108, 120, 132, 144, 156, 168, 180]),
        (HourRange(last=10, step=4), [0, 4, 8]),
    ],
)
def test_hour_range(hours: HourRange, expected: list[int]) -> None:
    assert hours.hours == expected


@pytest.mark.parametrize(
    "build",
    [
        lambda: HourRange(first=6, last=3),
        lambda: HourRange(last=6, step=0),
        lambda: LayerConfig(id="a", name="A", url="https://x/{init:%Y}/{hr}.gif", hours=HourRange(last=3), maptype="w"),
        lambda: LayerConfig(id="a", name="A", url="https://x/{init:%Y}.gif", hours=HourRange(last=3), maptype="w"),
        lambda: LayerConfig(id="a", name="A", url="https://x/{init:%Y}/{hour:02d}/{extra}.gif", hours=HourRange(last=3), maptype="w"),
        lambda: LayerConfig(id="a", name="A", url="https://x/{init:%Y}/{hour:02d.gif", hours=HourRange(last=3), maptype="w"),
        lambda: LayerConfig(id="a", name="A", url="https://x/{init}/{hour:%Y}.gif", hours=HourRange(last=3), maptype="w"),
        lambda: LayerConfig(id="Bad Id", name="A", url="https://x/{init}/{hour}.gif", hours=HourRange(last=3), maptype="w"),
        lambda: LayerConfig(id="a", name="A", url="https://x/{init}/{hour}.gif", hours=HourRange(last=3), maptype="../w"),
        lambda: ModelConfig(id="m", name="M", start_times=[24], layers=[make_layer()]),
        lambda: ModelConfig(id="m", name="M", start_times=[], layers=[make_layer()]),
        lambda: ModelConfig(id="m", name="M", start_times=[0], layers=[]),
        lambda: ModelConfig(id="m", name="M", start_times=[0], layers=[make_layer(), make_layer()]),
        lambda: ModelsConfig([make_model(), make_model()]),
    ],
)
def test_invalid_config_raises(build) -> None:
    with pytest.raises(ConfigError):
        build()


def test_load_reports_the_problem_and_where(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        "models:\n"
        "  - {id: m, name: M, start_times: [0], layers: [{id: a, name: A, url: 'https://x/{init:%Y}/{hr}.gif', hours: {last: 3}, maptype: w}]}\n"
    )
    with pytest.raises(ConfigError, match=r"\['hr', 'init'\].*models\[0\]\.layers\[0\]"):
        ModelsConfig.load(path)
    path.write_text("models:\n  - {id: m, name: M, start_times: [0], layers: [], typo: 1}\n")
    with pytest.raises(ConfigError):
        ModelsConfig.load(path)


@pytest.mark.parametrize(
    ("now", "newest"),
    [
        (datetime(2026, 10, 29, 5, 33, tzinfo=UTC), datetime(2026, 10, 29, 0, tzinfo=UTC)),
        (datetime(2026, 10, 29, 12, 0, tzinfo=UTC), datetime(2026, 10, 29, 12, tzinfo=UTC)),
        (datetime(2026, 10, 29, 11, 59, tzinfo=UTC), datetime(2026, 10, 29, 0, tzinfo=UTC)),
        # 10pm PDT is 05Z the next day
        (datetime(2026, 10, 28, 22, 0, tzinfo=timezone(timedelta(hours=-7))), datetime(2026, 10, 29, 0, tzinfo=UTC)),
    ],
)
def test_initializations(now: datetime, newest: datetime) -> None:
    runs = make_model().initializations(now)
    assert runs[:3] == [newest, newest - timedelta(hours=12), newest - timedelta(hours=24)]
    # 48 hours of lookback
    assert all(now - timedelta(hours=48) < run <= now for run in runs) and len(runs) == 4


def test_initializations_follow_start_times() -> None:
    model = ModelConfig(id="m", name="M", start_times=[6, 18, 0, 12], layers=[make_layer()], lookback_hours=14)
    assert model.initializations(datetime(2026, 10, 29, 7, tzinfo=UTC)) == [
        datetime(2026, 10, 29, 6, tzinfo=UTC), datetime(2026, 10, 29, 0, tzinfo=UTC), datetime(2026, 10, 28, 18, tzinfo=UTC),
    ]


def test_configured_fingerprints_are_stable() -> None:
    """A changed fingerprint re-exports every retained run of the model, so config refactors must keep them."""
    config = ModelsConfig.load(DEFAULT_MODELS_CONFIG)
    assert config.model("uwwrf_4km").fingerprint() == "e1359839afb6d730"
    assert config.model("uwwrf_1.33km").fingerprint() == "4ac7afb08cb8b50b"


def test_model_source_defaults_to_uw_wrf(tmp_path: Path) -> None:
    assert make_model().source.kind == "uw_wrf"
    assert {model.source.kind for model in ModelsConfig.load(DEFAULT_MODELS_CONFIG).models} == {"uw_wrf", "dynamical"}
    path = tmp_path / "models.yaml"
    path.write_text(
        "models:\n"
        "  - {id: m, name: M, start_times: [0], source: {kind: elsewhere},"
        " layers: [{id: a, name: A, url: 'https://x/{init:%Y}/{hour}.gif', hours: {last: 3}, maptype: w}]}\n"
    )
    with pytest.raises(ConfigError, match="source"):
        ModelsConfig.load(path)


def test_fingerprint_tracks_what_the_export_produces() -> None:
    base = make_model(make_layer(), make_layer("3-hour-precip", "wa_pcp3", HourRange(first=6, last=84)))
    assert make_model(*base.layers).fingerprint() == base.fingerprint()
    # display names don't change the frames
    renamed = evolve(base.layers[0], name="Something else")
    assert make_model(renamed, base.layers[1]).fingerprint() == base.fingerprint()

    changed = [
        make_model(base.layers[0]),
        make_model(*base.layers, make_layer("other")),
        make_model(base.layers[1], base.layers[0]),
        make_model(evolve(base.layers[0], hours=HourRange(last=84)), base.layers[1]),
        make_model(evolve(base.layers[0], url=base.layers[0].url.replace("wa_", "")), base.layers[1]),
        make_model(evolve(base.layers[0], maptype="pacific-northwest"), base.layers[1]),
        make_model(evolve(base.layers[0], id="renamed"), base.layers[1]),
    ]
    assert base.fingerprint() not in {model.fingerprint() for model in changed}


# --- georegistration and frame export


def test_georegister_writes_web_mercator_geotiff(registration: Registration, tmp_path: Path) -> None:
    out = tmp_path / "hour06.tiff"
    registration.georegister(SAMPLE_GIF, out, "washington")
    with rasterio.open(out) as dataset:
        assert dataset.crs == OUTPUT_CRS
        assert dataset.nodata is not None
        assert dataset.colormap(1)  # GIF palette kept


def test_georegister_unknown_domain_raises(registration: Registration, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        registration.georegister(SAMPLE_GIF, tmp_path / "out.tiff", "atlantis")


def test_export_frame_uploads_a_placed_png(memory_storage, document: AnnotationDocument, tmp_path: Path) -> None:
    """Downloaded GIF -> transparent paletted PNG named by valid time, in a layer placed over Washington."""
    layer = make_layer(hours=HourRange(first=6, last=6))
    model = make_model(layer)
    client = AsyncMock()
    client.download_image.return_value = SAMPLE_GIF.read_bytes()
    annotations = AnnotationStore(memory_storage)
    annotations.publish(document)
    source = UwWrfSource(client, annotations)
    driver = ForecastDriver(config=ModelsConfig([model]), sources={"uw_wrf": source}, store=ForecastStore(memory_storage))

    run_layer = driver.new_layer(model, layer)

    async def scenario():
        async with source.check():
            return await driver.export_frame(model, source, layer, run_layer, INITIALIZED, 6, tmp_path)

    frame = asyncio.run(scenario())

    assert (frame.hour, frame.valid) == (6, INITIALIZED + timedelta(hours=6))
    assert run_layer.frames == [frame]
    for lon, lat in run_layer.coordinates:
        assert -130 < lon < -110 and 40 < lat < 52
    key = f"forecasts/uwwrf_4km/2026/10/21/1200Z/{frame.path}"
    data, content_type = memory_storage.objects[key]
    # named by valid time and content, so it can be cached for good
    assert frame.path == f"surface-temp/2026-10-21T1800-{content_digest(data)}.png"
    assert content_type == "image/png" and memory_storage.cache_control[key] == IMMUTABLE_CACHE_CONTROL
    with Image.open(io.BytesIO(data)) as png:
        assert png.mode == "P"
        assert "transparency" in png.info
        assert png.size == tuple(run_layer.dimensions)
    assert list(tmp_path.rglob("*.*")) == []  # scratch files are removed as it goes


# --- model run detection

RUN_MODEL = make_model(
    make_layer("surface-temp", "wa_tsfc", HourRange(last=120)),
    make_layer("3-hour-precip", "wa_pcp3", HourRange(first=6, last=84)),
)


def uw_client(*published: str, status: int = 404) -> ImageClient:
    """An ImageClient whose requests are answered locally: `published` paths (under `data/`) exist, others get `status`."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/wrfrt/data/")
        return httpx.Response(200 if path in published else status)

    return mock_client(handler)


def mock_client(handler, **kwargs) -> ImageClient:
    """An ImageClient answered by `handler`, with no throttle spacing and no real waiting between retries."""
    return ImageClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), throttle=Throttle(interval=0, sleep=no_sleep), sleep=no_sleep, **kwargs)


async def no_sleep(seconds: float) -> None:
    pass


def last_hours(run: str) -> list[str]:
    """Paths of the last hour of each RUN_MODEL layer in `run` (YYYYMMDDHH)."""
    return [f"{run}/images_d3/wa_tsfc.120.0000.gif", f"{run}/images_d3/wa_pcp3.84.0000.gif"]


def test_uw_source_maps_upstream_responses(memory_storage, document: AnnotationDocument, tmp_path: Path) -> None:
    """A 404 is an hour not published yet; other errors propagate, to be recorded and retried."""
    annotations = AnnotationStore(memory_storage)
    layer = RUN_MODEL.layers[0]
    out = tmp_path / "frame.tiff"

    async def render(source: UwWrfSource) -> None:
        async with source.check():
            assert source.revision(RUN_MODEL) == document.revision
            await source.render_frame(RUN_MODEL, layer, INITIALIZED, 0, out)

    with pytest.raises(SourceUnavailable):
        asyncio.run(render(UwWrfSource(uw_client(), annotations)))
    annotations.publish(document)
    with pytest.raises(NotPublished):
        asyncio.run(render(UwWrfSource(uw_client(), annotations)))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(render(UwWrfSource(uw_client(status=503), annotations)))
    assert list(tmp_path.iterdir()) == []


def test_latest_complete_run_skips_a_run_still_being_published() -> None:
    """Mid-run, the new run's directory has early hours only; the previous run is complete and is used."""
    now = datetime(2026, 10, 29, 5, 0, tzinfo=UTC)
    partial = "2026102900/images_d3/wa_tsfc.06.0000.gif"
    client = uw_client(partial, *last_hours("2026102812"))
    assert asyncio.run(client.latest_complete_run(RUN_MODEL, now)) == datetime(2026, 10, 28, 12, tzinfo=UTC)

    # every layer's last hour is needed, not just one
    client = uw_client(last_hours("2026102900")[0], *last_hours("2026102812"))
    assert asyncio.run(client.latest_complete_run(RUN_MODEL, now)) == datetime(2026, 10, 28, 12, tzinfo=UTC)

    client = uw_client(*last_hours("2026102900"), *last_hours("2026102812"))
    assert asyncio.run(client.latest_complete_run(RUN_MODEL, now)) == datetime(2026, 10, 29, 0, tzinfo=UTC)


def test_latest_complete_run_none_without_a_complete_run() -> None:
    assert asyncio.run(uw_client().latest_complete_run(RUN_MODEL, datetime(2026, 10, 29, 5, 0, tzinfo=UTC))) is None


def test_latest_complete_run_raises_on_upstream_errors() -> None:
    """Only a 404 means "not published yet"; an outage must not be mistaken for an incomplete run."""
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(uw_client(status=503).latest_complete_run(RUN_MODEL, datetime(2026, 10, 29, 5, 0, tzinfo=UTC)))


def test_download_layer_reads_the_runs_directory(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, content=b"gif")

    client = mock_client(handler)
    layer = make_layer("3-hour-precip", "wa_pcp3", HourRange(first=6, last=12))
    asyncio.run(client.download_layer(layer, datetime(2026, 10, 28, 12, tzinfo=UTC), tmp_path / "3-hour-precip"))
    assert requested == [
        "/wrfrt/data/2026102812/images_d3/wa_pcp3.06.0000.gif",
        "/wrfrt/data/2026102812/images_d3/wa_pcp3.09.0000.gif",
        "/wrfrt/data/2026102812/images_d3/wa_pcp3.12.0000.gif",
    ]
    assert (tmp_path / "3-hour-precip/hour12.gif").read_bytes() == b"gif"

    # files already on disk aren't downloaded again
    requested.clear()
    asyncio.run(client.download_layer(layer, datetime(2026, 10, 28, 12, tzinfo=UTC), tmp_path / "3-hour-precip"))
    assert requested == []


# --- driver


class FakeUpstream:
    """Serves the sample GIF for every image of `available` runs, and of `partial` runs up to an hour.

    Records the downloads. Requests whose path contains `fail` time out; `status` overrides the response.
    """

    def __init__(
        self, *available: datetime, partial: dict[datetime, int] | None = None, fail: str | None = None, status: int | None = None
    ) -> None:
        self.published = {f"{run:%Y%m%d%H}": 999 for run in available} | {
            f"{run:%Y%m%d%H}": hour for run, hour in (partial or {}).items()
        }
        self.fail = fail
        self.status = status
        self.downloads: list[str] = []
        self.gif = SAMPLE_GIF.read_bytes()

    def publish(self, run: datetime, through: int) -> None:
        self.published[f"{run:%Y%m%d%H}"] = through

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/wrfrt/data/")
        if self.fail and self.fail in path:
            if self.status:
                return httpx.Response(self.status)
            if request.method == "GET":
                raise httpx.ConnectTimeout("timed out")
        run, _, name = path.partition("/")
        hour = int(name.split(".")[-3])
        if hour > self.published.get(run, -1):
            return httpx.Response(404)
        if request.method == "GET":
            self.downloads.append(path)
        return httpx.Response(200, content=self.gif)

    def client(self) -> ImageClient:
        return mock_client(self.handler)


EXPORT_MODEL = make_model(
    make_layer("surface-temp", "wa_tsfc", HourRange(last=3)),
    make_layer("3-hour-precip", "wa_pcp3", HourRange(first=3, last=6)),
)
NOW = INITIALIZED + timedelta(hours=18)


@pytest.fixture
def published(memory_storage, document: AnnotationDocument) -> AnnotationStore:
    store = AnnotationStore(memory_storage)
    store.publish(document)
    return store


def make_export_driver(
    memory_storage, upstream: FakeUpstream, *models: ModelConfig, now: datetime = NOW
) -> ForecastDriver:
    return ForecastDriver(
        config=ModelsConfig(list(models) or [EXPORT_MODEL]),
        sources={"uw_wrf": UwWrfSource(upstream.client(), AnnotationStore(memory_storage))},
        store=ForecastStore(memory_storage),
        now=lambda: now,
    )


def frame_keys(memory_storage) -> list[str]:
    """Frame keys, without their content digests."""
    keys = (key for key in memory_storage.objects if key.startswith("forecasts/") and key.endswith(".png"))
    return sorted(re.sub(r"-[0-9a-f]{16}(?=\.)", "", key) for key in keys)


def test_run_exports_the_newest_complete_run(memory_storage, published, document) -> None:
    upstream = FakeUpstream(INITIALIZED, INITIALIZED - timedelta(hours=12))
    driver = make_export_driver(memory_storage, upstream)
    (manifest,) = export(driver)

    assert (manifest.model, manifest.name, manifest.initialized) == ("uwwrf_4km", "UW WRF 4km", INITIALIZED)
    assert manifest.registration_revision == document.revision and manifest.complete
    assert [(layer.id, [frame.hour for frame in layer.frames]) for layer in manifest.layers] == [
        ("surface-temp", [0, 3]), ("3-hour-precip", [3, 6]),
    ]
    assert frame_keys(memory_storage) == [
        "forecasts/uwwrf_4km/2026/10/21/1200Z/3-hour-precip/2026-10-21T1500.png",
        "forecasts/uwwrf_4km/2026/10/21/1200Z/3-hour-precip/2026-10-21T1800.png",
        "forecasts/uwwrf_4km/2026/10/21/1200Z/surface-temp/2026-10-21T1200.png",
        "forecasts/uwwrf_4km/2026/10/21/1200Z/surface-temp/2026-10-21T1500.png",
    ]
    store = ForecastStore(memory_storage)
    assert store.read_manifest("uwwrf_4km", INITIALIZED) == manifest
    assert store.read_latest("uwwrf_4km") == RunPointer("uwwrf_4km", INITIALIZED, "2026/10/21/1200Z", NOW)


def test_run_skips_a_run_exported_with_current_annotations_and_config(memory_storage, published) -> None:
    upstream = FakeUpstream(INITIALIZED)
    driver = make_export_driver(memory_storage, upstream)
    export(driver)
    upstream.downloads.clear()
    puts = len(memory_storage.puts)

    assert export(driver) == []
    assert upstream.downloads == []
    # only the record of the check itself
    assert memory_storage.puts[puts:] == ["forecasts/uwwrf_4km/in-progress.json"]


def test_run_reexports_every_layer_when_annotations_change(memory_storage, published, document) -> None:
    upstream = FakeUpstream(INITIALIZED)
    driver = make_export_driver(memory_storage, upstream)
    export(driver)
    upstream.downloads.clear()

    published.publish(evolve(document, revision=document.revision + 1))
    (manifest,) = export(driver)
    assert len(upstream.downloads) == 4
    assert manifest.registration_revision == published.load_published().revision


def test_run_reexports_only_changed_layers(memory_storage, published) -> None:
    upstream = FakeUpstream(INITIALIZED)
    export(make_export_driver(memory_storage, upstream))
    upstream.downloads.clear()

    changed = make_model(
        EXPORT_MODEL.layers[0],
        evolve(EXPORT_MODEL.layers[1], hours=HourRange(first=3, last=9)),
        make_layer("snow", "wa_snow3", HourRange(first=3, last=3)),
    )
    (manifest,) = export(make_export_driver(memory_storage, upstream, changed))
    assert sorted(upstream.downloads) == [
        "2026102112/images_d3/wa_pcp3.03.0000.gif",
        "2026102112/images_d3/wa_pcp3.06.0000.gif",
        "2026102112/images_d3/wa_pcp3.09.0000.gif",
        "2026102112/images_d3/wa_snow3.03.0000.gif",
    ]
    assert [layer.id for layer in manifest.layers] == ["surface-temp", "3-hour-precip", "snow"]


def test_run_renames_layers_without_downloading(memory_storage, published) -> None:
    upstream = FakeUpstream(INITIALIZED)
    export(make_export_driver(memory_storage, upstream))
    upstream.downloads.clear()

    renamed = make_model(evolve(EXPORT_MODEL.layers[0], name="Temperature"), EXPORT_MODEL.layers[1])
    (manifest,) = export(make_export_driver(memory_storage, upstream, renamed))
    assert upstream.downloads == []
    assert manifest.layers[0].name == "Temperature"


def test_run_force_reexports(memory_storage, published) -> None:
    upstream = FakeUpstream(INITIALIZED)
    driver = make_export_driver(memory_storage, upstream)
    export(driver)
    upstream.downloads.clear()
    assert len(export(driver, force=True)) == 1
    assert len(upstream.downloads) == 4


def test_run_failed_download_leaves_the_published_run_alone(memory_storage, published) -> None:
    older = INITIALIZED - timedelta(hours=12)
    export(make_export_driver(memory_storage, FakeUpstream(older), now=older + timedelta(hours=6)))

    export(make_export_driver(memory_storage, FakeUpstream(INITIALIZED, older, fail="wa_pcp3.06")))
    store = ForecastStore(memory_storage)
    assert not store.read_manifest("uwwrf_4km", INITIALIZED).complete
    assert store.read_latest("uwwrf_4km").initialized == older


def test_run_ignores_runs_older_than_the_latest(memory_storage, published) -> None:
    store = ForecastStore(memory_storage)
    export(make_export_driver(memory_storage, FakeUpstream(INITIALIZED)))

    # an older run upstream (here, the only one left) is never exported, and latest.json never moves back
    older = INITIALIZED - timedelta(hours=12)
    upstream = FakeUpstream(older)
    export(make_export_driver(memory_storage, upstream))
    assert upstream.downloads == []
    assert store.read_manifest("uwwrf_4km", older) is None
    assert store.read_latest("uwwrf_4km").initialized == INITIALIZED


def test_run_prunes_runs_past_retention(memory_storage, published) -> None:
    old = INITIALIZED - timedelta(days=8)
    export(make_export_driver(memory_storage, FakeUpstream(old), now=old + timedelta(hours=6)))
    export(make_export_driver(memory_storage, FakeUpstream(INITIALIZED)))
    assert ForecastStore(memory_storage).list_runs("uwwrf_4km") == [INITIALIZED]


def test_run_exports_each_model_and_continues_past_failures(memory_storage, published) -> None:
    other = make_model(make_layer(hours=HourRange(last=0)), model_id="uwwrf_12km")
    broken = make_model(make_layer("snow", "wa_snow3", HourRange(last=0)), model_id="broken")
    # an outage checking for the broken model's runs fails that model only
    upstream = FakeUpstream(INITIALIZED, fail="wa_snow3", status=503)
    driver = make_export_driver(memory_storage, upstream, EXPORT_MODEL, broken, other)
    report = check(driver)
    assert report.failed == ["broken"]
    assert [model.state for model in report.models] == ["ok", "failed", "ok"]
    assert report.models[1].error == "HTTP 503"
    assert [manifest.model for manifest in report.manifests] == ["uwwrf_4km", "uwwrf_12km"]


# --- in-progress runs

NEXT = INITIALIZED + timedelta(hours=12)


def test_partial_run_is_exported_as_it_publishes(memory_storage, published) -> None:
    """The newer run's published hours are exported; latest.json stays on the complete run until it finishes."""
    store = ForecastStore(memory_storage)
    upstream = FakeUpstream(INITIALIZED, partial={NEXT: 3})
    driver = make_export_driver(memory_storage, upstream, now=NEXT + timedelta(hours=3))
    export(driver)

    partial = store.read_manifest("uwwrf_4km", NEXT)
    assert [(layer.id, [f.hour for f in layer.frames]) for layer in partial.layers] == [("surface-temp", [0, 3]), ("3-hour-precip", [3])]
    assert not partial.complete
    assert store.read_latest("uwwrf_4km").initialized == INITIALIZED
    progress = store.read_in_progress("uwwrf_4km")
    assert (progress.initialized, progress.path, progress.complete) == (NEXT, "2026/10/22/0000Z", False)
    assert [(layer.id, layer.available_hour, layer.last_hour, layer.error) for layer in progress.layers] == [
        ("surface-temp", 3, 3, None), ("3-hour-precip", 3, 6, None),
    ]

    # the next check downloads only the new hour, and the run becomes the latest
    upstream.downloads.clear()
    upstream.publish(NEXT, 999)
    export(driver)
    assert upstream.downloads == ["2026102200/images_d3/wa_pcp3.06.0000.gif"]
    assert store.read_manifest("uwwrf_4km", NEXT).complete
    assert store.read_latest("uwwrf_4km").initialized == NEXT
    assert store.read_in_progress("uwwrf_4km").complete


def test_resume_ignores_frame_order(memory_storage, published) -> None:
    """A manifest whose frames aren't in hour order resumes from its highest hour, without re-downloading."""
    store = ForecastStore(memory_storage)
    upstream = FakeUpstream(INITIALIZED, partial={NEXT: 3})
    driver = make_export_driver(memory_storage, upstream, now=NEXT + timedelta(hours=3))
    export(driver)
    manifest = store.read_manifest("uwwrf_4km", NEXT)
    manifest.layers[0].frames.reverse()
    store.write_manifest(manifest)

    upstream.downloads.clear()
    upstream.publish(NEXT, 999)
    export(driver)
    assert upstream.downloads == ["2026102200/images_d3/wa_pcp3.06.0000.gif"]
    assert store.read_in_progress("uwwrf_4km").layer("surface-temp").available_hour == 3
    assert store.read_latest("uwwrf_4km").initialized == NEXT


def test_run_not_started_writes_nothing(memory_storage, published) -> None:
    store = ForecastStore(memory_storage)
    upstream = FakeUpstream(INITIALIZED)
    driver = make_export_driver(memory_storage, upstream, now=NEXT + timedelta(hours=1))
    export(driver)
    assert store.read_manifest("uwwrf_4km", NEXT) is None
    assert store.list_runs("uwwrf_4km") == [INITIALIZED]
    # in-progress.json shows the newest run that has started, here the complete one
    assert store.read_in_progress("uwwrf_4km").initialized == INITIALIZED


def test_progress_time_is_kept_until_a_layer_advances(memory_storage, published) -> None:
    store = ForecastStore(memory_storage)
    upstream = FakeUpstream(INITIALIZED, partial={NEXT: 0})
    first = NEXT + timedelta(hours=1)
    export(make_export_driver(memory_storage, upstream, now=first))
    later = first + timedelta(minutes=15)
    export(make_export_driver(memory_storage, upstream, now=later))
    progress = store.read_in_progress("uwwrf_4km")
    assert progress.checked_at == later
    assert progress.layer("surface-temp").progressed_at == first
    assert progress.layer("3-hour-precip").progressed_at is None  # starts at hour 3


@pytest.mark.parametrize(("status", "error"), [(None, "ConnectTimeout: timed out"), (503, "HTTP 503")])
def test_upstream_errors_are_recorded_and_retried(memory_storage, published, status: int | None, error: str) -> None:
    """Only a 404 means "not published yet"; other failures stop the layer for this check and are retried."""
    store = ForecastStore(memory_storage)
    upstream = FakeUpstream(INITIALIZED, partial={NEXT: 0})
    driver = make_export_driver(memory_storage, upstream, now=NEXT + timedelta(hours=6))
    export(driver)
    upstream.publish(NEXT, 6)
    upstream.fail, upstream.status = "2026102200/images_d3/wa_tsfc.03", status
    assert export(driver)  # the other layer still exports; the model doesn't fail

    layer = store.read_in_progress("uwwrf_4km").layer("surface-temp")
    assert (layer.available_hour, layer.error) == (0, f"hour 3: {error}")
    assert [f.hour for f in store.read_manifest("uwwrf_4km", NEXT).layer("surface-temp").frames] == [0]

    upstream.fail = upstream.status = None
    export(driver)
    layer = store.read_in_progress("uwwrf_4km").layer("surface-temp")
    assert (layer.available_hour, layer.error) == (3, None)
    assert store.read_latest("uwwrf_4km").initialized == NEXT


def test_annotation_publish_mid_run_starts_layers_over(memory_storage, published, document) -> None:
    store = ForecastStore(memory_storage)
    upstream = FakeUpstream(INITIALIZED, partial={NEXT: 3})
    driver = make_export_driver(memory_storage, upstream, now=NEXT + timedelta(hours=3))
    export(driver)
    upstream.downloads.clear()

    published.publish(evolve(document, revision=document.revision + 1))
    export(driver)
    # the latest run and the in-progress one are both redone with the new annotations
    assert len([d for d in upstream.downloads if d.startswith("2026102200")]) == 3
    assert len([d for d in upstream.downloads if d.startswith("2026102112")]) == 4
    assert store.read_manifest("uwwrf_4km", NEXT).registration_revision == document.revision + 1


def test_prune_keeps_the_in_progress_run(memory_storage, published) -> None:
    """A model stuck mid-run keeps the run being shown as in progress, however old."""
    old = INITIALIZED - timedelta(days=10)
    export(make_export_driver(memory_storage, FakeUpstream(old - timedelta(hours=12), partial={old: 0}), now=old + timedelta(hours=6)))
    store = ForecastStore(memory_storage)
    assert store.read_in_progress("uwwrf_4km").initialized == old
    # much later, nothing new is published; both the latest and in-progress runs are kept
    export(make_export_driver(memory_storage, FakeUpstream(), now=old + timedelta(days=9)))
    assert store.list_runs("uwwrf_4km") == [old, old - timedelta(hours=12)]


def test_run_skips_without_a_complete_run(memory_storage, published) -> None:
    upstream = FakeUpstream()
    assert export(make_export_driver(memory_storage, upstream), force=True) == []
    assert frame_keys(memory_storage) == []


def test_run_skips_without_published_annotations(memory_storage) -> None:
    upstream = FakeUpstream(INITIALIZED)
    assert export(make_export_driver(memory_storage, upstream)) == []
    assert upstream.downloads == []


class FakeSource:
    """A source with every layer's hours published up to `published[run]`, rendered as tiny paletted frames."""

    def __init__(self, published: dict[datetime, int], revision: int | None = None, legend: Legend | None = None) -> None:
        self.published = published
        self.current_revision = revision
        self.legend = legend
        self.rendered: list[tuple[str, int]] = []
        self.checking = False

    @asynccontextmanager
    async def check(self) -> AsyncIterator[None]:
        self.checking = True
        try:
            yield
        finally:
            self.checking = False

    async def latest_complete_run(self, model: ModelConfig, now: datetime) -> datetime | None:
        last = max(layer.hours.last for layer in model.layers)
        return max((run for run, hour in self.published.items() if hour >= last), default=None)

    def revision(self, model: ModelConfig) -> int | None:
        return self.current_revision

    async def render_frame(self, model: ModelConfig, layer: LayerConfig, initialized: datetime, hour: int, out: Path) -> RenderedFrame:
        assert self.checking
        if hour > self.published.get(initialized, -1):
            raise NotPublished(f"hour {hour}")
        self.rendered.append((layer.id, hour))
        data = np.zeros((1, 4, 4), dtype=np.uint8)
        data[0, 1:3, 1:3] = 1
        transform = Affine(1000.0, 0.0, -13_600_000.0, 0.0, -1000.0, 6_000_000.0)
        write_geotiff(out, data, transform, nodata=255, colormap={0: (0, 0, 0, 255), 1: (255, 0, 0, 255)})
        if self.legend is None:
            return RenderedFrame()
        values = np.full((4, 4), hour + 0.25)
        values[0, 0] = np.nan
        return RenderedFrame(values=values, legend=self.legend)


def make_fake_driver(memory_storage, source: FakeSource, now: datetime = NOW) -> ForecastDriver:
    return ForecastDriver(ModelsConfig([EXPORT_MODEL]), {"uw_wrf": source}, ForecastStore(memory_storage), now=lambda: now)


def test_driver_exports_from_any_source(memory_storage) -> None:
    """The driver needs no annotations or HTTP: whatever the source renders becomes the run's frames."""
    source = FakeSource({INITIALIZED: 999, NEXT: 0})
    driver = make_fake_driver(memory_storage, source, now=NEXT + timedelta(hours=1))
    complete, partial = export(driver)
    assert (complete.initialized, complete.complete, complete.registration_revision) == (INITIALIZED, True, None)
    assert [(layer.id, [f.hour for f in layer.frames]) for layer in partial.layers] == [("surface-temp", [0]), ("3-hour-precip", [])]
    store = ForecastStore(memory_storage)
    assert store.read_latest("uwwrf_4km").initialized == INITIALIZED
    assert len(frame_keys(memory_storage)) == 5

    # nothing new: nothing is rendered again
    source.rendered.clear()
    assert export(driver) == []
    assert source.rendered == []


def test_frames_with_values_get_a_value_image(memory_storage, tmp_path: Path) -> None:
    """A source's numbers are exported beside each frame, with the legend and the model's attribution."""
    legend = Legend("°F", [LegendStop(0, "#0000ff"), LegendStop(32, "#ffffff")], decimals=1)
    model = evolve(EXPORT_MODEL, attribution="Data: somebody (CC BY 4.0)")
    driver = ForecastDriver(
        ModelsConfig([model]), {"uw_wrf": FakeSource({INITIALIZED: 999}, legend=legend)}, ForecastStore(memory_storage), now=lambda: NOW
    )
    (manifest,) = export(driver)
    assert manifest.attribution == "Data: somebody (CC BY 4.0)"
    layer = manifest.layer("surface-temp")
    assert (layer.legend, layer.values) == (legend, ValueEncoding())
    frame = layer.frames[1]
    assert re.fullmatch(r"surface-temp/2026-10-21T1500-[0-9a-f]{16}\.values\.png", frame.values)

    data, content_type = memory_storage.objects[f"forecasts/uwwrf_4km/2026/10/21/1200Z/{frame.values}"]
    assert content_type == "image/png"
    (tmp_path / "values.png").write_bytes(data)
    values = decode_values_png(tmp_path / "values.png", layer.values.scale, layer.values.offset)
    assert values.shape == (4, 4) and np.isnan(values[0, 0])
    assert values[1, 1] == pytest.approx(3.25)


def test_frame_budget_spreads_a_backlog_over_checks(memory_storage) -> None:
    """A model exports at most `frames_per_model` frames per check, then picks up where it stopped."""
    source = FakeSource({INITIALIZED: 999})
    driver = ForecastDriver(ModelsConfig([EXPORT_MODEL]), {"uw_wrf": source}, ForecastStore(memory_storage), now=lambda: NOW, frames_per_model=3)
    (manifest,) = export(driver)
    assert source.rendered == [("surface-temp", 0), ("surface-temp", 3), ("3-hour-precip", 3)]
    assert not manifest.complete
    progress = ForecastStore(memory_storage).read_in_progress("uwwrf_4km").layer("3-hour-precip")
    assert (progress.available_hour, progress.error) == (3, None)  # not an error: it continues next check

    source.rendered.clear()
    (manifest,) = export(driver)
    assert source.rendered == [("3-hour-precip", 6)] and manifest.complete


def test_source_revision_change_reexports(memory_storage) -> None:
    source = FakeSource({INITIALIZED: 999}, revision=1)
    driver = make_fake_driver(memory_storage, source)
    export(driver)
    source.rendered.clear()
    source.current_revision = 2
    (manifest,) = export(driver)
    assert len(source.rendered) == 4 and manifest.registration_revision == 2


def test_fit_report_matches_snapshot_thresholds(document: AnnotationDocument) -> None:
    """The snapshot's thresholds are 1.5x its fit errors, so nothing is over threshold."""
    report = fit_report(document)
    assert set(report) == set(document.maptypes)
    for maptype, fit in report.items():
        assert fit.error is None and fit.mean_error_m is not None
        assert not fit.over_threshold
        assert fit.fit_threshold_m == pytest.approx(fit.mean_error_m * 1.5, abs=5)
    assert report["washington"].mean_error_m == pytest.approx(779, abs=1)


def test_fit_report_flags_too_few_points(document: AnnotationDocument) -> None:
    sparse = AnnotationDocument.from_json(document.to_json())
    images = sparse.maptypes["washington"].images
    for image in images.values():
        image.annotations = dict(list(image.annotations.items())[:1])
    del images["legacy-hour36"]
    fit = fit_report(sparse)["washington"]
    assert fit.mean_error_m is None and "two control points" in fit.error
    with pytest.raises(ValueError, match="washington"):
        Registration.build(sparse)


def test_cli_registers_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("export-web", "serve", "download", "reproject", "check-layers", "delete-legacy-exports"):
        assert command in result.output
    assert "--force" in CliRunner().invoke(main, ["export-web", "--help"]).output


# --- UW requests: retries and throttling


class Answers:
    """Answers a client's requests with the queued responses in turn (a status, or an exception to raise)."""

    def __init__(self, *answers, headers: dict[str, str] | None = None) -> None:
        self.answers = list(answers)
        self.headers = headers or {}
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, content=b"gif", headers=self.headers)


def retrying_client(answers: Answers, waits: list[float]) -> ImageClient:
    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    return ImageClient(
        httpx.AsyncClient(transport=httpx.MockTransport(answers)), throttle=Throttle(interval=0, sleep=no_sleep), sleep=sleep
    )


LAYER = make_layer()


def test_overloaded_responses_are_retried_with_backoff() -> None:
    waits: list[float] = []
    answers = Answers(503, 502, 200)
    client = retrying_client(answers, waits)
    assert asyncio.run(client.download_image(LAYER, INITIALIZED, 3)) == b"gif"
    assert answers.requests == 3
    # exponential, with up to 25% jitter below
    assert 1.5 <= waits[0] <= 2.0 and 3.0 <= waits[1] <= 4.0
    # a 503 holds back every request to UW, not just the retried one
    assert client.throttle._next_start > 0


def test_retries_give_up_after_the_last_try() -> None:
    waits: list[float] = []
    answers = Answers(503)
    with pytest.raises(httpx.HTTPStatusError, match="503"):
        asyncio.run(retrying_client(answers, waits).download_image(LAYER, INITIALIZED, 3))
    assert answers.requests == 4 and len(waits) == 3

    answers = Answers(httpx.ConnectTimeout("timed out"))
    with pytest.raises(httpx.ConnectTimeout):
        asyncio.run(retrying_client(answers, []).download_image(LAYER, INITIALIZED, 3))
    assert answers.requests == 4


def test_connection_errors_are_retried() -> None:
    answers = Answers(httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), 200)
    assert asyncio.run(retrying_client(answers, []).image_exists(LAYER, INITIALIZED, 3))
    assert answers.requests == 3


def test_not_published_is_never_retried() -> None:
    answers = Answers(404)
    assert not asyncio.run(retrying_client(answers, []).image_exists(LAYER, INITIALIZED, 3))
    assert answers.requests == 1


def test_retry_after_is_honored_up_to_the_cap() -> None:
    waits: list[float] = []
    asyncio.run(retrying_client(Answers(429, 200, headers={"Retry-After": "12"}), waits).download_image(LAYER, INITIALIZED, 3))
    assert waits == [12.0]
    waits.clear()
    asyncio.run(retrying_client(Answers(503, 200, headers={"Retry-After": "600"}), waits).download_image(LAYER, INITIALIZED, 3))
    assert waits == [30.0]


def test_throttle_limits_concurrency_and_spaces_requests() -> None:
    in_flight = peak = 0
    waits: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200)

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    client = ImageClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), throttle=Throttle(concurrency=2, interval=5.0, sleep=sleep))

    async def many() -> None:
        await asyncio.gather(*(client.image_exists(LAYER, INITIALIZED, hour) for hour in range(6)))

    asyncio.run(many())
    assert peak == 2
    # every request after the first waited out the spacing (the fake sleep doesn't pass time)
    assert len(waits) == 5 and all(4.9 < wait <= 5.0 for wait in waits)


# --- concurrency and isolation between models


class SlowSource(FakeSource):
    """A FakeSource whose `stuck` model never finishes rendering, and that records which models render at once."""

    def __init__(self, published: dict[datetime, int], stuck: str | None = None) -> None:
        super().__init__(published)
        self.stuck = stuck
        self.active: set[str] = set()
        self.overlapped = False

    async def render_frame(self, model: ModelConfig, layer: LayerConfig, initialized: datetime, hour: int, out: Path) -> RenderedFrame:
        self.active.add(model.id)
        self.overlapped |= len(self.active) > 1
        try:
            if model.id == self.stuck:
                await asyncio.Event().wait()
            await asyncio.sleep(0.01)
            return await super().render_frame(model, layer, initialized, hour, out)
        finally:
            self.active.discard(model.id)


TWO_MODELS = [EXPORT_MODEL, evolve(EXPORT_MODEL, id="uwwrf_12km")]


def test_models_are_exported_concurrently(memory_storage) -> None:
    source = SlowSource({INITIALIZED: 999})
    driver = ForecastDriver(ModelsConfig(TWO_MODELS), {"uw_wrf": source}, ForecastStore(memory_storage), now=lambda: NOW)
    report = check(driver)
    assert source.overlapped
    assert [(m.model, m.state, m.frames) for m in report.models] == [("uwwrf_4km", "ok", 4), ("uwwrf_12km", "ok", 4)]


def test_a_model_that_runs_too_long_is_stopped_without_holding_up_the_others(memory_storage) -> None:
    source = SlowSource({INITIALIZED: 999}, stuck="uwwrf_4km")
    driver = ForecastDriver(
        ModelsConfig(TWO_MODELS), {"uw_wrf": source}, ForecastStore(memory_storage), now=lambda: NOW, model_timeout=1.0
    )
    stuck, other = check(driver).models
    assert stuck.state == "failed" and stuck.error.startswith("stopped after")
    assert (other.state, other.frames) == ("ok", 4)
    assert ForecastStore(memory_storage).read_latest("uwwrf_12km").initialized == INITIALIZED


def test_a_source_that_cant_start_its_check_fails_only_its_models(memory_storage) -> None:
    class Broken(FakeSource):
        @asynccontextmanager
        async def check(self) -> AsyncIterator[None]:
            raise RuntimeError("annotations unreadable")
            yield

    temperature = DynamicalLayerConfig("surface-temp", "Temperature", FieldConfig(("temperature_2m",)), "temperature_f", HourRange(last=3))
    dynamical = ModelConfig(
        "gfs", "GFS", [0, 12], [temperature], source=DynamicalSourceConfig("test"), region=(-126.0, 44.0, -116.0, 49.0)
    )
    driver = ForecastDriver(
        ModelsConfig([EXPORT_MODEL, dynamical]),
        {"uw_wrf": Broken({}), "dynamical": FakeSource({INITIALIZED: 999})},
        ForecastStore(memory_storage),
        now=lambda: NOW,
    )
    broken, ok = check(driver).models
    assert (broken.state, broken.error) == ("failed", "could not start the check: RuntimeError: annotations unreadable")
    assert (ok.state, ok.frames) == ("ok", 2)


def test_frame_errors_are_reported_for_retry(memory_storage, published) -> None:
    upstream = FakeUpstream(INITIALIZED, partial={NEXT: 0})
    driver = make_export_driver(memory_storage, upstream, now=NEXT + timedelta(hours=6))
    export(driver)
    upstream.publish(NEXT, 6)
    upstream.fail, upstream.status = "2026102200/images_d3/wa_tsfc.03", 503
    (model,) = check(driver).models
    assert (model.state, model.layer_errors) == ("ok", ["surface-temp hour 3: HTTP 503"])


# --- the index the server reads (forecasts/index.json)


def index_writes(memory_storage) -> list[ForecastIndex]:
    """Record each index written to `memory_storage`."""
    writes: list[ForecastIndex] = []
    original = memory_storage.put_bytes

    def put_bytes(key: str, data: bytes, *args, **kwargs):
        if key == INDEX:
            writes.append(ForecastIndex.from_json(data))
        return original(key, data, *args, **kwargs)

    memory_storage.put_bytes = put_bytes
    return writes


def test_index_matches_each_models_files_after_a_check(memory_storage) -> None:
    store = ForecastStore(memory_storage)
    store.write_index(ForecastIndex([ModelIndex("retired")]))  # no longer configured: dropped
    driver = ForecastDriver(
        ModelsConfig(TWO_MODELS), {"uw_wrf": FakeSource({INITIALIZED: 999, NEXT: 0})}, store, now=lambda: NEXT + timedelta(hours=1)
    )
    writes = index_writes(memory_storage)
    check(driver)
    # once up front, adding the models it lacked (empty: nothing exported yet), then as each model finished
    assert len(writes) == 3 and [m.runs for m in writes[0].models] == [[], []]
    index = store.read_index()
    assert [m.id for m in index.models] == ["uwwrf_4km", "uwwrf_12km"]
    assert index.models == [store.read_model_index("uwwrf_4km"), store.read_model_index("uwwrf_12km")]
    assert (index.models[0].latest.initialized, index.models[0].in_progress.initialized) == (INITIALIZED, NEXT)
    assert index.models[0].runs == [NEXT, INITIALIZED]
    assert index.updated_at == NEXT + timedelta(hours=1)

    # nothing new: the index isn't rewritten
    check(driver)
    assert len(writes) == 3


def test_index_includes_what_a_failed_model_exported(memory_storage) -> None:
    source = SlowSource({INITIALIZED: 999}, stuck="uwwrf_12km")
    store = ForecastStore(memory_storage)
    driver = ForecastDriver(ModelsConfig(TWO_MODELS), {"uw_wrf": source}, store, now=lambda: NOW, model_timeout=1.0)
    check(driver)
    assert store.read_index().models == [store.read_model_index("uwwrf_4km"), ModelIndex("uwwrf_12km")]


def test_index_write_failures_dont_fail_the_check_and_are_retried(memory_storage) -> None:
    store = ForecastStore(memory_storage)
    driver = make_fake_driver(memory_storage, FakeSource({INITIALIZED: 999}))
    original = memory_storage.put_bytes

    def broken(key: str, *args, **kwargs):
        if key == INDEX:
            raise RuntimeError("s3 down")
        return original(key, *args, **kwargs)

    memory_storage.put_bytes = broken
    report = check(driver)
    assert not report.failed and store.read_index() is None

    memory_storage.put_bytes = original
    check(driver)  # nothing new to export, but the index is still behind storage
    assert store.read_index().models == [store.read_model_index("uwwrf_4km")]


def test_index_is_reread_at_each_check(memory_storage) -> None:
    """Another writer (`upslope export-web`, run by hand) may have changed the index; it's compared against storage."""
    store = ForecastStore(memory_storage)
    driver = ForecastDriver(ModelsConfig(TWO_MODELS), {"uw_wrf": FakeSource({INITIALIZED: 999})}, store, now=lambda: NOW)
    check(driver)
    expected = store.read_index().models
    store.write_index(ForecastIndex(expected[:1]))  # written by a process with only uwwrf_4km configured
    check(driver)
    assert store.read_index().models == expected


def test_models_missing_from_the_index_are_added_before_checking(memory_storage) -> None:
    """When the index is first written, models still being checked aren't left out of it meanwhile."""
    store = ForecastStore(memory_storage)
    without_index = ForecastDriver(
        ModelsConfig(TWO_MODELS), {"uw_wrf": FakeSource({INITIALIZED: 999})}, store, now=lambda: NOW, publish_index=False
    )
    check(without_index)
    assert store.read_index() is None
    seen: list[ForecastIndex | None] = []

    class Watching(FakeSource):
        async def render_frame(self, *args) -> RenderedFrame:
            seen.append(store.read_index())
            return await super().render_frame(*args)

    driver = ForecastDriver(ModelsConfig(TWO_MODELS), {"uw_wrf": Watching({INITIALIZED: 999, NEXT: 999})}, store, now=lambda: NEXT + timedelta(hours=1))
    check(driver)
    assert [m.id for m in seen[0].models] == ["uwwrf_4km", "uwwrf_12km"]
    assert all(m.latest.initialized == INITIALIZED for m in seen[0].models)


# --- two buckets: annotations stay private, forecasts go to the public one


def test_build_driver_reads_annotations_privately_and_exports_publicly(memory_storage, public_storage) -> None:
    from upslope.export_loop import build_driver

    driver = build_driver(ModelsConfig([EXPORT_MODEL]), memory_storage, public_storage)
    assert driver.store.storage is public_storage
    assert driver.sources["uw_wrf"].annotations.storage is memory_storage


def test_export_writes_only_forecasts_to_the_public_bucket(memory_storage, public_storage, published) -> None:
    upstream = FakeUpstream(INITIALIZED)
    driver = make_export_driver(memory_storage, upstream)
    driver.store = ForecastStore(public_storage)
    export(driver)
    assert public_storage.objects and all(key.startswith("forecasts/") for key in public_storage.objects)
    assert not any(key.startswith("forecasts/") for key in memory_storage.objects)
