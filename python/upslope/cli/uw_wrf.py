from pathlib import Path

import click
from cattrs.preconf.json import make_converter
from tqdm import tqdm

from upslope.cli import DEFAULT_GEOREGISTRATION_CONFIG, DEFAULT_WRF_CONFIG, PROJECT_BASE
from upslope.models.uw_wrf.georegistration import Registration, RegistrationConfig
from upslope.models.uw_wrf.wrf_client import WrfClient, WrfConfig


@click.command()
@click.option("-c", "--config", type=click.Path(path_type=Path), default=DEFAULT_WRF_CONFIG)
@click.option(
    "-o", "--output", "output_path", type=click.Path(path_type=Path), default=(PROJECT_BASE / "forecast/raw")
)
def download(config: Path, output_path: Path) -> None:
    wrf_config = WrfConfig.load(config)
    wrf_client = WrfClient(wrf_config)
    for i, layer in enumerate(wrf_config.layers):
        click.echo(f"Downloading layer {i + 1} of {len(wrf_config.layers)}")
        wrf_client.download_layer(layer, output_path)


@click.command()
@click.option(
    "-i", "--input", "input_path", type=click.Path(path_type=Path), default=(PROJECT_BASE / "forecast/raw")
)
@click.option(
    "-o", "--output", "output_path", type=click.Path(path_type=Path), default=(PROJECT_BASE / "forecast/georegistered")
)
def reproject(input_path: Path, output_path: Path) -> None:
    wrf_config = WrfConfig.load(DEFAULT_WRF_CONFIG)
    registration = Registration.build(RegistrationConfig.load(DEFAULT_GEOREGISTRATION_CONFIG))

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
@click.option("-c", "--config", "registration_config", type=click.Path(path_type=Path), default=DEFAULT_GEOREGISTRATION_CONFIG)
def registration_error(registration_config: Path) -> None:
    config = RegistrationConfig.load(registration_config)
    registration = Registration.build(config)
    errors = registration.compute_error(config)

    converter = make_converter()
    click.echo(converter.dumps(errors))
