from pathlib import Path

import click

from upslope.cli import DEFAULT_MODELS_CONFIG
from upslope.logging import configure_logging, get_logger

logger = get_logger(__name__)


@click.command()
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=DEFAULT_MODELS_CONFIG)
@click.option("-f", "--force", is_flag=True, help="Export even if the current model run was already exported.")
@click.option("-v", "--verbose", is_flag=True, help="Log per-file detail.")
def export_web(config_path: Path, force: bool, verbose: bool) -> None:
    """Download, georegister and convert every configured layer, then upload the frames and manifest to storage.

    Georegisters with the annotations currently published in storage.
    """
    from upslope.clients.annotations import AnnotationStore
    from upslope.clients.storage import StorageClient
    from upslope.models.config import ModelsConfig
    from upslope.models.driver import ExportFailed, ForecastDriver
    from upslope.models.image_client import ImageClient

    configure_logging(verbose)
    logger.info("loading models config from %s", config_path)
    config = ModelsConfig.load(config_path)
    storage = StorageClient.build()
    driver = ForecastDriver(config=config, client=ImageClient(), storage=storage, annotations=AnnotationStore(storage))
    try:
        manifests = driver.run(force=force)
    except ExportFailed as e:
        raise click.ClickException(str(e)) from e
    for manifest in manifests:
        frame_count = sum(len(layer.frames) for layer in manifest.layers)
        logger.info(
            "exported %d frames in %d layers in %.1fs",
            frame_count,
            len(manifest.layers),
            (manifest.finished - manifest.started).total_seconds(),
        )
