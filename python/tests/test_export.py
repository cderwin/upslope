"""Web export pipeline: config, georegistration and frame conversion, using the repo's real configs and sample GIFs."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import io

import httpx
import pytest
import rasterio
from attrs import evolve
from click.testing import CliRunner
from PIL import Image

from upslope.cli import DEFAULT_MODELS_CONFIG, DEV_GEOREGISTRATION
from upslope.clients.annotations import AnnotationStore, read_snapshot, snapshot_image_path
from upslope.clients.forecasts import ForecastStore
from upslope.cli.main import main
from upslope.models.config import ConfigError, HourRange, LayerConfig, ModelConfig, ModelsConfig
from upslope.models.driver import ExportFailed, ForecastDriver
from upslope.models.image_client import ImageClient
from upslope.models.manifest import RunPointer
from upslope.models.uw_wrf.georegistration import OUTPUT_CRS, Registration, fit_report
from upslope.models.uw_wrf.georegistration_document import AnnotationDocument

SAMPLE_GIF = snapshot_image_path(DEV_GEOREGISTRATION, "washington", "legacy-hour06")
INITIALIZED = datetime(2026, 10, 21, 12, tzinfo=UTC)


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


def test_export_frame_uploads_a_placed_png(registration: Registration, memory_storage, tmp_path: Path) -> None:
    """Downloaded GIF -> transparent paletted PNG named by valid time, in a layer placed over Washington."""
    layer = make_layer(hours=HourRange(first=6, last=6))
    model = make_model(layer)
    client = MagicMock()
    client.download_image.return_value = SAMPLE_GIF.read_bytes()
    driver = ForecastDriver(config=ModelsConfig([model]), client=client, store=ForecastStore(memory_storage), annotations=MagicMock())

    run_layer = driver.new_layer(layer)
    frame = driver.export_frame(model, layer, run_layer, INITIALIZED, 6, lambda: registration, tmp_path)

    assert (frame.hour, frame.valid, frame.path) == (6, INITIALIZED + timedelta(hours=6), "surface-temp/2026-10-21T1800.png")
    assert run_layer.frames == [frame]
    for lon, lat in run_layer.coordinates:
        assert -130 < lon < -110 and 40 < lat < 52
    data, content_type = memory_storage.objects["forecasts/uwwrf_4km/2026/10/21/1200Z/surface-temp/2026-10-21T1800.png"]
    assert content_type == "image/png"
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

    return ImageClient(httpx.Client(transport=httpx.MockTransport(handler)))


def last_hours(run: str) -> list[str]:
    """Paths of the last hour of each RUN_MODEL layer in `run` (YYYYMMDDHH)."""
    return [f"{run}/images_d3/wa_tsfc.120.0000.gif", f"{run}/images_d3/wa_pcp3.84.0000.gif"]


def test_latest_complete_run_skips_a_run_still_being_published() -> None:
    """Mid-run, the new run's directory has early hours only; the previous run is complete and is used."""
    now = datetime(2026, 10, 29, 5, 0, tzinfo=UTC)
    partial = "2026102900/images_d3/wa_tsfc.06.0000.gif"
    client = uw_client(partial, *last_hours("2026102812"))
    assert client.latest_complete_run(RUN_MODEL, now) == datetime(2026, 10, 28, 12, tzinfo=UTC)

    # every layer's last hour is needed, not just one
    client = uw_client(last_hours("2026102900")[0], *last_hours("2026102812"))
    assert client.latest_complete_run(RUN_MODEL, now) == datetime(2026, 10, 28, 12, tzinfo=UTC)

    client = uw_client(*last_hours("2026102900"), *last_hours("2026102812"))
    assert client.latest_complete_run(RUN_MODEL, now) == datetime(2026, 10, 29, 0, tzinfo=UTC)


def test_latest_complete_run_none_without_a_complete_run() -> None:
    assert uw_client().latest_complete_run(RUN_MODEL, datetime(2026, 10, 29, 5, 0, tzinfo=UTC)) is None


