"""Web export pipeline: config, georegistration and frame conversion, using the repo's real configs and sample GIFs."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import rasterio
from click.testing import CliRunner
from PIL import Image

from upslope.cli import DEFAULT_WRF_CONFIG, DEV_GEOREGISTRATION
from upslope.clients.annotations import read_snapshot, snapshot_image_path
from upslope.clients.storage import ModelRunMarker
from upslope.cli.main import main
from upslope.models.uw_wrf.driver import WrfDriver
from upslope.models.uw_wrf.georegistration import OUTPUT_CRS, Registration, fit_report
from upslope.models.uw_wrf.georegistration_document import AnnotationDocument
from upslope.models.uw_wrf.wrf_client import (
    CodeConfig,
    ForecastScheduleConfig,
    LayerConfig,
    WrfClient,
    WrfConfig,
    latest_initializations,
)

SAMPLE_GIF = snapshot_image_path(DEV_GEOREGISTRATION, "washington", "legacy-hour06")
INITIALIZED = datetime(2026, 10, 21, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def document() -> AnnotationDocument:
    return read_snapshot(DEV_GEOREGISTRATION)


@pytest.fixture(scope="module")
def registration(document: AnnotationDocument) -> Registration:
    return Registration.build(document)


# --- config


def test_every_configured_layer_resolves() -> None:
    """Catches typos in wrf_codes.yaml: every layer's names must map to codes and a schedule."""
    config = WrfConfig.load(DEFAULT_WRF_CONFIG)
    assert config.layers
    client = WrfClient(config)
    for layer in config.layers:
        assert config.get_forecast_hours(layer.forecast_schedule)
        assert client.build_url(INITIALIZED, layer.domain, layer.variable, 6, layer.maptype).endswith(".06.0000.gif")


def test_every_configured_maptype_has_a_transform(registration: Registration) -> None:
    config = WrfConfig.load(DEFAULT_WRF_CONFIG)
    missing = {layer.maptype for layer in config.layers} - registration.domain_transforms.keys()
    assert not missing


def test_build_url() -> None:
    config = WrfConfig(
        domain_codes=[CodeConfig("12km", "d2")],
        maptype_codes=[CodeConfig("washington", "wa_")],
        variable_codes=[CodeConfig("3-hour-precip", "pcp3")],
        forecast_schedules=[],
        layers=[],
    )
    url = WrfClient(config).build_url(INITIALIZED, "12km", "3-hour-precip", 9, "washington")
    assert url == "https://a.atmos.washington.edu/wrfrt/data/2026102112/images_d2/wa_pcp3.09.0000.gif"


def test_unknown_names_raise() -> None:
    config = WrfConfig(domain_codes=[], maptype_codes=[], variable_codes=[], forecast_schedules=[], layers=[])
    with pytest.raises(ValueError):
        config.get_domain_code("nope")
    with pytest.raises(ValueError):
        config.get_forecast_hours("nope")


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
    layer = LayerConfig(domain="4km", variable="surface-temp", forecast_schedule="test", maptype="washington")
    config = WrfConfig(
        domain_codes=[], maptype_codes=[], variable_codes=[],
        forecast_schedules=[ForecastScheduleConfig("test", [6])], layers=[layer],
    )
    tiff = tmp_path / "registered/4km/surface-temp/hour06.tiff"
    tiff.parent.mkdir(parents=True)
    registration.georegister(SAMPLE_GIF, tiff, "washington")

    driver = WrfDriver(config=config, client=WrfClient(config), storage=MagicMock(), annotations=MagicMock())
    manifest_layer = driver.export_layer(layer, tmp_path, INITIALIZED)

    assert (manifest_layer.model, manifest_layer.variable, manifest_layer.initialized) == ("uwwrf_4km", "surface-temp", INITIALIZED)
    assert [frame.hour for frame in manifest_layer.frames] == [6]
    for lon, lat in manifest_layer.coordinates:
        assert -130 < lon < -110 and 40 < lat < 52

    with Image.open(manifest_layer.frames[0].path) as png:
        assert png.mode == "P"
        assert "transparency" in png.info
        assert png.size == manifest_layer.dimensions


def test_export_layer_missing_frame_raises(registration: Registration, tmp_path: Path) -> None:
    layer = LayerConfig(domain="4km", variable="surface-temp", forecast_schedule="test", maptype="washington")
    config = WrfConfig(
        domain_codes=[], maptype_codes=[], variable_codes=[],
        forecast_schedules=[ForecastScheduleConfig("test", [6])], layers=[layer],
    )
    driver = WrfDriver(config=config, client=WrfClient(config), storage=MagicMock(), annotations=MagicMock())
    with pytest.raises(RuntimeError, match="does not exist"):
        driver.export_layer(layer, tmp_path, INITIALIZED)


