import click

from upslope.cli.dynamical import render_frame
from upslope.cli.georegistration import georegistration
from upslope.cli.manifest import delete_legacy_exports, export_loop, export_web, migrate_forecasts
from upslope.cli.server import serve
from upslope.cli.uw_wrf import check_layers, registration_error


@click.group()
def main() -> None:
    ...


main.add_command(check_layers)
main.add_command(registration_error)
main.add_command(export_web)
main.add_command(export_loop)
main.add_command(delete_legacy_exports)
main.add_command(migrate_forecasts)
main.add_command(serve)
main.add_command(georegistration)
main.add_command(render_frame)


if __name__ == "__main__":
    main()