def test_latest_complete_run_raises_on_upstream_errors() -> None:
    """Only a 404 means "not published yet"; an outage must not be mistaken for an incomplete run."""
    with pytest.raises(httpx.HTTPStatusError):
        uw_client(status=503).latest_complete_run(RUN_MODEL, datetime(2026, 10, 29, 5, 0, tzinfo=UTC))


def test_download_layer_reads_the_runs_directory(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, content=b"gif")

    client = ImageClient(httpx.Client(transport=httpx.MockTransport(handler)))
    layer = make_layer("3-hour-precip", "wa_pcp3", HourRange(first=6, last=12))
    client.download_layer(layer, datetime(2026, 10, 28, 12, tzinfo=UTC), tmp_path / "3-hour-precip")
    assert requested == [
        "/wrfrt/data/2026102812/images_d3/wa_pcp3.06.0000.gif",
        "/wrfrt/data/2026102812/images_d3/wa_pcp3.09.0000.gif",
        "/wrfrt/data/2026102812/images_d3/wa_pcp3.12.0000.gif",
    ]
    assert (tmp_path / "3-hour-precip/hour12.gif").read_bytes() == b"gif"

    # files already on disk aren't downloaded again
    requested.clear()
    client.download_layer(layer, datetime(2026, 10, 28, 12, tzinfo=UTC), tmp_path / "3-hour-precip")
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
        return ImageClient(httpx.Client(transport=httpx.MockTransport(self.handler)))


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
        client=upstream.client(),
        store=ForecastStore(memory_storage),
        annotations=AnnotationStore(memory_storage),
        now=lambda: now,
    )


def frame_keys(memory_storage) -> list[str]:
    return sorted(key for key in memory_storage.objects if key.startswith("forecasts/") and key.endswith(".png"))


def test_run_exports_the_newest_complete_run(memory_storage, published, document) -> None:
    upstream = FakeUpstream(INITIALIZED, INITIALIZED - timedelta(hours=12))
    driver = make_export_driver(memory_storage, upstream)
    (manifest,) = driver.run()

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
    driver.run()
    upstream.downloads.clear()
    puts = len(memory_storage.puts)

    assert driver.run() == []
    assert upstream.downloads == []
    # only the record of the check itself
    assert memory_storage.puts[puts:] == ["forecasts/uwwrf_4km/in-progress.json"]


def test_run_reexports_every_layer_when_annotations_change(memory_storage, published, document) -> None:
    upstream = FakeUpstream(INITIALIZED)
    driver = make_export_driver(memory_storage, upstream)
    driver.run()
    upstream.downloads.clear()

    published.publish(evolve(document, revision=document.revision + 1))
    (manifest,) = driver.run()
    assert len(upstream.downloads) == 4
    assert manifest.registration_revision == published.load_published().revision


def test_run_reexports_only_changed_layers(memory_storage, published) -> None:
    upstream = FakeUpstream(INITIALIZED)
    make_export_driver(memory_storage, upstream).run()
    upstream.downloads.clear()

    changed = make_model(
        EXPORT_MODEL.layers[0],
        evolve(EXPORT_MODEL.layers[1], hours=HourRange(first=3, last=9)),
        make_layer("snow", "wa_snow3", HourRange(first=3, last=3)),
    )
    (manifest,) = make_export_driver(memory_storage, upstream, changed).run()
    assert sorted(upstream.downloads) == [
        "2026102112/images_d3/wa_pcp3.03.0000.gif",
        "2026102112/images_d3/wa_pcp3.06.0000.gif",
        "2026102112/images_d3/wa_pcp3.09.0000.gif",
        "2026102112/images_d3/wa_snow3.03.0000.gif",
    ]
    assert [layer.id for layer in manifest.layers] == ["surface-temp", "3-hour-precip", "snow"]


