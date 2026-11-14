"""Migration 002: replace the old RGB PNG value images with int16 GeoTIFFs on the model's grid.

    just py-migrate m002_int16_values strip --dry-run
    just py-migrate m002_int16_values strip
    just py-migrate m002_int16_values backfill

`strip` removes the old value images from every manifest (and deletes them), so the webapp and the exporter
never see the old format; runs then show no values until `backfill` restores them. It reads nothing upstream
and takes minutes. The new exporter can't read a manifest that still lists old value images, so strip the
runs it follows before it starts (see README.md for the order).

`backfill` recomputes each dynamical.org frame's values from dynamical.org, as the exporter now writes them,
newest runs first, and points the manifest at them. Frames that already have values are skipped, so it can be
stopped and rerun. A layer whose config changed since it was exported is left alone: its images no longer
match the config, so it's the exporter's to redo.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click
from affine import Affine
from attrs import evolve
from pyproj import Transformer

from migrations.common import KeptRun, Summary, common_options, finish, kept_runs, migrate_runs, rewrite_manifest, run_key, start
from upslope.clients.forecasts import MANIFEST, ForecastStore
from upslope.clients.storage import PreconditionFailed
from upslope.logging import get_logger
from upslope.models.config import DynamicalLayerConfig, ModelsConfig
from upslope.models.dynamical.source import DynamicalSource
from upslope.models.manifest import Frame, RunLayer, RunManifest, ValueEncoding, content_digest, lookup_name, values_name
from upslope.raster import LOOKUP_STEP, OUTPUT_CRS, encode_lookup, encode_values

logger = get_logger(__name__)


# --- strip


def strip_run(store: ForecastStore, run: KeptRun, summary: Summary, dry_run: bool) -> None:
    """Remove a run's old value images from its manifest, then delete them."""
    key = f"{store.run_prefix(run.model.id, run.initialized)}/{MANIFEST}"
    stored = store.storage.get_bytes(key)
    if stored is None:
        return  # pruned since it was listed
    # the old format doesn't fit RunManifest any more, so edit the JSON itself
    data = json.loads(stored.data)
    old_keys = []
    changed = False
    for layer in data.get("layers", []):
        values = layer.get("values")
        if values is not None and "lookup" not in values:
            layer["values"] = None
            changed = True
        for frame in layer.get("frames", []):
            if (frame.get("values") or "").endswith(".values.png"):
                old_keys.append(run_key(store, run, frame["values"]))
                frame["values"] = None
    if not old_keys and not changed:
        return
    name = f"{run.model.id} {run.initialized:%Y-%m-%d %HZ}"
    manifest = RunManifest.from_json(json.dumps(data))  # it's the new format now
    if dry_run:
        logger.info("%s: would remove %d value images", name, len(old_keys))
    else:
        try:
            store.write_manifest(manifest, if_match=stored.etag)
        except PreconditionFailed:
            logger.warning("%s changed meanwhile; left as it was", name)
            summary.skipped_changed += 1
            return
        store.storage.delete_keys(old_keys)
        logger.info("%s: removed %d value images", name, len(old_keys))
    summary.runs += 1
    summary.add("value images removed", len(old_keys))


# --- backfill


def display_transform(layer: RunLayer) -> Affine:
    """A layer image's transform (pixel corner -> Web Mercator), from its corners and size in the manifest."""
    to_mercator = Transformer.from_crs("EPSG:4326", OUTPUT_CRS, always_xy=True)
    (left, top), (right, bottom) = (to_mercator.transform(*layer.coordinates[i]) for i in (0, 2))
    width, height = layer.dimensions
    return Affine((right - left) / width, 0.0, left, 0.0, (bottom - top) / height, top)


