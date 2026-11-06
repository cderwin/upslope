"""`upslope georegistration`: move annotations between a snapshot directory and storage, and seed new maptypes."""

import shutil
from pathlib import Path

import click

from upslope.cli import DEFAULT_MODELS_CONFIG
from upslope.clients.annotations import AnnotationStore, iter_images, read_snapshot, snapshot_image_path
from upslope.clients.storage import StorageClient


@click.group()
def georegistration() -> None:
    """Manage the georegistration annotations in storage."""


@georegistration.command()
@click.argument("directory", type=click.Path(path_type=Path, file_okay=False, exists=True))
@click.option("--force", is_flag=True, help="Replace the draft and published annotations if storage already has a draft.")
def upload(directory: Path, force: bool) -> None:
    """Upload a snapshot DIRECTORY as both the draft and the published annotations.

    Does nothing if storage already has a draft, unless --force is given, so it is safe to run on
    every start of a local stack.
    """
    document = read_snapshot(directory)
    store = AnnotationStore(StorageClient.build())

    existing = store.load_draft()
    if existing is not None and not force:
        click.echo(f"storage already has draft annotations (r{existing[0].revision}); not uploading")
        return

    # keep revisions increasing, so stale editors and the export notice the change
    published = store.load_published()
    floor = max(existing[0].revision if existing else 0, published.revision if published else 0)
    document.revision = max(document.revision, floor + 1)

    for maptype, image_id in iter_images(document):
        store.put_image(maptype, image_id, snapshot_image_path(directory, maptype, image_id).read_bytes())
    store.publish(document, note=f"uploaded from {directory.name}")
    if existing is None:
        store.save_draft(document, create=True)
    else:
        store.save_draft(document, if_match=existing[1])
    images = sum(1 for _ in iter_images(document))
    click.echo(f"uploaded r{document.revision}: {len(document.geographic_points)} points, {images} images")


@georegistration.command()
@click.argument("directory", type=click.Path(path_type=Path, file_okay=False))
def snapshot(directory: Path) -> None:
    """Download the published annotations and their images into DIRECTORY."""
    store = AnnotationStore(StorageClient.build())
    document = store.load_published()
    if document is None:
        raise click.ClickException("no published annotations in storage")

    images_dir = directory / "images"
    if images_dir.exists():
        shutil.rmtree(images_dir)
    for maptype, image_id in iter_images(document):
        data = store.read_image(maptype, image_id)
        if data is None:
            raise click.ClickException(f"image missing from storage: {maptype}/{image_id}")
        path = snapshot_image_path(directory, maptype, image_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (directory / "document.json").write_bytes(document.to_json())
    click.echo(f"wrote r{document.revision} to {directory}")


@georegistration.command("promote-examples")
@click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=DEFAULT_MODELS_CONFIG)
@click.option("--hour", default=6, show_default=True, help="Forecast hour to promote, if the layer has it (else its first hour).")
@click.option("--dry-run", is_flag=True, help="List the frames that would be promoted without downloading them.")
def promote_examples(config_path: Path, hour: int, dry_run: bool) -> None:
    """Add an image to annotate to the draft, for each configured maptype that has none.

    Each image is a frame of the newest complete run of the first layer using the maptype. Annotate
    it in the admin UI, then publish. Maptypes that already have an image are left alone.
    """
    import asyncio

    from upslope.annotation_service import AnnotationError, AnnotationService
    from upslope.models.config import ModelsConfig

    service = AnnotationService(AnnotationStore(StorageClient.build()), ModelsConfig.load(config_path))

    async def promote() -> list[str]:
        frames = service.example_frames((await service.draft()).document, preferred_hour=hour)
        if not frames:
            click.echo("every configured maptype has an image")
        failed = []
        for frame in frames:
            name = f"{frame.maptype}: {frame.model}/{frame.variable} hour {frame.hour}"
            if dry_run:
                click.echo(f"would promote {name}")
                continue
            try:
                result = await service.promote(frame.model, frame.variable, frame.hour)
            except AnnotationError as e:
                click.echo(f"could not promote {name}: {e}", err=True)
                failed.append(frame.maptype)
                continue
            click.echo(f"promoted {name} as {result.image} (draft r{result.draft.document.revision})")
        return failed

    if failed := asyncio.run(promote()):
        raise click.ClickException(f"could not promote an image for {', '.join(failed)}")
