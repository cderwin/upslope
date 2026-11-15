"""CLI for dynamical.org models: checking layers against the live datasets, and previewing a frame."""

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click

from upslope.cli import DEFAULT_MODELS_CONFIG

if TYPE_CHECKING:
    from upslope.models.config import ModelConfig
    from upslope.models.dynamical.source import DynamicalSource


def resolve_run(source: "DynamicalSource", model: "ModelConfig", run: datetime | None) -> datetime:
    """The run to check: the one given, else the newest complete run, else the newest that should have started."""
    if run is not None:
        return run.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    initialized = source.find_latest_complete_run(model, now)
    if initialized is not None:
        return initialized
    click.echo(f"{model.id} has no complete run; checking the newest run")
    return model.initializations(now)[0]


def check_model(model: "ModelConfig", run: datetime | None) -> int:
    """Print whether each layer of a dynamical.org model would export fully; returns how many wouldn't."""
    from upslope.models.dynamical.source import DynamicalSource

    source = DynamicalSource()
    problems = 0
    source.begin_check()
    try:
        initialized = resolve_run(source, model, run)
        click.echo(f"{model.id} ({model.source.dataset}) run {initialized:%Y-%m-%d %HZ}:")
        for layer in model.layers:
            found = source.layer_problems(model, layer, initialized)
            if not found:
                click.echo(f"  ok       {layer.id}: {len(layer.hours.hours)} hours")
                continue
            problems += 1
            click.echo(f"  PROBLEM  {layer.id}: {'; '.join(found)}")
    finally:
        source.end_check()
    return problems


@click.command()
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=DEFAULT_MODELS_CONFIG, help="Models config file.")
@click.option("-m", "--model", "model_id", required=True, help="Model id (a dynamical.org model).")
@click.option("-l", "--layer", "layer_id", required=True, help="Layer id.")
@click.option("--hour", type=int, required=True, help="Forecast hour.")
@click.option(
    "--run",
    type=click.DateTime(["%Y%m%d%H", "%Y-%m-%dT%H", "%Y-%m-%dT%HZ"]),
    help="Run initialization time in UTC. Defaults to the newest complete run.",
)
@click.argument("output", type=click.Path(path_type=Path, dir_okay=False))
def render_frame(config_path: Path, model_id: str, layer_id: str, hour: int, run: datetime | None, output: Path) -> None:
    """Render one frame of a dynamical.org layer to OUTPUT (a lossless WebP), as the export would, without uploading it."""
    import numpy as np
    import rasterio

    from upslope.models.config import DynamicalSourceConfig, ModelsConfig
    from upslope.models.dynamical.source import DynamicalSource
    from upslope.models.source import NotPublished
    from upslope.raster import convert_to_webp, image_coordinates

    model = ModelsConfig.load(config_path).model(model_id)
    if model is None or not isinstance(model.source, DynamicalSourceConfig):
        raise click.ClickException(f"{model_id} isn't a configured dynamical.org model")
    layer = model.layer(layer_id)
    if layer is None:
        raise click.ClickException(f"{model_id} has no layer {layer_id}")

    source = DynamicalSource()
    tiff = output.with_suffix(".tiff")
    source.begin_check()
    try:
        try:
            initialized = resolve_run(source, model, run)
            frame = source.render(model, layer, initialized, hour, tiff)
        except NotPublished as e:
            raise click.ClickException(f"not published yet: {e}") from e
        finally:
            source.end_check()
        with rasterio.open(tiff) as dataset:
            convert_to_webp(dataset, output)
            corners = image_coordinates(dataset)
            size = (dataset.width, dataset.height)
    finally:
        tiff.unlink(missing_ok=True)
    values = frame.values[np.isfinite(frame.values)]
    click.echo(f"{model.id} {initialized:%Y-%m-%d %HZ} +{hour}h {layer.id}: wrote {output} ({size[0]}x{size[1]})")
    click.echo(f"  corners (lon, lat): {corners[0]} to {corners[2]}")
    if values.size:
        click.echo(f"  values {values.min():.{frame.legend.decimals}f} to {values.max():.{frame.legend.decimals}f} {frame.legend.units}")
