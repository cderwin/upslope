from pathlib import Path

import click
from cattrs.preconf.json import make_converter
from tqdm import tqdm

from upslope.cli import DEFAULT_WRF_CONFIG, PROJECT_BASE, load_annotations
from upslope.models.uw_wrf.georegistration import Registration, fit_report
from upslope.models.uw_wrf.wrf_client import WrfClient, WrfConfig


@click.command()
@click.option("-c", "--config", type=click.Path(path_type=Path), default=DEFAULT_WRF_CONFIG)
@click.option(
    "-o", "--output", "output_path", type=click.Path(path_type=Path), default=(PROJECT_BASE / "forecast/raw")
)
def download(config: Path, output_path: Path) -> None:
    wrf_config = WrfConfig.load(config)
    wrf_client = WrfClient(wrf_config)
    initialized = wrf_client.latest_complete_run()
    if initialized is None:
        raise click.ClickException("UW has no complete model run to download")
    click.echo(f"Downloading model run initialized {initialized:%Y-%m-%d %HZ}")
    for i, layer in enumerate(wrf_config.layers):
        click.echo(f"Downloading layer {i + 1} of {len(wrf_config.layers)}")
        wrf_client.download_layer(layer, output_path, initialized)


@click.command()
@click.option(
    "-i", "--input", "input_path", type=click.Path(path_type=Path), default=(PROJECT_BASE / "forecast/raw")
)
@click.option(
    "-o", "--output", "output_path", type=click.Path(path_type=Path), default=(PROJECT_BASE / "forecast/georegistered")
)
@click.option(
    "--snapshot",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    help="Read annotations from a snapshot directory (e.g. dev/georegistration) instead of storage.",
)
def reproject(input_path: Path, output_path: Path, snapshot: Path | None) -> None:
    """Georegister downloaded GIFs, with the published annotations (or a snapshot's)."""
    wrf_config = WrfConfig.load(DEFAULT_WRF_CONFIG)
    registration = Registration.build(load_annotations(snapshot))

    for layer in tqdm(wrf_config.layers):
        for fx_hour in wrf_config.get_forecast_hours(layer.forecast_schedule):
            raw_gif_path = input_path / layer.domain / layer.variable / f"hour{fx_hour:02d}.gif"
            if not raw_gif_path.exists():
                click.echo(f"forecast gif does not exist: {raw_gif_path}", err=True)
                continue

            output_tiff_path = (output_path / raw_gif_path.relative_to(input_path)).with_suffix(".tiff")
            output_tiff_path.parent.mkdir(exist_ok=True, parents=True)
            registration.georegister(raw_gif_path, output_tiff_path, layer.maptype)


@click.command()
@click.option(
    "--snapshot",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    help="Read annotations from a snapshot directory (e.g. dev/georegistration) instead of storage.",
)
@click.option("--draft", is_flag=True, help="Check the draft in storage instead of the published annotations.")
def registration_error(snapshot: Path | None, draft: bool) -> None:
    """Print each maptype's fit error, in meters, as JSON."""
    report = fit_report(load_annotations(snapshot, draft))
    click.echo(make_converter().dumps(report, indent=2))
