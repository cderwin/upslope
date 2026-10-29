"""The export pipeline: download each model's newest complete run, georegister it and upload map-ready frames."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

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
from upslope.models.manifest import Frame, RunLayer, RunManifest, RunPointer, frame_name, valid_time
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
        manifests: Manifests of the models that exported successfully.
    """

    def __init__(self, models: list[str], manifests: list[RunManifest]) -> None:
        super().__init__(f"export failed for {', '.join(models)}")
        self.models = models
        self.manifests = manifests


def run_name(model: ModelConfig, initialized: datetime) -> str:
    return f"{model.id} {initialized:%Y-%m-%d %HZ}"


def reusable_layers(model: ModelConfig, existing: RunManifest | None, revision: int) -> dict[str, RunLayer]:
    """Layers of an exported run that are still what the config and annotations would produce.

    A layer is reusable when it was georegistered with the current annotation revision and its frames
    came from the same URL, hours and maptype (its fingerprint). Its display name may differ.

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
        if exported is not None and exported.fingerprint == layer.fingerprint() and exported.complete:
            reusable[layer.id] = evolve(exported, name=layer.name)
    return reusable


@define
class ForecastDriver:
    """Runs the export pipeline for every configured model: download, georegister, and upload each run.

    Attributes:
        config: Models and layers to export.
        client: Client for downloading forecast images.
        store: Destination for the exported runs.
        annotations: Source of the published georegistration annotations, read at the start of each run.
        now: Clock, for tests.
    """

    config: ModelsConfig
    client: ImageClient
    store: ForecastStore
    annotations: AnnotationStore
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def run(self, force: bool = False) -> list[RunManifest]:
        """Export every configured model's newest complete run, where it isn't exported already.

        A model failing to export doesn't stop the others; the failures are raised together at the end.

        Args:
            force: Export every layer again, even ones exported with the current annotations and config.

        Returns:
            The manifests written, one per model that had something to export. Raises ExportFailed
            if any model failed.
        """
        published = self.annotations.load_published()
        if published is None:
            logger.error("no georegistration annotations have been published; skipping export")
            return []

        registration: Registration | None = None

        def get_registration() -> Registration:
            # fitting is only needed when something is downloaded
            nonlocal registration
            if registration is None:
                registration = Registration.build(published)
            return registration

        manifests = []
        failed: list[str] = []
        for model in self.config.models:
            try:
                manifest = self.run_model(model, published, get_registration, force)
                if manifest is not None:
                    manifests.append(manifest)
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
    ) -> RunManifest | None:
        """Export a model's newest complete run, reusing layers exported with the current annotations and config.

        Points `latest.json` at the run and deletes runs past the model's retention.

        Args:
            model: Model to export.
            published: Published annotations.
            registration: Returns the transforms fitted from `published`.
            force: Export every layer again.

        Returns:
            The manifest written, or None if there was nothing to do.
        """
        initialized = self.client.latest_complete_run(model, self.now())
        if initialized is None:
            logger.warning("%s has no complete run in the last %d hours; skipping", model.id, model.lookback_hours)
            return None

        existing = self.store.read_manifest(model.id, initialized)
        reusable = {} if force else reusable_layers(model, existing, published.revision)
        names_current = existing is not None and [(l.id, l.name) for l in existing.layers] == [(l.id, l.name) for l in model.layers]
        latest = self.store.read_latest(model.id)
        if len(reusable) == len(model.layers) and names_current:
            logger.info("%s already exported with annotations r%d and this config; skipping", run_name(model, initialized), published.revision)
            self._update_latest(existing, latest)
            return None
        if existing is not None:
            stale = [layer.id for layer in model.layers if layer.id not in reusable]
            logger.info(
                "%s was already exported; %s",
                run_name(model, initialized),
                f"exporting {len(stale)} layer(s) again ({', '.join(stale)})" if stale else "updating layer names",
            )

        manifest = self.export_run(model, initialized, published.revision, registration, reusable)
        latest_run = self._update_latest(manifest, latest)
        self.store.prune(model.id, model.retention_days, self.now(), keep={latest_run})
        return manifest

    def _update_latest(self, manifest: RunManifest, latest: RunPointer | None) -> datetime:
        """Point `latest.json` at a complete run, unless it already points at that run or a newer one.

        Returns:
            The initialization time `latest.json` now points at.
        """
        if latest is None or latest.initialized < manifest.initialized:
            self.store.write_latest(RunPointer.to(manifest, self.now()))
            return manifest.initialized
        return latest.initialized

    def export_run(
        self,
        model: ModelConfig,
        initialized: datetime,
        revision: int,
        registration: Callable[[], Registration],
        reusable: dict[str, RunLayer],
    ) -> RunManifest:
        """Export a run's layers in a temporary directory and write its manifest.

        Args:
            model: Model to export.
            initialized: Initialization time of the run.
            revision: Revision of the annotations `registration` fits.
            registration: Returns the fitted transforms to place the images with.
            reusable: Layers already exported that can be kept as they are.

        Returns:
            The manifest written.
        """
        layers = []
        with TemporaryDirectory() as tmp_dir_name:
            work = Path(tmp_dir_name)
            logger.info("exporting %d layers of %s", len(model.layers) - len(reusable), run_name(model, initialized))
            for i, layer in enumerate(model.layers, start=1):
                if layer.id in reusable:
                    layers.append(reusable[layer.id])
                    continue
                name = f"{model.id}/{layer.id}"
                logger.info("[%d/%d] exporting layer %s", i, len(model.layers), name)
                run_layer = self.new_layer(layer)
                with log_duration(logger, f"exported {name}"):
                    for hour in layer.hours.hours:
                        self.export_frame(model, layer, run_layer, initialized, hour, registration(), work)
                layers.append(run_layer)

        manifest = RunManifest(model.id, model.name, initialized, revision, self.now(), layers)
        self.store.write_manifest(manifest)
        logger.info("wrote %s manifest (%d frames)", run_name(model, initialized), sum(len(l.frames) for l in layers))
        return manifest

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
        registration: Registration,
        work: Path,
    ) -> Frame:
        """Download one forecast hour, georegister it, upload it as a PNG and add it to `run_layer`.

        Args:
            model: Model the layer belongs to.
            layer: Layer config.
            run_layer: Layer to add the frame to; sets its dimensions and corners from the first frame.
            initialized: Initialization time of the run.
            hour: Forecast hour.
            registration: Fitted transforms to place the image with.
            work: Scratch directory.

        Returns:
            The frame. Raises httpx.HTTPError if the download fails, and ValueError if the frame's
            size differs from the layer's other frames.
        """
        gif = work / layer.id / f"{hour:03d}.gif"
        gif.parent.mkdir(parents=True, exist_ok=True)
        gif.write_bytes(self.client.download_image(layer, initialized, hour))
        tiff, png = gif.with_suffix(".tiff"), gif.with_suffix(".png")
        registration.georegister(gif, tiff, layer.maptype)

        with rasterio.open(tiff) as dataset:
            size = (dataset.width, dataset.height)
            if run_layer.dimensions is None:
                run_layer.dimensions = size
                run_layer.coordinates = image_coordinates(dataset)
            elif tuple(run_layer.dimensions) != size:
                raise ValueError(f"frame size changed at hour {hour} for layer {model.id}/{layer.id}")
            convert_to_png(dataset, png)

        valid = valid_time(initialized, hour)
        frame = Frame(hour, valid, f"{layer.id}/{frame_name(valid)}")
        self.store.upload_frame(model.id, initialized, frame.path, png)
        run_layer.frames.append(frame)
        for path in (gif, tiff, png):
            path.unlink()
        return frame
