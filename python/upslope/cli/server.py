import click
import uvicorn


@click.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option(
    "--export-on-startup",
    is_flag=True,
    help="Run the WRF export once at startup (in the background) instead of waiting for the next hour.",
)
def serve(host: str, port: int, export_on_startup: bool) -> None:
    """Run the API server, which also runs the WRF export every hour."""
    # imported here so other commands don't pay for loading the server
    from upslope.server.app import create_app

    app = create_app(export_on_startup=export_on_startup)
    # log_config=None keeps uvicorn from replacing our logging setup
    uvicorn.run(app, host=host, port=port, log_config=None)
