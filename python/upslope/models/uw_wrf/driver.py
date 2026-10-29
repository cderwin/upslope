from datetime import datetime, UTC, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

import numpy as np
import rasterio
from attrs import define
from PIL import Image
from pyproj import Transformer


from upslope.clients.storage import StorageClient
from upslope.logging import get_logger, log_duration
from upslope.models.manifest import Frame, Manifest, ManifestLayer
from upslope.clients.annotations import AnnotationStore
from upslope.models.uw_wrf.georegistration import Registration
from upslope.models.uw_wrf.wrf_client import RUN_LOOKBACK, WrfConfig, WrfClient, LayerConfig

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


@define
class WrfDriver:
    """Runs the UW WRF pipeline: download, georegister, and export each layer to a manifest.

    Attributes:
        config: Layers to process and how to fetch them.
        client: Client for downloading forecast images.
        storage: Destination for the exported manifest.
        annotations: Source of the published georegistration annotations, read at the start of each run.
    """

    config: WrfConfig
    client: WrfClient
    storage: StorageClient
    annotations: AnnotationStore

    model: ClassVar[str] = "uwwrf"

    def georegister_layer(self, layer: LayerConfig, output_directory: Path, registration: Registration) -> None:
        """Georegister every downloaded GIF for a layer into a GeoTIFF.

        Args:
            layer: Layer to georegister.
            output_directory: Working directory; reads from `raw/` and writes to `registered/`.
            registration: Fitted transforms to place the images with.
        """
        raw_images = (output_directory / "raw" / layer.domain / layer.variable).glob("*.gif")
        for raw_image in raw_images:
            registered_image_path = (
                output_directory / "registered" / raw_image.relative_to(output_directory / "raw")
            ).with_suffix(".tiff")
            registered_image_path.parent.mkdir(exist_ok=True, parents=True)
            logger.debug("georegistering %s", raw_image.name)
            registration.georegister(raw_image, registered_image_path, layer.maptype)

    def export_layer(self, layer: LayerConfig, output_directory: Path, initialized: datetime) -> ManifestLayer:
        """Convert a layer's registered GeoTIFFs to PNG frames and collect them into a manifest layer.

        Args:
            layer: Layer to export.
            output_directory: Working directory; reads from `registered/` and writes to `manifest/`.
            initialized: Model run initialization time.

        Returns:
            The manifest layer with one frame per forecast hour. Raises if any frame is missing.
        """
        manifest_layer = ManifestLayer.new(f"{self.model}_{layer.domain}", layer.variable, initialized)
        for hour in self.client.config.get_forecast_hours(layer.forecast_schedule):
            tiff_path = output_directory / "registered" / layer.domain / layer.variable / f"hour{hour:02d}.tiff"
            if not tiff_path.exists():
                raise RuntimeError(f"registered image does not exist for layer {layer.domain}-{layer.variable} at hour {hour}")

            with rasterio.open(tiff_path) as dataset:
                if manifest_layer.dimensions is None:
                    manifest_layer.dimensions = (dataset.width, dataset.height)
                    manifest_layer.coordinates = image_coordinates(dataset)
                elif manifest_layer.dimensions != (dataset.width, dataset.height):
                    raise ValueError(f"frame size changed at hour {hour} for layer {layer.domain}-{layer.variable}")

                frame_path = output_directory / "manifest" / layer.domain / layer.variable / f"hour_{hour:02d}.png"
                frame_path.parent.mkdir(exist_ok=True, parents=True)
                convert_to_png(dataset, frame_path)
                manifest_layer.add_frame(frame_path, hour)

        if manifest_layer.dimensions is None or manifest_layer.coordinates is None:
            raise ValueError(f"layer {layer.domain}-{layer.variable} has no frames")

        return manifest_layer

    def run(self, force: bool = False) -> Manifest | None:
        """Process every configured layer in a temporary directory and export the resulting manifest.

        Exports the newest run UW has finished publishing. A run is exported again when the published
        annotations or the layer config have changed since it was last exported, so a publish or a deploy
        takes effect at the next run of the export.

        Args:
            force: Export even if the run was already exported with the current annotations and config.

        Returns:
            The exported manifest, or None if there was nothing to do (already exported, no complete
            run, or no annotations published).
        """
        start_ts = datetime.now()
        published = self.annotations.load_published()
        if published is None:
            logger.error("no georegistration annotations have been published; skipping export")
            return None

        initialized = self.client.latest_complete_run()
        if initialized is None:
            logger.warning("UW has no complete model run in the last %d runs; skipping export", RUN_LOOKBACK)
            return None

        run_name = f"{initialized:%Y-%m-%d %HZ}"
        fingerprint = self.config.fingerprint()
        marker = self.storage.model_run_marker(self.model, initialized)
        if marker is not None:
            annotations_changed = marker.registration_revision != published.revision
            config_changed = marker.config_fingerprint != fingerprint
            if not (force or annotations_changed or config_changed):
                logger.info("model run %s already exported with annotations r%d and this config; skipping", run_name, published.revision)
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
            logger.info("model run %s was already exported; exporting again (%s)", run_name, ", ".join(reasons))
        registration = Registration.build(published)

        with TemporaryDirectory() as tmp_dir_name:
            output_directory = Path(tmp_dir_name)
            manifest_layers: list[ManifestLayer] = []
            logger.info("exporting %d layers for model run initialized %s", len(self.config.layers), run_name)
            for i, layer in enumerate(self.config.layers, start=1):
                name = f"{layer.domain}-{layer.variable}"
                logger.info("[%d/%d] processing layer %s", i, len(self.config.layers), name)
                with log_duration(logger, f"downloaded {name}"):
                    self.client.download_layer(layer, output_directory / "raw", initialized)
                with log_duration(logger, f"georegistered {name}"):
                    self.georegister_layer(layer, output_directory, registration)
                with log_duration(logger, f"converted {name} to PNG frames"):
                    manifest_layers.append(self.export_layer(layer, output_directory, initialized))

            manifest = Manifest(start_ts, datetime.now(), manifest_layers)
            with log_duration(logger, "uploaded manifest"):
                manifest_key = self.storage.export_manifest(manifest)
            self.storage.mark_model_run_exported(self.model, initialized, manifest_key, published.revision, fingerprint)
            return manifest
