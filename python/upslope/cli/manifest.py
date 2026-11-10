import asyncio
from pathlib import Path

import click

from upslope.cli import DEFAULT_MODELS_CONFIG
from upslope.logging import configure_logging, get_logger

logger = get_logger(__name__)


# Worker threads for blocking export work (rendering, georegistering, storage). Models are exported concurrently,
# and the default pool (CPUs + 4) would leave them queueing behind each other on a small machine.
EXPORT_THREADS = 16


def _use_export_threads() -> None:
    from concurrent.futures import ThreadPoolExecutor

    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(EXPORT_THREADS, thread_name_prefix="export"))


@click.command()
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=DEFAULT_MODELS_CONFIG)
@click.option("-f", "--force", is_flag=True, help="Export every layer again, even if the run was already exported.")
@click.option("-v", "--verbose", is_flag=True, help="Log per-file detail.")
def export_web(config_path: Path, force: bool, verbose: bool) -> None:
    """Check every model once for new frames and export them to storage, then exit.

    The same check `export-loop` runs on a schedule (and records in the export status, which this doesn't).
    UW WRF frames are georegistered with the annotations currently published in storage. Layers already
    exported with those annotations and the current config are kept as they are.
    """
    from upslope.clients.storage import StorageClient
    from upslope.export_loop import build_driver
    from upslope.models.config import ModelsConfig

    configure_logging(verbose)
    logger.info("loading models config from %s", config_path)
    config = ModelsConfig.load(config_path)
    driver = build_driver(config, StorageClient.build(), StorageClient.build_public(), model_timeout=None)

    async def check():
        _use_export_threads()
        return await driver.run(force=force)

    report = asyncio.run(check())
    for manifest in report.manifests:
        frame_count = sum(len(layer.frames) for layer in manifest.layers)
        logger.info("%s run %s: %d frames in %d layers", manifest.model, manifest.path, frame_count, len(manifest.layers))
    if report.failed:
        raise click.ClickException(f"export failed for {', '.join(report.failed)}")


@click.command()
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=DEFAULT_MODELS_CONFIG)
@click.option(
    "--interval",
    type=click.IntRange(1, 1440),
    default=15,
    show_default=True,
    help="Minutes between checks for new frames, on the clock (e.g. :00, :15, ...).",
)
@click.option(
    "--check-timeout",
    type=click.IntRange(1, 1440),
    default=45,
    show_default=True,
    help="Minutes a check may run before it's stopped (it resumes at the next).",
)
@click.option("--run-immediately", is_flag=True, help="Also check once at startup, instead of waiting for the first slot.")
@click.option("-v", "--verbose", is_flag=True, help="Log per-file detail.")
def export_loop(config_path: Path, interval: int, check_timeout: int, run_immediately: bool, verbose: bool) -> None:
    """Run the exporter: check every model for new frames on a schedule, forever.

    Runs as its own process (in production, the `exporter` Fly process group), separate from `serve`. Each
    check's outcome is written to forecasts/export-status.json, which `serve` reports at /fx/status. Errors
    are logged and recorded; they never stop the loop.
    """
    from datetime import timedelta

    from upslope.clients.storage import StorageClient
    from upslope.export_loop import ExportLoop, build_driver
    from upslope.models.config import ModelsConfig

    configure_logging(verbose)
    # fail fast (and let the machine restart) on a broken config or missing credentials
    logger.info("loading models config from %s", config_path)
    config = ModelsConfig.load(config_path)
    driver = build_driver(config, StorageClient.build(), StorageClient.build_public())
    loop = ExportLoop(
        driver, driver.store, interval=timedelta(minutes=interval), check_timeout=timedelta(minutes=check_timeout)
    )

    async def run() -> None:
        _use_export_threads()
        await loop.run_forever(run_immediately=run_immediately)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("exporter stopped")
