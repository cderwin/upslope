from pathlib import Path

import click

from upslope.cli import DEFAULT_GEOREGISTRATION_CONFIG, DEFAULT_WRF_CONFIG
from upslope.clients.storage import StorageClient
from upslope.logging import configure_logging, get_logger
from upslope.models.uw_wrf.driver import WrfDriver
from upslope.models.uw_wrf.georegistration import Registration, RegistrationConfig
from upslope.models.uw_wrf.wrf_client import WrfClient, WrfConfig

logger = get_logger(__name__)


@click.command()
@click.option("-c", "--config", "wrf_config_path", type=click.Path(path_type=Path), default=DEFAULT_WRF_CONFIG)
@click.option(
    "-r",
    "--registration-config",
    "registration_config_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_GEOREGISTRATION_CONFIG,
)
@click.option("-f", "--force", is_flag=True, help="Export even if the current model run was already exported.")
@click.option("-v", "--verbose", is_flag=True, help="Log per-file detail.")
def export_web(wrf_config_path: Path, registration_config_path: Path, force: bool, verbose: bool) -> None:
    """Download, georegister and convert every configured layer, then upload the frames and manifest to storage."""
    configure_logging(verbose)
    logger.info("loading WRF config from %s and registration config from %s", wrf_config_path, registration_config_path)
    wrf_config = WrfConfig.load(wrf_config_path)
    driver = WrfDriver(
        config=wrf_config,
        client=WrfClient(wrf_config),
        registration=Registration.build(RegistrationConfig.load(registration_config_path)),
        storage=StorageClient.build(),
    )
    manifest = driver.run(force=force)
    if manifest is None:
        return
    frame_count = sum(len(layer.frames) for layer in manifest.layers)
    logger.info(
        "exported %d frames in %d layers in %.1fs",
        frame_count,
        len(manifest.layers),
        (manifest.finished - manifest.started).total_seconds(),
    )
