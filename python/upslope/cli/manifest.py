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


@click.command()
@click.option("--yes", is_flag=True, help="Don't ask for confirmation.")
@click.option(
    "--old-forecasts",
    is_flag=True,
    help="Also delete forecasts/ from the private bucket, once forecasts are served from PUBLIC_BUCKET_NAME.",
)
def delete_legacy_exports(yes: bool, old_forecasts: bool) -> None:
    """Delete old storage layouts from the private bucket: everything under web_exports/ and model_runs/, and
    with --old-forecasts, forecasts/ (copied to the public bucket by `migrate-forecasts`).

    Never touches georegistration/, or anything in the public bucket.
    """
    from upslope.clients.forecasts import LEGACY_PREFIXES, PREFIX
    from upslope.clients.storage import StorageClient, StorageConfig

    configure_logging()
    storage = StorageClient.build()
    prefixes = list(LEGACY_PREFIXES)
    if old_forecasts:
        public = StorageConfig.from_env().public_bucket
        if public is None or public == storage.bucket:
            raise click.ClickException("PUBLIC_BUCKET_NAME isn't set to another bucket: forecasts/ in the private bucket is still in use")
        prefixes.append(f"{PREFIX}/")
    keys = {prefix: storage.list_keys(prefix) for prefix in prefixes}
    if not any(keys.values()):
        click.echo("nothing to delete")
        return
    summary = ", ".join(f"{len(found)} objects under {prefix}" for prefix, found in keys.items())
    if not yes:
        click.confirm(f"Delete {summary} from bucket {storage.bucket}?", abort=True)
    for prefix, found in keys.items():
        if found:
            storage.delete_keys(found)
            logger.info("deleted %d objects under %s", len(found), prefix)
    click.echo(f"deleted {sum(len(found) for found in keys.values())} objects")


@click.command()
@click.option("--dry-run", is_flag=True, help="List what would be copied without copying.")
@click.option("--source", "source_bucket", help="Bucket to copy from. Defaults to BUCKET_NAME.")
@click.option("--destination", "destination_bucket", help="Bucket to copy to. Defaults to PUBLIC_BUCKET_NAME.")
@click.option("-v", "--verbose", is_flag=True, help="List each key copied.")
def migrate_forecasts(dry_run: bool, source_bucket: str | None, destination_bucket: str | None, verbose: bool) -> None:
    """Copy exported forecasts (forecasts/) from the private bucket to the public one, server side.

    Copies only objects the destination lacks, never overwriting the exporter's newer files there, so it's
    safe to run again: before switching the exporter over, and once after to pick up what it exported meanwhile.
    """
    from upslope.clients.forecasts import migrate
    from upslope.clients.storage import StorageClient, StorageConfig

    configure_logging(verbose)
    destination_bucket = destination_bucket or StorageConfig.from_env().public_bucket
    if destination_bucket is None:
        raise click.ClickException("set PUBLIC_BUCKET_NAME or pass --destination")
    source, destination = StorageClient.build(source_bucket), StorageClient.build(destination_bucket)

    def progress(done: int, total: int) -> None:
        if done % 1000 == 0 or done == total:
            logger.info("copied %d of %d", done, total)

    try:
        result = migrate(source, destination, dry_run=dry_run, progress=progress)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    shown = result.copied if verbose else result.copied[:50] if dry_run else []
    for key in shown:
        click.echo(key)
    if len(shown) < len(result.copied) and dry_run:
        click.echo(f"... and {len(result.copied) - len(shown)} more")
    verb = "would copy" if dry_run else "copied"
    click.echo(f"{verb} {len(result.copied)} objects from {source.bucket} to {destination.bucket}; {result.skipped} already there")