# --- model run detection

RUN_CONFIG = WrfConfig(
    domain_codes=[CodeConfig("4km", "d3")],
    maptype_codes=[CodeConfig("washington", "wa_")],
    variable_codes=[CodeConfig("surface-temp", "tsfc"), CodeConfig("3-hour-precip", "pcp3")],
    forecast_schedules=[ForecastScheduleConfig("to-120", [0, 3, 120]), ForecastScheduleConfig("to-84", [6, 84])],
    layers=[
        LayerConfig("4km", "surface-temp", "to-120", "washington"),
        LayerConfig("4km", "3-hour-precip", "to-84", "washington"),
    ],
)


def uw_client(*published: str, status: int = 404) -> WrfClient:
    """A WrfClient whose requests are answered locally: `published` paths (under `data/`) exist, others get `status`."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/wrfrt/data/")
        return httpx.Response(200 if path in published else status)

    return WrfClient(RUN_CONFIG, httpx.Client(transport=httpx.MockTransport(handler)))


def last_hours(run: str) -> list[str]:
    """Paths of the last scheduled hour of each RUN_CONFIG layer in `run` (YYYYMMDDHH)."""
    return [f"{run}/images_d3/wa_tsfc.120.0000.gif", f"{run}/images_d3/wa_pcp3.84.0000.gif"]


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
def test_latest_initializations(now: datetime, newest: datetime) -> None:
    runs = latest_initializations(now, count=3)
    assert runs == [newest, newest - timedelta(hours=12), newest - timedelta(hours=24)]


def test_latest_complete_run_skips_a_run_still_being_published() -> None:
    """Mid-run, the new run's directory has early hours only; the previous run is complete and is used."""
    now = datetime(2026, 10, 29, 5, 0, tzinfo=UTC)
    partial = "2026102900/images_d3/wa_tsfc.06.0000.gif"
    client = uw_client(partial, *last_hours("2026102812"))
    assert client.latest_complete_run(now) == datetime(2026, 10, 28, 12, tzinfo=UTC)

    # every layer's last hour is needed, not just one
    client = uw_client(last_hours("2026102900")[0], *last_hours("2026102812"))
    assert client.latest_complete_run(now) == datetime(2026, 10, 28, 12, tzinfo=UTC)

    client = uw_client(*last_hours("2026102900"), *last_hours("2026102812"))
    assert client.latest_complete_run(now) == datetime(2026, 10, 29, 0, tzinfo=UTC)


def test_latest_complete_run_none_without_a_complete_run() -> None:
    assert uw_client().latest_complete_run(datetime(2026, 10, 29, 5, 0, tzinfo=UTC)) is None


def test_latest_complete_run_raises_on_upstream_errors() -> None:
    """Only a 404 means "not published yet"; an outage must not be mistaken for an incomplete run."""
    with pytest.raises(httpx.HTTPStatusError):
        uw_client(status=503).latest_complete_run(datetime(2026, 10, 29, 5, 0, tzinfo=UTC))


def test_download_layer_reads_the_runs_directory(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, content=b"gif")

    client = WrfClient(RUN_CONFIG, httpx.Client(transport=httpx.MockTransport(handler)))
    client.download_layer(RUN_CONFIG.layers[1], tmp_path, datetime(2026, 10, 28, 12, tzinfo=UTC))
    assert requested == [
        "/wrfrt/data/2026102812/images_d3/wa_pcp3.06.0000.gif",
        "/wrfrt/data/2026102812/images_d3/wa_pcp3.84.0000.gif",
    ]
    assert (tmp_path / "4km/3-hour-precip/hour84.gif").read_bytes() == b"gif"


# --- config fingerprint


