import click

from upslope.cli.georegistration import georegistration
from upslope.cli.manifest import export_web
from upslope.cli.server import serve
from upslope.cli.uw_wrf import check_layers, download, registration_error, reproject


@click.group()
def main() -> None:
    ...


main.add_command(download)
main.add_command(reproject)
main.add_command(check_layers)
main.add_command(registration_error)
main.add_command(export_web)
main.add_command(serve)
main.add_command(georegistration)


if __name__ == "__main__":
    main()
