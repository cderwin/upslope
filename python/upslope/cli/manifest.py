from pathlib import Path

import click

from upslope.cli import DEFAULT_MODELS_CONFIG
from upslope.logging import configure_logging, get_logger

logger = get_logger(__name__)


@click.command()
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=DEFAULT_MODELS_CONFIG)
@click.option("-f", "--force", is_flag=True, help="Export every layer again, even if the run was already exported.")
@click.option("-v", "--verbose", is_flag=True, help="Log per-file detail.")
def export_web(config_path: Path, force: bool, verbose: bool) -> None:
    """Export each model's newest complete run to storage: download, georegister and convert every layer.

    Georegisters with the annotations currently published in storage. Layers already exported with
    those annotations and the current config are kept as they are.
    """
    from upslope.clients.annotations import AnnotationStore
    from upslope.clients.forecasts import ForecastStore
    from upslope.clients.storage import StorageClient
    from upslope.models.config import ModelsConfig
    from upslope.models.driver import ExportFailed, ForecastDriver
    from upslope.models.image_client import ImageClient

    configure_logging(verbose)
    logger.info("loading models config from %s", config_path)
    config = ModelsConfig.load(config_path)
    storage = StorageClient.build()
    driver = ForecastDriver(config=config, client=ImageClient(), store=ForecastStore(storage), annotations=AnnotationStore(storage))
    try:
        manifests = driver.run(force=force)
    except ExportFailed as e:
        raise click.ClickException(str(e)) from e
    for manifest in manifests:
        frame_count = sum(len(layer.frames) for layer in manifest.layers)
        logger.info("%s run %s: %d frames in %d layers", manifest.model, manifest.path, frame_count, len(manifest.layers))


@click.command()
@click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
def delete_legacy_exports(yes: bool) -> None:
    """Delete the storage layout used before forecasts/: everything under web_exports/ and model_runs/.

    Never touches georegistration/ or forecasts/.
    """
    from upslope.clients.forecasts import LEGACY_PREFIXES, ForecastStore
    from upslope.clients.storage import StorageClient

    configure_logging()
    storage = StorageClient.build()
    counts = {prefix: len(storage.list_keys(prefix)) for prefix in LEGACY_PREFIXES}
    if not any(counts.values()):
        click.echo("nothing to delete")
        return
    summary = ", ".join(f"{count} objects under {prefix}" for prefix, count in counts.items())
    if not yes:
        click.confirm(f"Delete {summary} from bucket {storage.bucket}?", abort=True)
    deleted = ForecastStore(storage).delete_legacy()
    click.echo(f"deleted {deleted} objects")
