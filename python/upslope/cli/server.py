import click
import uvicorn


@click.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
def serve(host: str, port: int) -> None:
    """Run the API server and webapp. Exporting runs separately: see `export-loop`."""
    # imported here so other commands don't pay for loading the server
    from upslope.server.app import create_app

    app = create_app()
    # log_config=None keeps uvicorn from replacing our logging setup
    uvicorn.run(app, host=host, port=port, log_config=None)
