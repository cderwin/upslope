"""The export pipeline: follow each model's runs as they're published, georegistering and uploading each new frame.

Every check advances each run from the latest complete one onward. A layer resumes after its newest exported
hour and downloads hours until one isn't published yet (a 404), so a run's frames appear as the model
produces them. Other failures (timeouts, server errors, bad images) are recorded and retried at the next check.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import numpy as np
import rasterio
from attrs import define, evolve
from PIL import Image
from pyproj import Transformer

from upslope.clients.annotations import AnnotationStore
from upslope.clients.forecasts import ForecastStore
from upslope.logging import get_logger, log_duration
from upslope.models.config import LayerConfig, ModelConfig, ModelsConfig
from upslope.models.image_client import ImageClient
from upslope.models.manifest import (
    Frame,
    InProgress,
    LayerProgress,
    RunLayer,
    RunManifest,
    RunPointer,
    frame_name,
    valid_time,
)
from upslope.models.uw_wrf.georegistration import Registration
from upslope.models.uw_wrf.georegistration_document import AnnotationDocument

logger = get_logger(__name__)


def image_coordinates(dataset: rasterio.DatasetReader) -> list[tuple[float, float]]:
    """Get the geographic coordinates of a raster's four corners.

    Args:
        dataset: Open georeferenced raster.

    Returns:
        (lon, lat) pairs, rounded to 6 places, in order top-left, top-right, bottom-right, bottom-left.
    """
    to_geographic = Transformer.from_crs(dataset.crs, "EPSG:4326", always_xy=True)
    coordinates = []
    for col, row in ((0, 0), (dataset.width, 0), (dataset.width, dataset.height), (0, dataset.height)):
        x, y = dataset.transform * (col, row)
        lon, lat = to_geographic.transform(x, y)
        coordinates.append((round(lon, 6), round(lat, 6)))
    return coordinates


def convert_to_png(dataset: rasterio.DatasetReader, png_path: Path) -> None:
    """Save a raster as a PNG, keeping its palette and making nodata pixels transparent.

    Args:
        dataset: Open raster to convert.
        png_path: Path to write the PNG.
    """
    data = dataset.read()
    try:
        cmap = dataset.colormap(1)
    except ValueError:
        cmap = None

    save_options = {}
    if dataset.count == 1 and dataset.nodata is not None:
        # Warping leaves nodata around the frame; make it see-through on the map.
        save_options["transparency"] = int(dataset.nodata)

    if dataset.count == 1 and cmap is not None:
        # Keep the image paletted: smaller files and exactly the source colors.
        image = Image.fromarray(data[0], mode="P")
        image.putpalette([channel for index in range(256) for channel in cmap.get(index, (0, 0, 0, 255))[:3]])
    elif dataset.count == 1:
        image = Image.fromarray(data[0], mode="L")
    else:
        image = Image.fromarray(np.moveaxis(data[:4], 0, -1))

    image.save(png_path, optimize=True, **save_options)


class ExportFailed(Exception):
    """One or more models failed to export (each failure has been logged).

    Attributes:
        models: IDs of the models that failed.
        manifests: Manifests written for the models that didn't fail.
    """

    def __init__(self, models: list[str], manifests: list[RunManifest]) -> None:
        super().__init__(f"export failed for {', '.join(models)}")
        self.models = models
        self.manifests = manifests


def run_name(model: ModelConfig, initialized: datetime) -> str:
    return f"{model.id} {initialized:%Y-%m-%d %HZ}"


def reusable_layers(model: ModelConfig, existing: RunManifest | None, revision: int) -> dict[str, RunLayer]:
    """Layers of an exported run whose frames are still what the config and annotations would produce.

    A layer is reusable, complete or not, when it was georegistered with the current annotation revision
    and its frames came from the same URL, hours and maptype (its fingerprint). Its display name may differ.

    Args:
        model: The model's current config.
        existing: The run's manifest, if it was exported before.
        revision: Revision of the published annotations.

    Returns:
        Reusable layers by id, with names updated from the config.
    """
    if existing is None or existing.registration_revision != revision:
        return {}
    reusable = {}
    for layer in model.layers:
        exported = existing.layer(layer.id)
        if exported is not None and exported.fingerprint == layer.fingerprint():
            reusable[layer.id] = evolve(exported, name=layer.name)
    return reusable


def _describe(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return f"{error.__class__.__name__}: {error}" if str(error) else error.__class__.__name__


@define
class ForecastDriver:
    """Runs the export pipeline for every configured model.

    Attributes:
        config: Models and layers to export.
        client: Client for downloading forecast images.
        store: Destination for the exported runs.
        annotations: Source of the published georegistration annotations, read at the start of each check.
        now: Clock, for tests.
    """

    config: ModelsConfig
    client: ImageClient
    store: ForecastStore
    annotations: AnnotationStore
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def run(self, force: bool = False) -> list[RunManifest]:
        """Check every configured model for new frames and export them.

        A model failing doesn't stop the others; the failures are raised together at the end.

        Args:
            force: Export every layer of the runs being followed again, even ones already exported with the
                current annotations and config.

        Returns:
            The manifests written. Raises ExportFailed if any model failed.
        """
        published = self.annotations.load_published()
        if published is None:
            logger.error("no georegistration annotations have been published; skipping export")
            return []

        registration: Registration | None = None

        def get_registration() -> Registration:
            # fitting is only needed once something is downloaded
            nonlocal registration
            if registration is None:
                registration = Registration.build(published)
            return registration

        manifests = []
        failed: list[str] = []
        for model in self.config.models:
            try:
                manifests.extend(self.run_model(model, published, get_registration, force))
            except Exception:
                logger.exception("export of %s failed", model.id)
                failed.append(model.id)
        if failed:
            raise ExportFailed(failed, manifests)
        return manifests

    def run_model(
        self,
        model: ModelConfig,
        published: AnnotationDocument,
        registration: Callable[[], Registration],
        force: bool = False,
    ) -> list[RunManifest]:
        """Advance every run of a model from its latest complete run onward, oldest first.

        Points `latest.json` at a run once it's complete, `in-progress.json` at the newest run that has
        started, and deletes runs past the model's retention.

        Args:
            model: Model to export.
            published: Published annotations.
            registration: Returns the transforms fitted from `published`.
            force: Export every layer again.

        Returns:
            The manifests written, oldest run first.
        """
        now = self.now()
        latest = self.store.read_latest(model.id)
        # no runs before the newest complete one: they'd never be shown
        floor = latest.initialized if latest is not None else self.client.latest_complete_run(model, now)
        runs = sorted(run for run in model.initializations(now) if floor is None or run >= floor)
        previous = self.store.read_in_progress(model.id)

        written: list[RunManifest] = []
        newest: InProgress | None = None
        latest_run = latest.initialized if latest is not None else None
        for initialized in runs:
            carried = previous if previous is not None and previous.initialized == initialized else None
            advanced = self.advance_run(model, initialized, published.revision, registration, force, carried)
            if advanced is None:
                continue
            manifest, progress, wrote = advanced
            if wrote:
                written.append(manifest)
            newest = progress
            if manifest.complete and (latest_run is None or latest_run < initialized):
                self.store.write_latest(RunPointer.to(manifest, self.now()))
                latest_run = initialized

        if newest is not None:
            self.store.write_in_progress(newest)
        if latest_run is not None:
            # keep what the pointers show, even when the model has stopped publishing
            showing = newest or previous
            keep = {latest_run} | ({showing.initialized} if showing is not None else set())
            self.store.prune(model.id, model.retention_days, now, keep=keep)
        return written

    def advance_run(
        self,
        model: ModelConfig,
        initialized: datetime,
        revision: int,
        registration: Callable[[], Registration],
        force: bool = False,
        previous: InProgress | None = None,
    ) -> tuple[RunManifest, InProgress, bool] | None:
        """Export whatever new frames a run has, writing its manifest after each layer that got any.

        Args:
            model: Model to export.
            initialized: Initialization time of the run.
            revision: Revision of the annotations `registration` fits.
            registration: Returns the fitted transforms to place the images with.
            force: Start every layer over.
            previous: The run's progress from the last check, to carry forward when each layer last advanced.

        Returns:
            The manifest, the run's progress, and whether the manifest was written; or None if the run
            hasn't started publishing (nothing is written for it then).
        """
        existing = self.store.read_manifest(model.id, initialized)
        reusable = {} if force else reusable_layers(model, existing, revision)
        layers = [reusable.get(layer.id) or self.new_layer(layer) for layer in model.layers]
        manifest = RunManifest(model.id, model.name, initialized, revision, self.now(), layers)
        # names, layers or annotations changed, or some layers are starting over
        dirty = existing is None or evolve(existing, updated_at=manifest.updated_at) != manifest

        progress = InProgress(model.id, initialized, manifest.path, self.now())
        wrote = False
        with TemporaryDirectory() as tmp_dir_name:
            work = Path(tmp_dir_name)
            for config, run_layer in zip(model.layers, layers):
                before = previous.layer(config.id) if previous is not None else None
                count = len(run_layer.frames)
                progress.layers.append(self.advance_layer(model, config, run_layer, initialized, registration, work, before))
                # a new run gets its first manifest once it has a frame
                started = existing is not None or any(layer.frames for layer in layers)
                if len(run_layer.frames) > count or (dirty and started):
                    manifest.updated_at = self.now()
                    self.store.write_manifest(manifest)
                    wrote, dirty = True, False

        progress.checked_at = self.now()
        if existing is None and not wrote:
            return None
        if wrote:
            frames = sum(len(layer.frames) for layer in manifest.layers)
            total = sum(len(layer.hours) for layer in manifest.layers)
            logger.info("%s: %d of %d frames%s", run_name(model, initialized), frames, total, " (complete)" if manifest.complete else "")
        return manifest, progress, wrote

    def advance_layer(
        self,
        model: ModelConfig,
        layer: LayerConfig,
        run_layer: RunLayer,
        initialized: datetime,
        registration: Callable[[], Registration],
        work: Path,
        previous: LayerProgress | None = None,
    ) -> LayerProgress:
        """Export a layer's hours after its newest frame, until one isn't published yet or fails.

        Args:
            model: Model the layer belongs to.
            layer: Layer config.
            run_layer: The layer in the run's manifest; frames are added to it.
            initialized: Initialization time of the run.
            registration: Returns the fitted transforms.
            work: Scratch directory.
            previous: The layer's progress at the last check.

        Returns:
            The layer's progress.
        """
        exported = {frame.hour for frame in run_layer.frames}
        available = run_layer.frames[-1].hour if run_layer.frames else None
        progress = LayerProgress(layer.id, layer.hours.last, available)
        if previous is not None and previous.available_hour == available:
            progress.progressed_at = previous.progressed_at
        name = f"{model.id}/{layer.id}"
        for hour in layer.hours.hours:
            if hour in exported:
                continue
            try:
                self.export_frame(model, layer, run_layer, initialized, hour, registration, work)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.debug("%s hour %d isn't published yet", name, hour)
                    break
                self._record_error(progress, name, hour, e)
                break
            except Exception as e:
                # timeouts, half-written images and the like: try again at the next check
                self._record_error(progress, name, hour, e)
                break
            progress.available_hour = hour
            progress.progressed_at = self.now()
        if progress.available_hour != available:
            logger.info("%s: exported hours %s to %d", name, "start" if available is None else available, progress.available_hour)
        return progress

    def _record_error(self, progress: LayerProgress, name: str, hour: int, error: Exception) -> None:
        progress.error = f"hour {hour}: {_describe(error)}"
        progress.error_at = self.now()
        logger.warning("%s: could not export hour %d (%s); will retry", name, hour, _describe(error), exc_info=not isinstance(error, httpx.HTTPError))

    @staticmethod
    def new_layer(layer: LayerConfig) -> RunLayer:
        """An empty run layer for a configured layer."""
        return RunLayer(layer.id, layer.name, layer.maptype, layer.fingerprint(), layer.hours.hours)

    def export_frame(
        self,
        model: ModelConfig,
        layer: LayerConfig,
        run_layer: RunLayer,
        initialized: datetime,
        hour: int,
        registration: Callable[[], Registration],
        work: Path,
    ) -> Frame:
        """Download one forecast hour, georegister it, upload it as a PNG and add it to `run_layer`.

        Args:
            model: Model the layer belongs to.
            layer: Layer config.
            run_layer: Layer to add the frame to; sets its dimensions and corners from the first frame.
            initialized: Initialization time of the run.
            hour: Forecast hour.
            registration: Returns the fitted transforms to place the image with.
            work: Scratch directory.

        Returns:
            The frame. Raises httpx.HTTPError if the download fails, and ValueError if the frame's
            size differs from the layer's other frames.
        """
        gif = work / layer.id / f"{hour:03d}.gif"
        gif.parent.mkdir(parents=True, exist_ok=True)
        tiff, png = gif.with_suffix(".tiff"), gif.with_suffix(".png")
        try:
            gif.write_bytes(self.client.download_image(layer, initialized, hour))
            registration().georegister(gif, tiff, layer.maptype)

            with rasterio.open(tiff) as dataset:
                size = (dataset.width, dataset.height)
                if run_layer.dimensions is not None and tuple(run_layer.dimensions) != size:
                    raise ValueError(f"frame size changed at hour {hour} for layer {model.id}/{layer.id}")
                coordinates = image_coordinates(dataset)
                convert_to_png(dataset, png)

            valid = valid_time(initialized, hour)
            frame = Frame(hour, valid, f"{layer.id}/{frame_name(valid)}")
            self.store.upload_frame(model.id, initialized, frame.path, png)
        finally:
            for path in (gif, tiff, png):
                path.unlink(missing_ok=True)
        if run_layer.dimensions is None:
            run_layer.dimensions, run_layer.coordinates = size, coordinates
        run_layer.frames.append(frame)
        return frame