def test_fingerprint_tracks_what_the_export_produces() -> None:
    def config(**changes) -> WrfConfig:
        fields = {
            "domain_codes": RUN_CONFIG.domain_codes,
            "maptype_codes": RUN_CONFIG.maptype_codes,
            "variable_codes": RUN_CONFIG.variable_codes,
            "forecast_schedules": RUN_CONFIG.forecast_schedules,
            "layers": RUN_CONFIG.layers,
        } | changes
        return WrfConfig(**fields)

    base = config().fingerprint()
    assert config().fingerprint() == base
    # unused codes and schedules don't change the export
    assert config(variable_codes=[*RUN_CONFIG.variable_codes, CodeConfig("unused", "x")]).fingerprint() == base
    assert config(forecast_schedules=[*RUN_CONFIG.forecast_schedules, ForecastScheduleConfig("unused", [1])]).fingerprint() == base

    changed = [
        config(layers=RUN_CONFIG.layers[:1]),
        config(layers=[*RUN_CONFIG.layers, LayerConfig("4km", "surface-temp", "to-84", "washington")]),
        config(forecast_schedules=[ForecastScheduleConfig("to-120", [0, 120]), RUN_CONFIG.forecast_schedules[1]]),
        config(variable_codes=[CodeConfig("surface-temp", "tsfc2"), RUN_CONFIG.variable_codes[1]]),
        config(maptype_codes=[CodeConfig("washington", "")]),
    ]
    assert base not in {c.fingerprint() for c in changed}


# --- driver and CLI


def make_driver(document: AnnotationDocument | None, marker: ModelRunMarker | None) -> tuple[WrfDriver, MagicMock, MagicMock]:
    client, storage, annotations = MagicMock(), MagicMock(), MagicMock()
    client.latest_complete_run.return_value = INITIALIZED
    storage.model_run_marker.return_value = marker
    annotations.load_published.return_value = document
    config = MagicMock(layers=[])
    config.fingerprint.return_value = FINGERPRINT
    driver = WrfDriver(config=config, client=client, storage=storage, annotations=annotations)
    return driver, client, storage


FINGERPRINT = "f1ngerpr1nt"


def exported_with(revision: int | None, fingerprint: str | None = FINGERPRINT) -> ModelRunMarker:
    return ModelRunMarker("uwwrf", INITIALIZED, "web_exports/x/manifest.json", revision, fingerprint)


def test_run_skips_run_exported_with_current_annotations_and_config(document: AnnotationDocument) -> None:
    driver, client, storage = make_driver(document, exported_with(document.revision))
    assert driver.run() is None
    storage.model_run_marker.assert_called_once_with("uwwrf", INITIALIZED)
    storage.export_manifest.assert_not_called()


@pytest.mark.parametrize(
    ("revision", "fingerprint"),
    [
        (None, FINGERPRINT),  # never exported
        (0, FINGERPRINT),  # annotations published since
        ("current", "an-older-config"),  # layers changed since, e.g. by a deploy
        ("current", None),  # exported before fingerprints were recorded
    ],
)
def test_run_reexports_when_annotations_or_config_changed(
    document: AnnotationDocument, revision: int | str | None, fingerprint: str | None
) -> None:
    marker = None if revision is None else exported_with(document.revision if revision == "current" else revision, fingerprint)
    driver, client, storage = make_driver(document, marker)
    storage.export_manifest.return_value = "web_exports/y/manifest.json"
    assert driver.run() is not None
    storage.mark_model_run_exported.assert_called_once_with(
        "uwwrf", INITIALIZED, "web_exports/y/manifest.json", document.revision, FINGERPRINT
    )


def test_run_force_reexports(document: AnnotationDocument) -> None:
    driver, client, storage = make_driver(document, exported_with(document.revision))
    storage.export_manifest.return_value = "web_exports/y/manifest.json"
    assert driver.run(force=True) is not None
    storage.mark_model_run_exported.assert_called_once()


def test_run_downloads_from_the_complete_run(document: AnnotationDocument) -> None:
    driver, client, storage = make_driver(document, None)
    layer = LayerConfig("4km", "surface-temp", "s", "washington")
    driver.config.layers = [layer]
    # the mocked download writes no frames, so the export stops after downloading
    with pytest.raises(ValueError, match="no frames"):
        driver.run()
    ((args, _),) = client.download_layer.call_args_list
    assert args[0] == layer and args[2] == INITIALIZED


def test_run_skips_without_a_complete_run(document: AnnotationDocument) -> None:
    driver, client, storage = make_driver(document, None)
    client.latest_complete_run.return_value = None
    assert driver.run(force=True) is None
    storage.export_manifest.assert_not_called()
    storage.mark_model_run_exported.assert_not_called()


def test_run_skips_without_published_annotations() -> None:
    driver, client, storage = make_driver(None, None)
    assert driver.run() is None
    client.latest_complete_run.assert_not_called()
    storage.export_manifest.assert_not_called()


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
    for command in ("export-web", "serve", "download", "reproject"):
        assert command in result.output
    assert "--force" in CliRunner().invoke(main, ["export-web", "--help"]).output