def test_run_renames_layers_without_downloading(memory_storage, published) -> None:
    upstream = FakeUpstream(INITIALIZED)
    make_export_driver(memory_storage, upstream).run()
    upstream.downloads.clear()

    renamed = make_model(evolve(EXPORT_MODEL.layers[0], name="Temperature"), EXPORT_MODEL.layers[1])
    (manifest,) = make_export_driver(memory_storage, upstream, renamed).run()
    assert upstream.downloads == []
    assert manifest.layers[0].name == "Temperature"


def test_run_force_reexports(memory_storage, published) -> None:
    upstream = FakeUpstream(INITIALIZED)
    driver = make_export_driver(memory_storage, upstream)
    driver.run()
    upstream.downloads.clear()
    assert len(driver.run(force=True)) == 1
    assert len(upstream.downloads) == 4


def test_run_failed_download_leaves_the_published_run_alone(memory_storage, published) -> None:
    older = INITIALIZED - timedelta(hours=12)
    make_export_driver(memory_storage, FakeUpstream(older), now=older + timedelta(hours=6)).run()

    make_export_driver(memory_storage, FakeUpstream(INITIALIZED, older, fail="wa_pcp3.06")).run()
    store = ForecastStore(memory_storage)
    assert not store.read_manifest("uwwrf_4km", INITIALIZED).complete
    assert store.read_latest("uwwrf_4km").initialized == older


def test_run_ignores_runs_older_than_the_latest(memory_storage, published) -> None:
    store = ForecastStore(memory_storage)
    make_export_driver(memory_storage, FakeUpstream(INITIALIZED)).run()

    # an older run upstream (here, the only one left) is never exported, and latest.json never moves back
    older = INITIALIZED - timedelta(hours=12)
    upstream = FakeUpstream(older)
    make_export_driver(memory_storage, upstream).run()
    assert upstream.downloads == []
    assert store.read_manifest("uwwrf_4km", older) is None
    assert store.read_latest("uwwrf_4km").initialized == INITIALIZED


def test_run_prunes_runs_past_retention(memory_storage, published) -> None:
    old = INITIALIZED - timedelta(days=8)
    make_export_driver(memory_storage, FakeUpstream(old), now=old + timedelta(hours=6)).run()
    make_export_driver(memory_storage, FakeUpstream(INITIALIZED)).run()
    assert ForecastStore(memory_storage).list_runs("uwwrf_4km") == [INITIALIZED]


def test_run_exports_each_model_and_continues_past_failures(memory_storage, published) -> None:
    other = make_model(make_layer(hours=HourRange(last=0)), model_id="uwwrf_12km")
    broken = make_model(make_layer("snow", "wa_snow3", HourRange(last=0)), model_id="broken")
    # an outage checking for the broken model's runs fails that model only
    upstream = FakeUpstream(INITIALIZED, fail="wa_snow3", status=503)
    driver = make_export_driver(memory_storage, upstream, EXPORT_MODEL, broken, other)
    with pytest.raises(ExportFailed) as failure:
        driver.run()
    assert failure.value.models == ["broken"]
    assert [manifest.model for manifest in failure.value.manifests] == ["uwwrf_4km", "uwwrf_12km"]


# --- in-progress runs

NEXT = INITIALIZED + timedelta(hours=12)


def test_partial_run_is_exported_as_it_publishes(memory_storage, published) -> None:
    """The newer run's published hours are exported; latest.json stays on the complete run until it finishes."""
    store = ForecastStore(memory_storage)
    upstream = FakeUpstream(INITIALIZED, partial={NEXT: 3})
    driver = make_export_driver(memory_storage, upstream, now=NEXT + timedelta(hours=3))
    driver.run()

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
    driver.run()
    assert upstream.downloads == ["2026102200/images_d3/wa_pcp3.06.0000.gif"]
    assert store.read_manifest("uwwrf_4km", NEXT).complete
    assert store.read_latest("uwwrf_4km").initialized == NEXT
    assert store.read_in_progress("uwwrf_4km").complete


