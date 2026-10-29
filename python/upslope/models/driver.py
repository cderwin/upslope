"""The export pipeline: download each model's newest complete run, georegister it and upload map-ready frames."""

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import rasterio
from attrs import define
from PIL import Image
from pyproj import Transformer


from upslope.clients.storage import StorageClient
from upslope.logging import get_logger, log_duration
from upslope.clients.annotations import AnnotationStore
from upslope.models.config import LayerConfig, ModelConfig, ModelsConfig
from upslope.models.image_client import ImageClient, raw_image_name
from upslope.models.manifest import Manifest, ManifestLayer
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

    def __init__(self, models: list[str], manifests: list[Manifest]) -> None:
        super().__init__(f"export failed for {', '.join(models)}")
        self.models = models
        self.manifests = manifests


@define
class ForecastDriver:
    """Runs the export pipeline for every configured model: download, georegister, and export a manifest.

    Attributes:
        config: Models and layers to export.
        client: Client for downloading forecast images.
        storage: Destination for the exported manifests.
        annotations: Source of the published georegistration annotations, read at the start of each run.
    """

    config: ModelsConfig
    client: ImageClient
    storage: StorageClient
    annotations: AnnotationStore

    def georegister_layer(self, layer: LayerConfig, output_directory: Path, registration: Registration) -> None:
        """Georegister every downloaded image of a layer into a GeoTIFF.

        Args:
            layer: Layer to georegister.
            output_directory: Working directory; reads from `raw/<layer>/` and writes to `registered/<layer>/`.
            registration: Fitted transforms to place the images with.
        """
        for raw_image in (output_directory / "raw" / layer.id).glob("*.gif"):
            registered_image_path = (output_directory / "registered" / layer.id / raw_image.name).with_suffix(".tiff")
            registered_image_path.parent.mkdir(exist_ok=True, parents=True)
            logger.debug("georegistering %s/%s", layer.id, raw_image.name)
            registration.georegister(raw_image, registered_image_path, layer.maptype)

    def export_layer(
        self, model: ModelConfig, layer: LayerConfig, output_directory: Path, initialized: datetime
    ) -> ManifestLayer:
        """Convert a layer's registered GeoTIFFs to PNG frames and collect them into a manifest layer.

        Args:
            model: Model the layer belongs to.
            layer: Layer to export.
            output_directory: Working directory; reads from `registered/` and writes to `manifest/`.
            initialized: Model run initialization time.

        Returns:
            The manifest layer with one frame per forecast hour. Raises if any frame is missing.
        """
        manifest_layer = ManifestLayer.new(model.id, layer.id, initialized)
        for hour in layer.hours.hours:
            tiff_path = (output_directory / "registered" / layer.id / raw_image_name(hour)).with_suffix(".tiff")
            if not tiff_path.exists():
                raise RuntimeError(f"registered image does not exist for layer {model.id}/{layer.id} at hour {hour}")

            with rasterio.open(tiff_path) as dataset:
                if manifest_layer.dimensions is None:
                    manifest_layer.dimensions = (dataset.width, dataset.height)
                    manifest_layer.coordinates = image_coordinates(dataset)
                elif manifest_layer.dimensions != (dataset.width, dataset.height):
                    raise ValueError(f"frame size changed at hour {hour} for layer {model.id}/{layer.id}")

                frame_path = output_directory / "manifest" / layer.id / f"hour_{hour:02d}.png"
                frame_path.parent.mkdir(exist_ok=True, parents=True)
                convert_to_png(dataset, frame_path)
                manifest_layer.add_frame(frame_path, hour)

        if manifest_layer.dimensions is None or manifest_layer.coordinates is None:
            raise ValueError(f"layer {model.id}/{layer.id} has no frames")

        return manifest_layer

    def run(self, force: bool = False) -> list[Manifest]:
        """Export every configured model's newest complete run, where it isn't exported already.

        A model failing to export doesn't stop the others; the failures are raised together at the end.

        Args:
            force: Export even if a run was already exported with the current annotations and config.

        Returns:
            The manifests exported, one per model that had something to export. Raises ExportFailed
            if any model failed.
        """
        published = self.annotations.load_published()
        if published is None:
            logger.error("no georegistration annotations have been published; skipping export")
            return []

        manifests = []
        failed: list[str] = []
        registration: Registration | None = None
        for model in self.config.models:
            try:
                initialized = self._run_to_export(model, published, force)
                if initialized is None:
                    continue
                if registration is None:
                    registration = Registration.build(published)
                manifests.append(self.export_run(model, initialized, registration, published.revision))
            except Exception:
                logger.exception("export of %s failed", model.id)
                failed.append(model.id)
        if failed:
            raise ExportFailed(failed, manifests)
        return manifests

    def _run_to_export(self, model: ModelConfig, published: AnnotationDocument, force: bool) -> datetime | None:
        """The model's newest complete run, if it needs exporting with these annotations and this config."""
        initialized = self.client.latest_complete_run(model)
        if initialized is None:
            logger.warning("%s has no complete run in the last %d hours; skipping", model.id, model.lookback_hours)
            return None

        run_name = f"{model.id} {initialized:%Y-%m-%d %HZ}"
        marker = self.storage.model_run_marker(model.id, initialized)
        if marker is None:
            return initialized
        annotations_changed = marker.registration_revision != published.revision
        config_changed = marker.config_fingerprint != model.fingerprint()
        if not (force or annotations_changed or config_changed):
            logger.info("%s already exported with annotations r%d and this config; skipping", run_name, published.revision)
            return None
        reasons = [
            reason
            for reason, applies in (
                (f"annotations changed to r{published.revision}", annotations_changed),
                ("layer config changed", config_changed),
                ("forced", force),
            )
            if applies
        ]
        logger.info("%s was already exported; exporting again (%s)", run_name, ", ".join(reasons))
        return initialized

    def export_run(
        self, model: ModelConfig, initialized: datetime, registration: Registration, registration_revision: int
    ) -> Manifest:
        """Download, georegister and convert every layer of a run in a temporary directory, then upload it.

        Args:
            model: Model to export.
            initialized: Initialization time of the run.
            registration: Fitted transforms to place the images with.
            registration_revision: Revision of the annotations `registration` was fitted from.

        Returns:
            The exported manifest.
        """
        start_ts = datetime.now()
        with TemporaryDirectory() as tmp_dir_name:
            output_directory = Path(tmp_dir_name)
            manifest_layers: list[ManifestLayer] = []
            logger.info("exporting %d layers of %s run %s", len(model.layers), model.id, f"{initialized:%Y-%m-%d %HZ}")
            for i, layer in enumerate(model.layers, start=1):
                name = f"{model.id}/{layer.id}"
                logger.info("[%d/%d] processing layer %s", i, len(model.layers), name)
                with log_duration(logger, f"downloaded {name}"):
                    self.client.download_layer(layer, initialized, output_directory / "raw" / layer.id)
                with log_duration(logger, f"georegistered {name}"):
                    self.georegister_layer(layer, output_directory, registration)
                with log_duration(logger, f"converted {name} to PNG frames"):
                    manifest_layers.append(self.export_layer(model, layer, output_directory, initialized))

            manifest = Manifest(start_ts, datetime.now(), manifest_layers)
            with log_duration(logger, "uploaded manifest"):
                manifest_key = self.storage.export_manifest(manifest)
            self.storage.mark_model_run_exported(model.id, initialized, manifest_key, registration_revision, model.fingerprint())
            return manifest
