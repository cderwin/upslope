"""Web export pipeline: config, georegistration and frame conversion, using the repo's real configs and sample GIFs."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import rasterio
from attrs import evolve
from click.testing import CliRunner
from PIL import Image

from upslope.cli import DEFAULT_MODELS_CONFIG, DEV_GEOREGISTRATION
from upslope.clients.annotations import read_snapshot, snapshot_image_path
from upslope.clients.storage import ModelRunMarker
from upslope.cli.main import main
from upslope.models.config import ConfigError, HourRange, LayerConfig, ModelConfig, ModelsConfig
from upslope.models.driver import ExportFailed, ForecastDriver
from upslope.models.image_client import ImageClient
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


def test_export_layer_builds_manifest_layer(registration: Registration, tmp_path: Path) -> None:
    """Registered GeoTIFF -> transparent paletted PNG frame + manifest layer placed over Washington."""
    layer = make_layer(hours=HourRange(first=6, last=6))
    model = make_model(layer)
    tiff = tmp_path / "registered/surface-temp/hour06.tiff"
    tiff.parent.mkdir(parents=True)
    registration.georegister(SAMPLE_GIF, tiff, "washington")

    driver = ForecastDriver(config=ModelsConfig([model]), client=ImageClient(), storage=MagicMock(), annotations=MagicMock())
    manifest_layer = driver.export_layer(model, layer, tmp_path, INITIALIZED)

    # the same model/variable names as before layers had ids, so map links keep working
    assert (manifest_layer.model, manifest_layer.variable, manifest_layer.initialized) == ("uwwrf_4km", "surface-temp", INITIALIZED)
    assert [frame.hour for frame in manifest_layer.frames] == [6]
    for lon, lat in manifest_layer.coordinates:
        assert -130 < lon < -110 and 40 < lat < 52

    with Image.open(manifest_layer.frames[0].path) as png:
        assert png.mode == "P"
        assert "transparency" in png.info
        assert png.size == manifest_layer.dimensions


def test_export_layer_missing_frame_raises(tmp_path: Path) -> None:
    layer = make_layer(hours=HourRange(first=6, last=6))
    model = make_model(layer)
    driver = ForecastDriver(config=ModelsConfig([model]), client=ImageClient(), storage=MagicMock(), annotations=MagicMock())
    with pytest.raises(RuntimeError, match="does not exist"):
        driver.export_layer(model, layer, tmp_path, INITIALIZED)


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


# --- driver and CLI


def make_driver(
    document: AnnotationDocument | None, marker: ModelRunMarker | None, *models: ModelConfig
) -> tuple[ForecastDriver, MagicMock, MagicMock]:
    client, storage, annotations = MagicMock(), MagicMock(), MagicMock()
    client.latest_complete_run.return_value = INITIALIZED
    storage.model_run_marker.return_value = marker
    annotations.load_published.return_value = document
    config = ModelsConfig(list(models) or [RUN_MODEL])
    driver = ForecastDriver(config=config, client=client, storage=storage, annotations=annotations)
    return driver, client, storage


def exported_with(revision: int | None, fingerprint: str | None = RUN_MODEL.fingerprint()) -> ModelRunMarker:
    return ModelRunMarker("uwwrf_4km", INITIALIZED, "web_exports/x/manifest.json", revision, fingerprint)


def stub_export(driver: ForecastDriver, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, datetime]]:
    """Replace the slow download/georegister/convert step with one that records what it exported."""
    exported: list[tuple[str, datetime]] = []

    def export_run(self, model, initialized, registration, revision):
        exported.append((model.id, initialized))
        self.storage.mark_model_run_exported(model.id, initialized, "web_exports/y/manifest.json", revision, model.fingerprint())
        return MagicMock(layers=[])

    monkeypatch.setattr(ForecastDriver, "export_run", export_run)
    return exported


def test_run_skips_run_exported_with_current_annotations_and_config(document: AnnotationDocument, monkeypatch: pytest.MonkeyPatch) -> None:
    driver, client, storage = make_driver(document, exported_with(document.revision))
    exported = stub_export(driver, monkeypatch)
    assert driver.run() == []
    storage.model_run_marker.assert_called_once_with("uwwrf_4km", INITIALIZED)
    assert exported == []


@pytest.mark.parametrize(
    ("revision", "fingerprint"),
    [
        (None, RUN_MODEL.fingerprint()),  # never exported
        (0, RUN_MODEL.fingerprint()),  # annotations published since
        ("current", "an-older-config"),  # layers changed since, e.g. by a deploy
        ("current", None),  # exported before fingerprints were recorded
    ],
)
def test_run_reexports_when_annotations_or_config_changed(
    document: AnnotationDocument, monkeypatch: pytest.MonkeyPatch, revision: int | str | None, fingerprint: str | None
) -> None:
    marker = None if revision is None else exported_with(document.revision if revision == "current" else revision, fingerprint)
    driver, client, storage = make_driver(document, marker)
    exported = stub_export(driver, monkeypatch)
    assert len(driver.run()) == 1
    assert exported == [("uwwrf_4km", INITIALIZED)]
    storage.mark_model_run_exported.assert_called_once_with(
        "uwwrf_4km", INITIALIZED, "web_exports/y/manifest.json", document.revision, RUN_MODEL.fingerprint()
    )


def test_run_force_reexports(document: AnnotationDocument, monkeypatch: pytest.MonkeyPatch) -> None:
    driver, client, storage = make_driver(document, exported_with(document.revision))
    exported = stub_export(driver, monkeypatch)
    assert len(driver.run(force=True)) == 1
    assert exported == [("uwwrf_4km", INITIALIZED)]


def test_run_exports_each_model_from_its_own_complete_run(document: AnnotationDocument, monkeypatch: pytest.MonkeyPatch) -> None:
    other = make_model(make_layer(), model_id="uwwrf_1.33km")
    driver, client, storage = make_driver(document, None, RUN_MODEL, other)
    later = INITIALIZED - timedelta(hours=12)
    client.latest_complete_run.side_effect = lambda model: INITIALIZED if model.id == "uwwrf_4km" else later
    exported = stub_export(driver, monkeypatch)
    assert len(driver.run()) == 2
    assert exported == [("uwwrf_4km", INITIALIZED), ("uwwrf_1.33km", later)]


def test_run_continues_past_a_failing_model(document: AnnotationDocument, monkeypatch: pytest.MonkeyPatch) -> None:
    other = make_model(make_layer(), model_id="uwwrf_1.33km")
    driver, client, storage = make_driver(document, None, RUN_MODEL, other)
    exported = stub_export(driver, monkeypatch)

    def latest_complete_run(model):
        if model.id == "uwwrf_4km":
            raise httpx.ConnectTimeout("timed out")
        return INITIALIZED

    client.latest_complete_run.side_effect = latest_complete_run
    with pytest.raises(ExportFailed) as failure:
        driver.run()
    assert failure.value.models == ["uwwrf_4km"]
    assert len(failure.value.manifests) == 1
    assert exported == [("uwwrf_1.33km", INITIALIZED)]


def test_run_downloads_from_the_complete_run(document: AnnotationDocument) -> None:
    layer = make_layer()
    driver, client, storage = make_driver(document, None, make_model(layer))
    # the mocked download writes no frames, so the export stops after downloading
    with pytest.raises(ExportFailed):
        driver.run()
    ((args, _),) = client.download_layer.call_args_list
    assert args[0] == layer and args[1] == INITIALIZED and args[2].name == "surface-temp"


def test_run_skips_without_a_complete_run(document: AnnotationDocument, monkeypatch: pytest.MonkeyPatch) -> None:
    driver, client, storage = make_driver(document, None)
    exported = stub_export(driver, monkeypatch)
    client.latest_complete_run.return_value = None
    assert driver.run(force=True) == []
    assert exported == []


def test_run_skips_without_published_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    driver, client, storage = make_driver(None, None)
    exported = stub_export(driver, monkeypatch)
    assert driver.run() == []
    client.latest_complete_run.assert_not_called()
    assert exported == []


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
    for command in ("export-web", "serve", "download", "reproject", "check-layers"):
        assert command in result.output
    assert "--force" in CliRunner().invoke(main, ["export-web", "--help"]).output
