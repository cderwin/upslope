"""Web export pipeline: config, georegistration and frame conversion, using the repo's real configs and sample GIFs."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import rasterio
from click.testing import CliRunner
from PIL import Image

from upslope.cli import DEFAULT_GEOREGISTRATION_CONFIG, DEFAULT_WRF_CONFIG, PROJECT_BASE
from upslope.cli.main import main
from upslope.models.uw_wrf.driver import WrfDriver
from upslope.models.uw_wrf.georegistration import OUTPUT_CRS, Registration, RegistrationConfig
from upslope.models.uw_wrf.wrf_client import CodeConfig, ForecastScheduleConfig, LayerConfig, WrfClient, WrfConfig

SAMPLE_GIF = PROJECT_BASE / "data/georegistration/images/washington/hour06.gif"
INITIALIZED = datetime(2026, 10, 21, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def registration() -> Registration:
    return Registration.build(RegistrationConfig.load(DEFAULT_GEOREGISTRATION_CONFIG))


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

    driver = WrfDriver(config=config, client=WrfClient(config), registration=registration, storage=MagicMock())
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
    driver = WrfDriver(config=config, client=WrfClient(config), registration=registration, storage=MagicMock())
    with pytest.raises(RuntimeError, match="does not exist"):
        driver.export_layer(layer, tmp_path, INITIALIZED)


# --- driver and CLI


def test_run_skips_already_exported_model_run() -> None:
    client, storage = MagicMock(), MagicMock()
    client.current_run_initialization.return_value = INITIALIZED
    storage.model_run_exported.return_value = True
    driver = WrfDriver(config=MagicMock(layers=[MagicMock()]), client=client, registration=MagicMock(), storage=storage)

    assert driver.run() is None
    client.download_layer.assert_not_called()
    storage.export_manifest.assert_not_called()


def test_cli_registers_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("export-web", "serve", "download", "reproject"):
        assert command in result.output
    assert "--force" in CliRunner().invoke(main, ["export-web", "--help"]).output