def test_run_not_started_writes_nothing(memory_storage, published) -> None:
    store = ForecastStore(memory_storage)
    upstream = FakeUpstream(INITIALIZED)
    driver = make_export_driver(memory_storage, upstream, now=NEXT + timedelta(hours=1))
    driver.run()
    assert store.read_manifest("uwwrf_4km", NEXT) is None
    assert store.list_runs("uwwrf_4km") == [INITIALIZED]
    # in-progress.json shows the newest run that has started, here the complete one
    assert store.read_in_progress("uwwrf_4km").initialized == INITIALIZED


def test_progress_time_is_kept_until_a_layer_advances(memory_storage, published) -> None:
    store = ForecastStore(memory_storage)
    upstream = FakeUpstream(INITIALIZED, partial={NEXT: 0})
    first = NEXT + timedelta(hours=1)
    make_export_driver(memory_storage, upstream, now=first).run()
    later = first + timedelta(minutes=15)
    make_export_driver(memory_storage, upstream, now=later).run()
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
    driver.run()
    upstream.publish(NEXT, 6)
    upstream.fail, upstream.status = "2026102200/images_d3/wa_tsfc.03", status
    assert driver.run()  # the other layer still exports; the model doesn't fail

    layer = store.read_in_progress("uwwrf_4km").layer("surface-temp")
    assert (layer.available_hour, layer.error) == (0, f"hour 3: {error}")
    assert [f.hour for f in store.read_manifest("uwwrf_4km", NEXT).layer("surface-temp").frames] == [0]

    upstream.fail = upstream.status = None
    driver.run()
    layer = store.read_in_progress("uwwrf_4km").layer("surface-temp")
    assert (layer.available_hour, layer.error) == (3, None)
    assert store.read_latest("uwwrf_4km").initialized == NEXT


def test_annotation_publish_mid_run_starts_layers_over(memory_storage, published, document) -> None:
    store = ForecastStore(memory_storage)
    upstream = FakeUpstream(INITIALIZED, partial={NEXT: 3})
    driver = make_export_driver(memory_storage, upstream, now=NEXT + timedelta(hours=3))
    driver.run()
    upstream.downloads.clear()

    published.publish(evolve(document, revision=document.revision + 1))
    driver.run()
    # the latest run and the in-progress one are both redone with the new annotations
    assert len([d for d in upstream.downloads if d.startswith("2026102200")]) == 3
    assert len([d for d in upstream.downloads if d.startswith("2026102112")]) == 4
    assert store.read_manifest("uwwrf_4km", NEXT).registration_revision == document.revision + 1


def test_prune_keeps_the_in_progress_run(memory_storage, published) -> None:
    """A model stuck mid-run keeps the run being shown as in progress, however old."""
    old = INITIALIZED - timedelta(days=10)
    make_export_driver(memory_storage, FakeUpstream(old - timedelta(hours=12), partial={old: 0}), now=old + timedelta(hours=6)).run()
    store = ForecastStore(memory_storage)
    assert store.read_in_progress("uwwrf_4km").initialized == old
    # much later, nothing new is published; both the latest and in-progress runs are kept
    make_export_driver(memory_storage, FakeUpstream(), now=old + timedelta(days=9)).run()
    assert store.list_runs("uwwrf_4km") == [old, old - timedelta(hours=12)]


def test_run_skips_without_a_complete_run(memory_storage, published) -> None:
    upstream = FakeUpstream()
    assert make_export_driver(memory_storage, upstream).run(force=True) == []
    assert frame_keys(memory_storage) == []


def test_run_skips_without_published_annotations(memory_storage) -> None:
    upstream = FakeUpstream(INITIALIZED)
    assert make_export_driver(memory_storage, upstream).run() == []
    assert upstream.downloads == []


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
