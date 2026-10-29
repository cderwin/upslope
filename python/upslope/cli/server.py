import click
import uvicorn


@click.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option(
    "--export-on-startup",
    is_flag=True,
    help="Run the export once at startup (in the background) instead of waiting for the first scheduled check.",
)
@click.option(
    "--export-interval",
    type=click.IntRange(1, 1440),
    default=15,
    show_default=True,
    help="Minutes between checks for new frames, on the clock (e.g. :00, :15, ...).",
)
def serve(host: str, port: int, export_on_startup: bool, export_interval: int) -> None:
    """Run the API server, which also checks for and exports new forecast frames periodically."""
    from datetime import timedelta

    # imported here so other commands don't pay for loading the server
    from upslope.server.app import create_app

    app = create_app(export_on_startup=export_on_startup, export_interval=timedelta(minutes=export_interval))
    # log_config=None keeps uvicorn from replacing our logging setup
    uvicorn.run(app, host=host, port=port, log_config=None)
