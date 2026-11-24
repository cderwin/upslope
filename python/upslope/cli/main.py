import click

from upslope.cli.archive import archive_check, archive_loop, archive_run
from upslope.cli.dynamical import render_frame
from upslope.cli.georegistration import georegistration
from upslope.cli.manifest import export_loop, export_web
from upslope.cli.server import serve
from upslope.cli.uw_wrf import check_layers, registration_error


@click.group()
def main() -> None:
    ...


main.add_command(check_layers)
main.add_command(registration_error)
main.add_command(export_web)
main.add_command(export_loop)
main.add_command(serve)
main.add_command(georegistration)
main.add_command(render_frame)
main.add_command(archive_loop)
main.add_command(archive_run)
main.add_command(archive_check)


if __name__ == "__main__":
    main()