def backfill_run(
    store: ForecastStore, source: DynamicalSource, run: KeptRun, summary: Summary, pool: ThreadPoolExecutor, dry_run: bool
) -> None:
    """Compute and upload values for a run's frames that have none, and point its manifest at them."""
    model = run.model
    versioned = store.read_manifest_versioned(model.id, run.initialized)
    if versioned is None:
        return
    manifest, etag = versioned
    name = f"{model.id} {run.initialized:%Y-%m-%d %HZ}"
    work: list[tuple[RunLayer, DynamicalLayerConfig, Frame]] = []
    for run_layer in manifest.layers:
        config = model.layer(run_layer.id)
        if not isinstance(config, DynamicalLayerConfig) or config.values is None or run_layer.dimensions is None:
            continue
        # exported before values were stored (or by this migration), with the config as it is now
        if run_layer.fingerprint not in (model.layer_fingerprint(evolve(config, values=None)), model.layer_fingerprint(config)):
            logger.info("%s/%s: config changed since it was exported; left to the exporter", name, run_layer.id)
            summary.add("layers left to the exporter")
            continue
        work.extend((run_layer, config, frame) for frame in run_layer.frames if frame.values is None)
    if not work:
        return
    if dry_run:
        logger.info("%s: would compute values for %d frames", name, len(work))
        summary.runs += 1
        summary.add("frames to backfill", len(work))
        return

    # each layer's lookup first (one per layer), then every frame's values
    uploaded: list[str] = []
    encodings: dict[str, ValueEncoding] = {}
    try:
        for run_layer, config, frame in work:
            if run_layer.id in encodings:
                continue
            if run_layer.values is not None:
                encodings[run_layer.id] = run_layer.values
                continue
            grid = source.frame_values(model, config, run.initialized, frame.hour)
            lookup = encode_lookup(display_transform(run_layer), tuple(run_layer.dimensions), grid.crs, grid.transform)
            path = f"{run_layer.id}/{lookup_name(content_digest(lookup))}"
            store.upload_bytes(model.id, run.initialized, path, lookup, "image/tiff")
            uploaded.append(run_key(store, run, path))
            encodings[run_layer.id] = ValueEncoding(config.values.step, config.values.offset, path, LOOKUP_STEP)
    except Exception:
        store.storage.delete_keys(uploaded)
        raise

    def compute(item: tuple[RunLayer, DynamicalLayerConfig, Frame]) -> tuple[Frame, str]:
        run_layer, config, frame = item
        encoding = encodings[run_layer.id]
        values = source.frame_values(model, config, run.initialized, frame.hour)
        data = encode_values(values.values, encoding.scale, encoding.offset, values.crs, values.transform, values.legend.units)
        path = f"{run_layer.id}/{values_name(frame.valid, content_digest(data))}"
        store.upload_bytes(model.id, run.initialized, path, data, "image/tiff")
        return frame, path

    futures = [pool.submit(compute, item) for item in work]
    done = 0
    for item, future in zip(work, futures):
        try:
            frame, path = future.result()
        except Exception as e:
            # e.g. dynamical.org no longer has the run: the frame keeps showing, without values
            logger.warning("%s/%s hour %d: no values (%s)", name, item[0].id, item[2].hour, e)
            summary.add("frames without values")
            continue
        uploaded.append(run_key(store, run, path))
        frame.values = path
        done += 1
    for run_layer in manifest.layers:
        if run_layer.id in encodings:
            run_layer.values = encodings[run_layer.id]
            run_layer.fingerprint = model.layer_fingerprint(model.layer(run_layer.id))
    if not rewrite_manifest(store, manifest, etag, uploaded):
        summary.skipped_changed += 1
        return
    logger.info("%s: values for %d of %d frames", name, done, len(work))
    summary.runs += 1
    summary.add("frames backfilled", done)


# --- commands


def strip(store: ForecastStore, runs: list[KeptRun], include_followed: bool, dry_run: bool) -> Summary:
    return migrate_runs(store, runs, lambda run, summary: strip_run(store, run, summary, dry_run), include_followed)


def backfill(
    store: ForecastStore, source: DynamicalSource, runs: list[KeptRun], include_followed: bool, dry_run: bool, workers: int
) -> Summary:
    runs = [run for run in runs if run.model.source.kind == "dynamical"]
    source.begin_check()
    try:
        with ThreadPoolExecutor(workers, thread_name_prefix="m002") as pool:
            return migrate_runs(
                store, runs, lambda run, summary: backfill_run(store, source, run, summary, pool, dry_run), include_followed
            )
    finally:
        source.end_check()


@click.group()
def main() -> None:
    """Replace the old PNG value images with int16 GeoTIFFs on the model's grid."""


@main.command("strip")
@common_options
def strip_command(
    config_path: Path, model_ids: tuple[str, ...], include_followed: bool, dry_run: bool, workers: int, yes: bool, verbose: bool
) -> None:
    """Remove the old value images from every manifest, and delete them."""
    from upslope.clients.storage import StorageClient

    start(verbose, include_followed, dry_run, yes, "Remove the old value images")
    store = ForecastStore(StorageClient.build_public())
    finish(strip(store, kept_runs(store, ModelsConfig.load(config_path), model_ids), include_followed, dry_run))


@main.command("backfill")
@common_options
@click.option("--followed-only", is_flag=True, help="Only the runs the exporter follows (with --include-followed): the ones to do before restarting it.")
def backfill_command(
    config_path: Path,
    model_ids: tuple[str, ...],
    include_followed: bool,
    dry_run: bool,
    workers: int,
    yes: bool,
    verbose: bool,
    followed_only: bool,
) -> None:
    """Compute each dynamical.org frame's values from dynamical.org, newest runs first."""
    from upslope.clients.storage import StorageClient

    if followed_only and not include_followed:
        raise click.UsageError("--followed-only needs --include-followed (and the exporter stopped)")
    start(verbose, include_followed, dry_run, yes, "Compute values from dynamical.org and add them to the manifests")
    store = ForecastStore(StorageClient.build_public())
    runs = kept_runs(store, ModelsConfig.load(config_path), model_ids)
    if followed_only:
        runs = [run for run in runs if run.followed]
    finish(backfill(store, DynamicalSource(), runs, include_followed, dry_run, workers))


if __name__ == "__main__":
    main()
