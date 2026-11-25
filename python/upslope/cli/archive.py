"""CLI for the forecast archive: the archiver loop, one-off archiving, and checking a model against its source."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click

from upslope.cli import DEFAULT_ARCHIVE_CONFIG
from upslope.logging import configure_logging, get_logger

if TYPE_CHECKING:
    from upslope.archive.config import ArchiveConfig, ArchiveModel
    from upslope.archive.sources.base import ArchiveSource

logger = get_logger(__name__)

RUN_FORMATS = ["%Y%m%d%H", "%Y-%m-%dT%H", "%Y-%m-%dT%HZ"]
config_option = click.option(
    "-c", "--config", "config_path", type=click.Path(path_type=Path), default=DEFAULT_ARCHIVE_CONFIG, help="Archive config file."
)
run_option = click.option("--run", type=click.DateTime(RUN_FORMATS), help="Run initialization time in UTC. Defaults to the newest complete run.")


def load_config(config_path: Path, model_ids: tuple[str, ...] = ()) -> "ArchiveConfig":
    """Load the archive config; raises ClickException for an unknown model id."""
    from upslope.archive.config import ArchiveConfig

    config = ArchiveConfig.load(config_path)
    if unknown := [model_id for model_id in model_ids if config.model(model_id) is None]:
        raise click.ClickException(f"unknown model {', '.join(unknown)}; configured: {', '.join(m.id for m in config.models)}")
    return config


def resolve_run(source: "ArchiveSource", model: "ArchiveModel", run: datetime | None) -> datetime:
    """The run given, else the newest complete one. Raises ClickException if none is complete."""
    if run is not None:
        return run.replace(tzinfo=UTC)
    for initialized in model.initializations(datetime.now(UTC)):
        if source.is_complete(model, initialized):
            return initialized
    raise click.ClickException(f"{model.id} has no complete run in the last {model.lookback_hours} hours")


@click.command()
@config_option
@click.option("--interval", type=click.IntRange(1, 1440), default=15, show_default=True, help="Minutes between checks, on the clock.")
@click.option("--run-immediately", is_flag=True, help="Also check once at startup, instead of waiting for the first slot.")
@click.option("-v", "--verbose", is_flag=True, help="Log per-field detail.")
def archive_loop(config_path: Path, interval: int, run_immediately: bool, verbose: bool) -> None:
    """Run the archiver: archive new runs of every model on a schedule, forever.

    Runs as its own process (in production, the `archiver` Fly process group), the only writer of archive/ in the
    private bucket. Each check's outcome is written to archive/status.json. Errors are logged and recorded; they
    never stop the loop.
    """
    from datetime import timedelta

    from upslope.archive.loop import ArchiveLoop, build_driver
    from upslope.clients.storage import StorageClient

    configure_logging(verbose)
    # fail fast (and let the machine restart) on a broken config or missing credentials
    config = load_config(config_path)
    loop = ArchiveLoop(build_driver(config, StorageClient.build()), interval=timedelta(minutes=interval))
    try:
        asyncio.run(loop.run_forever(run_immediately=run_immediately))
    except KeyboardInterrupt:
        logger.info("archiver stopped")


@click.command()
@config_option
@click.option("-m", "--model", "model_ids", multiple=True, help="Only this model (repeatable). Defaults to every model.")
@click.option("-f", "--force", is_flag=True, help="Archive each model's newest run again, even if it's up to date.")
@click.option("-v", "--verbose", is_flag=True, help="Log per-field detail.")
def archive_run(config_path: Path, model_ids: tuple[str, ...], force: bool, verbose: bool) -> None:
    """Archive what every model (or those given with -m) is missing once, to storage, then exit.

    The same check `archive-loop` runs on a schedule, without recording it in archive/status.json.
    """
    from upslope.archive.loop import build_driver
    from upslope.clients.storage import StorageClient

    configure_logging(verbose)
    config = load_config(config_path, model_ids)
    driver = build_driver(config, StorageClient.build())
    driver.model_timeout = None
    statuses = asyncio.run(driver.run(list(model_ids) or None, force=force))
    for status in statuses:
        click.echo(f"{status.model}: {status.state} in {status.seconds:.0f}s, archived {', '.join(status.runs) or 'nothing'}")
        for warning in status.warnings:
            click.echo(f"  warning: {warning}")
        if status.error:
            click.echo(f"  error: {status.error}")
    if failed := [status.model for status in statuses if status.state == "failed"]:
        raise click.ClickException(f"archiving failed for {', '.join(failed)}")


@click.command()
@config_option
@click.option("-m", "--model", "model_id", required=True, help="Model id.")
@run_option
@click.option("-o", "--output", type=click.Path(path_type=Path, file_okay=False), help="Also write the run's store to this folder.")
@click.option("-v", "--verbose", is_flag=True, help="Log per-field detail.")
def archive_check(config_path: Path, model_id: str, run: datetime | None, output: Path | None, verbose: bool) -> None:
    """Check a model's config against its live source: read and standardize every variable of a run.

    Reports each variable's upstream fields that are missing, units that don't convert, lead times that don't fit
    the model's lead axis, and the range of its values (a quick check of units and conversions), then the same for
    the model's terrain. Nothing is written to storage; with --output, the run's store is written to a local folder.
    """
    import tempfile

    import numpy as np

    from upslope.archive.conversions import MissingField, standardize
    from upslope.archive.loop import build_sources
    from upslope.archive.sources.openmeteo import OpenMeteoRunSource
    from upslope.archive.terrain import model_terrain
    from upslope.archive.writer import RunWriter

    configure_logging(verbose)
    config = load_config(config_path, (model_id,))
    model = config.model(model_id)
    assert model is not None
    sources = build_sources()
    source = sources[model.source.kind]
    heights = sources["openmeteo"] if isinstance(sources["openmeteo"], OpenMeteoRunSource) else None
    initialized = resolve_run(source, model, run)
    click.echo(f"{model.id} ({model.source.describe()}) run {initialized:%Y-%m-%d %HZ}, {len(model.lead_hours)} lead times:")
    problems = 0
    chunks, shards = config.chunking(model)
    folder = output or Path(tempfile.mkdtemp(prefix="upslope-check-"))
    reader = source.open_run(model, initialized)
    writer = None
    try:
        for variable_id, source_field in model.variables.items():
            variable = config.variable(variable_id)
            try:
                fields = [reader.read(name) for name in source_field.fields]
                values, dropped = standardize(variable, source_field, fields, reader.grid, model.lead_hours)
            except (MissingField, ValueError) as e:
                problems += 1
                click.echo(f"  PROBLEM  {variable_id} <- {source_field.describe()}: {e}")
                continue
            with_data = int(np.isfinite(values).reshape(len(model.lead_hours), -1).any(axis=1).sum())
            finite = values[np.isfinite(values)]
            summary = f"{finite.min():.2f} to {finite.max():.2f} (mean {finite.mean():.2f}) {variable.units}" if finite.size else "no data"
            line = f"{variable_id} <- {source_field.describe()} [{fields[0].units}]: {with_data}/{len(model.lead_hours)} leads, {summary}"
            if dropped:
                problems += 1
                click.echo(f"  PROBLEM  {line}; upstream lead times {dropped} aren't on the lead axis")
            else:
                click.echo(f"  ok       {line}")
            if output is not None:
                writer = writer or RunWriter(folder / f"{initialized:%Y%m%dT%H}.zarr", reader.grid, model.lead_hours, chunks, shards, initialized, {"model": model.id})
                writer.write(variable, values, source_field.describe())
        if model.terrain is not None:
            try:
                terrain, terrain_source = model_terrain(model, reader, heights)
                finite = terrain[np.isfinite(terrain)]
                summary = f"{finite.min():.0f} to {finite.max():.0f} m" if finite.size else "no data"
                click.echo(f"  ok       terrain <- {terrain_source}: {summary}, {finite.size}/{terrain.size} cells")
                if writer is not None:
                    writer.write_terrain(terrain, terrain_source)
            except (MissingField, ValueError) as e:
                problems += 1
                click.echo(f"  PROBLEM  terrain <- {model.terrain.describe()}: {e}")
        if writer is not None:
            writer.finish()
            click.echo(f"wrote {folder / f'{initialized:%Y%m%dT%H}.zarr'}")
        grid = reader.grid
        click.echo(f"grid: {grid.shape[0]} x {grid.shape[1]} cells, transform {grid.geo_transform}")
    finally:
        reader.close()
        for each in sources.values():
            each.end_check()
    if problems:
        raise click.ClickException(f"{problems} problem(s)")
