from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click
from cattrs.preconf.json import make_converter

from upslope.cli import DEFAULT_MODELS_CONFIG, PROJECT_BASE, load_annotations

if TYPE_CHECKING:
    from upslope.models.config import ModelConfig, ModelsConfig
    from upslope.models.image_client import ImageClient

config_option = click.option(
    "-c", "--config", "config_path", type=click.Path(path_type=Path), default=DEFAULT_MODELS_CONFIG,
    help="Models config file.",
)
model_option = click.option("-m", "--model", "model_ids", multiple=True, help="Only this model (repeatable).")
run_option = click.option(
    "--run",
    type=click.DateTime(["%Y%m%d%H", "%Y-%m-%dT%H", "%Y-%m-%dT%HZ"]),
    help="Run initialization time in UTC, e.g. 2026102900 or 2026-10-29T00Z. Defaults to the newest complete run.",
)


def _select_models(config: "ModelsConfig", model_ids: tuple[str, ...]) -> list["ModelConfig"]:
    if not model_ids:
        return config.models
    unknown = [model_id for model_id in model_ids if config.model(model_id) is None]
    if unknown:
        raise click.ClickException(f"unknown model {', '.join(unknown)}; configured: {', '.join(m.id for m in config.models)}")
    return [model for model in config.models if model.id in model_ids]


def _resolve_run(client: "ImageClient", model: "ModelConfig", run: datetime | None, require_complete: bool) -> datetime:
    """The run to use: the one given, else the newest complete run, else (if allowed) the newest run."""
    if run is not None:
        return run.replace(tzinfo=UTC)
    initialized = client.latest_complete_run(model)
    if initialized is not None:
        return initialized
    if require_complete:
        raise click.ClickException(f"{model.id} has no complete run to download; pass --run")
    newest = model.initializations(datetime.now(UTC))[0]
    click.echo(f"{model.id} has no complete run; checking the newest run")
    return newest


@click.command()
@config_option
@model_option
@run_option
@click.option(
    "-o", "--output", "output_path", type=click.Path(path_type=Path), default=(PROJECT_BASE / "forecast/raw"),
    help="Directory to write <model>/<layer>/hourNN.gif to.",
)
def download(config_path: Path, model_ids: tuple[str, ...], run: datetime | None, output_path: Path) -> None:
    """Download every configured layer's images of a model run."""
    from upslope.models.config import ModelsConfig
    from upslope.models.image_client import ImageClient

    client = ImageClient()
    for model in _select_models(ModelsConfig.load(config_path), model_ids):
        initialized = _resolve_run(client, model, run, require_complete=True)
        click.echo(f"Downloading {model.id} run initialized {initialized:%Y-%m-%d %HZ}")
        for i, layer in enumerate(model.layers, start=1):
            click.echo(f"Downloading layer {i} of {len(model.layers)}: {layer.id}")
            client.download_layer(layer, initialized, output_path / model.id / layer.id)


@click.command()
@config_option
@click.option(
    "-i", "--input", "input_path", type=click.Path(path_type=Path), default=(PROJECT_BASE / "forecast/raw"),
    help="Directory written by `download`.",
)
@click.option(
    "-o", "--output", "output_path", type=click.Path(path_type=Path), default=(PROJECT_BASE / "forecast/georegistered"),
)
@click.option(
    "--snapshot",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    help="Read annotations from a snapshot directory (e.g. dev/georegistration) instead of storage.",
)
def reproject(config_path: Path, input_path: Path, output_path: Path, snapshot: Path | None) -> None:
    """Georegister downloaded GIFs, with the published annotations (or a snapshot's)."""
    from tqdm import tqdm

    from upslope.models.config import ModelsConfig
    from upslope.models.image_client import raw_image_name
    from upslope.models.uw_wrf.georegistration import Registration

    config = ModelsConfig.load(config_path)
    registration = Registration.build(load_annotations(snapshot))

    for model in config.models:
        for layer in tqdm(model.layers, desc=model.id):
            for hour in layer.hours.hours:
                raw_gif_path = input_path / model.id / layer.id / raw_image_name(hour)
                if not raw_gif_path.exists():
                    click.echo(f"forecast gif does not exist: {raw_gif_path}", err=True)
                    continue

                output_tiff_path = (output_path / raw_gif_path.relative_to(input_path)).with_suffix(".tiff")
                output_tiff_path.parent.mkdir(exist_ok=True, parents=True)
                registration.georegister(raw_gif_path, output_tiff_path, layer.maptype)


@click.command()
@config_option
@model_option
@run_option
def check_layers(config_path: Path, model_ids: tuple[str, ...], run: datetime | None) -> None:
    """Check that every hour of every configured layer exists upstream, e.g. after editing the models config.

    Checks the newest complete run unless --run is given. Exits with an error if any image is missing.
    """
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    from upslope.models.config import ModelsConfig
    from upslope.models.image_client import ImageClient

    client = ImageClient()
    problems = 0
    for model in _select_models(ModelsConfig.load(config_path), model_ids):
        initialized = _resolve_run(client, model, run, require_complete=False)
        click.echo(f"{model.id} run {initialized:%Y-%m-%d %HZ}:")

        def check(layer_hour):
            layer, hour = layer_hour
            try:
                return "ok" if client.image_exists(layer, initialized, hour) else "missing"
            except httpx.HTTPError as e:
                return f"error ({e.__class__.__name__})"

        checks = [(layer, hour) for layer in model.layers for hour in layer.hours.hours]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = dict(zip(checks, pool.map(check, checks)))

        for layer in model.layers:
            bad = {hour: results[(layer, hour)] for hour in layer.hours.hours if results[(layer, hour)] != "ok"}
            if not bad:
                click.echo(f"  ok       {layer.id}: {len(layer.hours.hours)} hours")
                continue
            problems += 1
            details = ", ".join(f"{hour} {status}" for hour, status in bad.items())
            click.echo(f"  PROBLEM  {layer.id}: {details}")
            click.echo(f"           e.g. {layer.image_url(initialized, next(iter(bad)))}")
    if problems:
        raise click.ClickException(f"{problems} layer(s) have missing or unreadable images")


@click.command()
@click.option(
    "--snapshot",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    help="Read annotations from a snapshot directory (e.g. dev/georegistration) instead of storage.",
)
@click.option("--draft", is_flag=True, help="Check the draft in storage instead of the published annotations.")
def registration_error(snapshot: Path | None, draft: bool) -> None:
    """Print each maptype's fit error, in meters, as JSON."""
    from upslope.models.uw_wrf.georegistration import fit_report

    report = fit_report(load_annotations(snapshot, draft))
    click.echo(make_converter().dumps(report, indent=2))
