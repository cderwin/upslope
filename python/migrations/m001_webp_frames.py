"""Migration 001: convert every exported PNG frame to a lossless WebP, as the exporter now writes them.

    just py-migrate m001_webp_frames --dry-run
    just py-migrate m001_webp_frames

Pixels are unchanged (lossless, transparency kept). Each frame's WebP is uploaded under its new content-named key,
the run's manifest is rewritten to point at it (only if unchanged meanwhile), and only then are the PNGs deleted,
so a run in storage never lists a missing frame. Runs already converted are skipped, so it can be rerun safely.
"""

import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click
from PIL import Image

from migrations.common import KeptRun, Summary, common_options, finish, kept_runs, migrate_runs, rewrite_manifest, run_key, start
from upslope.clients.forecasts import IMMUTABLE_CACHE_CONTROL, ForecastStore
from upslope.logging import get_logger
from upslope.models.manifest import Frame, content_digest, frame_name
from upslope.raster import save_webp

logger = get_logger(__name__)


def png_to_webp(data: bytes) -> bytes:
    """A PNG frame as the exporter writes frames now: a lossless WebP with the same pixels and transparency."""
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        buffer = io.BytesIO()
        save_webp(image, buffer)
    return buffer.getvalue()


def migrate_run(store: ForecastStore, run: KeptRun, summary: Summary, pool: ThreadPoolExecutor, dry_run: bool) -> None:
    """Convert one run's PNG frames to WebP and point its manifest at them."""
    versioned = store.read_manifest_versioned(run.model.id, run.initialized)
    if versioned is None:
        return  # pruned since it was listed
    manifest, etag = versioned
    frames = [(layer.id, frame) for layer in manifest.layers for frame in layer.frames if frame.path.endswith(".png")]
    if not frames:
        return
    name = f"{run.model.id} {run.initialized:%Y-%m-%d %HZ}"
    if dry_run:
        logger.info("%s: would convert %d frames", name, len(frames))
        summary.runs += 1
        summary.add("frames to convert", len(frames))
        return

    def convert(item: tuple[str, Frame]) -> tuple[Frame, str, int, int]:
        layer_id, frame = item
        stored = store.storage.get_bytes(run_key(store, run, frame.path))
        if stored is None:
            raise FileNotFoundError(f"{frame.path} is listed but missing")
        webp = png_to_webp(stored.data)
        path = f"{layer_id}/{frame_name(frame.valid, content_digest(webp))}"
        store.storage.put_bytes(run_key(store, run, path), webp, "image/webp", cache_control=IMMUTABLE_CACHE_CONTROL)
        return frame, path, len(stored.data), len(webp)

    # wait for every frame, so a failure leaves no upload behind once the run's are cleaned up
    futures = [pool.submit(convert, item) for item in frames]
    results, errors = [], []
    for future in futures:
        try:
            results.append(future.result())
        except Exception as e:
            errors.append(e)
    uploaded = [run_key(store, run, path) for _, path, _, _ in results]
    if errors:
        store.storage.delete_keys(uploaded)
        raise errors[0]
    old_keys = []
    before = after = 0
    for frame, path, png_size, webp_size in results:
        old_keys.append(run_key(store, run, frame.path))
        frame.path = path
        before, after = before + png_size, after + webp_size
    if not rewrite_manifest(store, manifest, etag, uploaded):
        summary.skipped_changed += 1
        return
    store.storage.delete_keys(old_keys)
    logger.info("%s: converted %d frames, %.1f MB -> %.1f MB", name, len(frames), before / 1e6, after / 1e6)
    summary.runs += 1
    summary.add("frames converted", len(frames))
    summary.add("PNG bytes", before)
    summary.add("WebP bytes", after)


def migrate(store: ForecastStore, runs: list[KeptRun], include_followed: bool, dry_run: bool, workers: int) -> Summary:
    """Convert the PNG frames of `runs` (all but the followed ones unless `include_followed`)."""
    with ThreadPoolExecutor(workers, thread_name_prefix="m001") as pool:
        return migrate_runs(
            store, runs, lambda run, summary: migrate_run(store, run, summary, pool, dry_run), include_followed
        )


@click.command()
@common_options
def main(
    config_path: Path, model_ids: tuple[str, ...], include_followed: bool, dry_run: bool, workers: int, yes: bool, verbose: bool
) -> None:
    """Convert every exported PNG frame to a lossless WebP."""
    from upslope.clients.storage import StorageClient
    from upslope.models.config import ModelsConfig

    start(verbose, include_followed, dry_run, yes, "Convert PNG frames to WebP")
    store = ForecastStore(StorageClient.build_public())
    runs = kept_runs(store, ModelsConfig.load(config_path), model_ids)
    finish(migrate(store, runs, include_followed, dry_run, workers))


if __name__ == "__main__":
    main()
