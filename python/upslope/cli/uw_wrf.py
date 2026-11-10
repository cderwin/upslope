from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click
from cattrs.preconf.json import make_converter

from upslope.cli import DEFAULT_MODELS_CONFIG, load_annotations

if TYPE_CHECKING:
    from upslope.models.config import ModelConfig, ModelsConfig
    from upslope.models.uw_wrf.image_client import ImageClient

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


async def _resolve_run(client: "ImageClient", model: "ModelConfig", run: datetime | None) -> datetime:
    """The run to use: the one given, else the newest complete run, else the newest run."""
    if run is not None:
        return run.replace(tzinfo=UTC)
    initialized = await client.latest_complete_run(model)
    if initialized is not None:
        return initialized
    newest = model.initializations(datetime.now(UTC))[0]
    click.echo(f"{model.id} has no complete run; checking the newest run")
    return newest


@click.command()
@config_option
@model_option
@run_option
def check_layers(config_path: Path, model_ids: tuple[str, ...], run: datetime | None) -> None:
    """Check that every hour of every configured layer exists upstream, e.g. after editing the models config.

    Checks the newest complete run unless --run is given. For dynamical.org models, also checks that each
    layer's variables, hours and units fit the dataset. Exits with an error if any layer has a problem.
    """
    import asyncio

    import httpx

    from upslope.cli.dynamical import check_model
    from upslope.models.config import ModelsConfig
    from upslope.models.uw_wrf.image_client import ImageClient, Throttle

    async def uw_results(model: "ModelConfig") -> tuple[datetime, dict]:
        # a one-off sweep of a few hundred HEADs: a little faster than the export's pace, still throttled
        client = ImageClient(throttle=Throttle(concurrency=4, interval=0.1))
        initialized = await _resolve_run(client, model, run)

        async def check(layer, hour) -> str:
            try:
                return "ok" if await client.image_exists(layer, initialized, hour) else "missing"
            except httpx.HTTPError as e:
                return f"error ({e.__class__.__name__})"

        checks = [(layer, hour) for layer in model.layers for hour in layer.hours.hours]
        return initialized, dict(zip(checks, await asyncio.gather(*(check(*c) for c in checks))))

    problems = 0
    for model in _select_models(ModelsConfig.load(config_path), model_ids):
        if model.source.kind == "dynamical":
            problems += check_model(model, run)
            continue
        initialized, results = asyncio.run(uw_results(model))
        click.echo(f"{model.id} run {initialized:%Y-%m-%d %HZ}:")

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
        raise click.ClickException(f"{problems} layer(s) have problems")


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
