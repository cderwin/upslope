"""Web export pipeline: config, georegistration and frame conversion, using the repo's real configs and sample GIFs."""

from datetime import UTC, datetime
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
    RUN_REFERENCE_IMAGE,
    CodeConfig,
    ForecastScheduleConfig,
    LayerConfig,
    RunStatusError,
    WrfClient,
    WrfConfig,
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
        assert client.build_url(layer.domain, layer.variable, 6, layer.maptype).endswith(".06.0000.gif")


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
    url = WrfClient(config).build_url("12km", "3-hour-precip", 9, "washington")
    assert url == "https://a.atmos.washington.edu/wrfrt/data/timeindep/images_d2/wa_pcp3.09.0000.gif"


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


def uw_client(status: str = "", last_modified: str | None = None) -> WrfClient:
    """A WrfClient whose HTTP requests are answered locally: the status page and the run reference image."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/run_status.html"):
            return httpx.Response(200, text=status)
        if request.url.path.endswith(RUN_REFERENCE_IMAGE):
            headers = {} if last_modified is None else {"Last-Modified": last_modified}
            return httpx.Response(200, headers=headers)
        return httpx.Response(404)

    config = WrfConfig(domain_codes=[], maptype_codes=[], variable_codes=[], forecast_schedules=[], layers=[])
    return WrfClient(config, httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize(
    ("last_modified", "initialized"),
    [
        # hour 0 of the 12Z run goes up around 16Z, and the run is served until the next one starts
        ("Wed, 28 Oct 2026 15:47:36 GMT", datetime(2026, 10, 28, 12, tzinfo=UTC)),
        ("Thu, 29 Oct 2026 03:50:11 GMT", datetime(2026, 10, 29, 0, tzinfo=UTC)),
        ("Thu, 29 Oct 2026 00:00:00 GMT", datetime(2026, 10, 29, 0, tzinfo=UTC)),
        ("Wed, 28 Oct 2026 23:59:59 GMT", datetime(2026, 10, 28, 12, tzinfo=UTC)),
    ],
)
def test_run_initialization_comes_from_when_uw_published_hour_0(last_modified: str, initialized: datetime) -> None:
    """Not the clock: at 00Z/12Z UW still serves the previous run, which must keep its own label."""
    assert uw_client("complete", last_modified).current_run_initialization() == initialized


@pytest.mark.parametrize("last_modified", [None, "yesterday"])
def test_run_initialization_requires_a_publish_time(last_modified: str | None) -> None:
    with pytest.raises(RunStatusError):
        uw_client(last_modified=last_modified).current_run_initialization()


@pytest.mark.parametrize(
    ("cell", "in_progress"),
    [
        ("<td class=wait>not yet begun</td>", False),
        ("<td class=done>complete</td>", False),
        ("<td class='run'>finished 36/12/4, 1 1/3km to hr 53</td>", True),
        ("<td>complete</td>", False),
        ("<td>something new</td>", True),
    ],
)
def test_run_in_progress_reads_uw_status(cell: str, in_progress: bool) -> None:
    status = f"<td><a target='_top' href='/wrfrt/gfsinit.html'>WRF-GFS</a></td>{cell}</tr></table>"
    assert uw_client(status).run_in_progress() is in_progress


# --- driver and CLI


def make_driver(document: AnnotationDocument | None, marker: ModelRunMarker | None) -> tuple[WrfDriver, MagicMock, MagicMock]:
    client, storage, annotations = MagicMock(), MagicMock(), MagicMock()
    client.run_in_progress.return_value = False
    client.current_run_initialization.return_value = INITIALIZED
    storage.model_run_marker.return_value = marker
    annotations.load_published.return_value = document
    driver = WrfDriver(config=MagicMock(layers=[]), client=client, storage=storage, annotations=annotations)
    return driver, client, storage


def exported_with(revision: int | None) -> ModelRunMarker:
    return ModelRunMarker("uwwrf", INITIALIZED, "web_exports/x/manifest.json", revision)


def test_run_skips_run_exported_with_current_annotations(document: AnnotationDocument) -> None:
    driver, client, storage = make_driver(document, exported_with(document.revision))
    assert driver.run() is None
    storage.export_manifest.assert_not_called()


@pytest.mark.parametrize("previous", [None, 0])
def test_run_reexports_when_annotations_changed(document: AnnotationDocument, previous: int | None) -> None:
    """A publish (new revision) re-exports a run already exported with older annotations."""
    driver, client, storage = make_driver(document, exported_with(previous))
    storage.export_manifest.return_value = "web_exports/y/manifest.json"
    assert driver.run() is not None
    storage.mark_model_run_exported.assert_called_once_with("uwwrf", INITIALIZED, "web_exports/y/manifest.json", document.revision)


@pytest.mark.parametrize("force", [False, True])
def test_run_skips_while_uw_is_mid_run(document: AnnotationDocument, force: bool) -> None:
    """Mid-run, UW serves early hours from the new run and late hours from the old one; don't export that."""
    driver, client, storage = make_driver(document, None)
    client.run_in_progress.return_value = True
    assert driver.run(force=force) is None
    client.current_run_initialization.assert_not_called()
    storage.export_manifest.assert_not_called()
    storage.mark_model_run_exported.assert_not_called()


def test_run_skips_without_published_annotations() -> None:
    driver, client, storage = make_driver(None, None)
    assert driver.run() is None
    client.current_run_initialization.assert_not_called()
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
