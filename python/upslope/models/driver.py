"""The export pipeline: follow each model's runs as they're published, rendering and uploading each new frame.

Every check advances each run from the latest complete one onward. A layer resumes after its newest exported
hour and renders hours until one isn't published yet, so a run's frames appear as the model produces them.
Other failures (timeouts, server errors, bad images) are recorded and retried at the next check. Where frames
come from is up to each model's source (`upslope.models.source`); everything else here is the same for all.
"""

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import rasterio
from attrs import define, evolve, field

from upslope.clients.forecasts import ForecastStore
from upslope.logging import get_logger
from upslope.models.config import AnyLayerConfig, ModelConfig, ModelsConfig
from upslope.models.manifest import (
    Frame,
    InProgress,
    LayerProgress,
    RunLayer,
    RunManifest,
    RunPointer,
    ValueEncoding,
    frame_name,
    valid_time,
    values_name,
)
from upslope.models.source import ForecastSource, NotPublished, SourceUnavailable
from upslope.raster import convert_to_png, encode_values_png, image_coordinates

logger = get_logger(__name__)


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


def reusable_layers(model: ModelConfig, existing: RunManifest | None, revision: int | None) -> dict[str, RunLayer]:
    """Layers of an exported run whose frames are still what the config and source would produce.

    A layer is reusable, complete or not, when it was exported at the source's current revision (for UW
    WRF, the annotation revision) and its frames came from the same config (its fingerprint). Its display
    name may differ.

    Args:
        model: The model's current config.
        existing: The run's manifest, if it was exported before.
        revision: The source's current revision (`ForecastSource.revision`).

    Returns:
        Reusable layers by id, with names updated from the config.
    """
    if existing is None or existing.registration_revision != revision:
        return {}
    reusable = {}
    for layer in model.layers:
        exported = existing.layer(layer.id)
        if exported is not None and exported.fingerprint == model.layer_fingerprint(layer):
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
        sources: Source for each kind of model (`ModelConfig.source.kind`).
        store: Destination for the exported runs.
        now: Clock, for tests.
        frames_per_model: Most frames to export per model in one check, or None for no limit. A backlog (a new
            model, a config change, an outage) then takes several checks, while every model keeps advancing.
    """

    config: ModelsConfig
    sources: Mapping[str, ForecastSource]
    store: ForecastStore
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    frames_per_model: int | None = None
    _frames_left: int | None = field(default=None, init=False)

    def run(self, force: bool = False) -> list[RunManifest]:
        """Check every configured model for new frames and export them.

        A model failing doesn't stop the others; the failures are raised together at the end. Models whose
        source can't export this check (e.g. UW WRF before any annotations are published) are skipped.

        Args:
            force: Export every layer of the runs being followed again, even ones already exported with the
                current config and source revision.

        Returns:
            The manifests written. Raises ExportFailed if any model failed.
        """
        manifests = []
        failed: list[str] = []
        with ExitStack() as checks:
            kinds = dict.fromkeys(model.source.kind for model in self.config.models)
            for kind in kinds:
                checks.enter_context(self.sources[kind].check())
            for model in self.config.models:
                try:
                    manifests.extend(self.run_model(model, self.sources[model.source.kind], force))
                except SourceUnavailable as e:
                    logger.debug("skipping %s: %s", model.id, e)
                except Exception:
                    logger.exception("export of %s failed", model.id)
                    failed.append(model.id)
        if failed:
            raise ExportFailed(failed, manifests)
        return manifests

    def run_model(self, model: ModelConfig, source: ForecastSource, force: bool = False) -> list[RunManifest]:
        """Advance every run of a model from its latest complete run onward, oldest first.

        Points `latest.json` at a run once it's complete, `in-progress.json` at the newest run that has
        started, and deletes runs past the model's retention.

        Args:
            model: Model to export.
            source: Where the model's frames come from.
            force: Export every layer again.

        Returns:
            The manifests written, oldest run first.
        """
        now = self.now()
        self._frames_left = self.frames_per_model
        revision = source.revision(model)
        latest = self.store.read_latest(model.id)
        # no runs before the newest complete one: they'd never be shown
        floor = latest.initialized if latest is not None else source.latest_complete_run(model, now)
        runs = sorted(run for run in model.initializations(now) if floor is None or run >= floor)
        previous = self.store.read_in_progress(model.id)

        written: list[RunManifest] = []
        newest: InProgress | None = None
        latest_run = latest.initialized if latest is not None else None
        for initialized in runs:
            carried = previous if previous is not None and previous.initialized == initialized else None
            advanced = self.advance_run(model, source, initialized, revision, force, carried)
            if advanced is None:
                continue
            manifest, progress, wrote = advanced
            if wrote:
                written.append(manifest)
            newest = progress
            if manifest.complete and (latest_run is None or latest_run < initialized):
                self.store.write_latest(RunPointer.to(manifest, self.now()))
                latest_run = initialized

        if self._frames_left == 0:
            logger.info("%s: exported %d frames this check; continuing at the next", model.id, self.frames_per_model)
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
        source: ForecastSource,
        initialized: datetime,
        revision: int | None,
        force: bool = False,
        previous: InProgress | None = None,
    ) -> tuple[RunManifest, InProgress, bool] | None:
        """Export whatever new frames a run has, writing its manifest after each layer that got any.

        Args:
            model: Model to export.
            source: Where the model's frames come from.
            initialized: Initialization time of the run.
            revision: The source's current revision, recorded in the manifest.
            force: Start every layer over.
            previous: The run's progress from the last check, to carry forward when each layer last advanced.

        Returns:
            The manifest, the run's progress, and whether the manifest was written; or None if the run
            hasn't started publishing (nothing is written for it then).
        """
        existing = self.store.read_manifest(model.id, initialized)
        reusable = {} if force else reusable_layers(model, existing, revision)
        layers = [reusable.get(layer.id) or self.new_layer(model, layer) for layer in model.layers]
        manifest = RunManifest(model.id, model.name, initialized, revision, self.now(), layers, model.attribution)
        # names, layers or annotations changed, or some layers are starting over
        dirty = existing is None or evolve(existing, updated_at=manifest.updated_at) != manifest

        progress = InProgress(model.id, initialized, manifest.path, self.now())
        wrote = False
        with TemporaryDirectory() as tmp_dir_name:
            work = Path(tmp_dir_name)
            for config, run_layer in zip(model.layers, layers):
                before = previous.layer(config.id) if previous is not None else None
                count = len(run_layer.frames)
                progress.layers.append(self.advance_layer(model, source, config, run_layer, initialized, work, before))
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
        source: ForecastSource,
        layer: AnyLayerConfig,
        run_layer: RunLayer,
        initialized: datetime,
        work: Path,
        previous: LayerProgress | None = None,
    ) -> LayerProgress:
        """Export a layer's hours after its newest frame, until one isn't published yet or fails.

        Args:
            model: Model the layer belongs to.
            source: Where the model's frames come from.
            layer: Layer config.
            run_layer: The layer in the run's manifest; frames are added to it.
            initialized: Initialization time of the run.
            work: Scratch directory.
            previous: The layer's progress at the last check.

        Returns:
            The layer's progress.
        """
        exported = {frame.hour for frame in run_layer.frames}
        available = max((frame.hour for frame in run_layer.frames), default=None)
        progress = LayerProgress(layer.id, layer.hours.last, available)
        if previous is not None and previous.available_hour == available:
            progress.progressed_at = previous.progressed_at
        name = f"{model.id}/{layer.id}"
        for hour in layer.hours.hours:
            if hour in exported:
                continue
            if self._frames_left == 0:
                break
            try:
                self.export_frame(model, source, layer, run_layer, initialized, hour, work)
            except NotPublished:
                logger.debug("%s hour %d isn't published yet", name, hour)
                break
            except Exception as e:
                # timeouts, half-written images and the like: try again at the next check
                self._record_error(progress, name, hour, e)
                break
            progress.available_hour = hour
            progress.progressed_at = self.now()
            if self._frames_left is not None:
                self._frames_left -= 1
        if progress.available_hour != available:
            logger.info("%s: exported hours %s to %d", name, "start" if available is None else available, progress.available_hour)
        return progress

    def _record_error(self, progress: LayerProgress, name: str, hour: int, error: Exception) -> None:
        progress.error = f"hour {hour}: {_describe(error)}"
        progress.error_at = self.now()
        logger.warning("%s: could not export hour %d (%s); will retry", name, hour, _describe(error), exc_info=not isinstance(error, httpx.HTTPError))

    @staticmethod
    def new_layer(model: ModelConfig, layer: AnyLayerConfig) -> RunLayer:
        """An empty run layer for a configured layer."""
        return RunLayer(layer.id, layer.name, layer.maptype, model.layer_fingerprint(layer), layer.hours.hours)

    def export_frame(
        self,
        model: ModelConfig,
        source: ForecastSource,
        layer: AnyLayerConfig,
        run_layer: RunLayer,
        initialized: datetime,
        hour: int,
        work: Path,
    ) -> Frame:
        """Render one forecast hour, upload it as a PNG (and its value image, if any) and add it to `run_layer`.

        Args:
            model: Model the layer belongs to.
            source: Where the model's frames come from.
            layer: Layer config.
            run_layer: Layer to add the frame to; sets its dimensions and corners from the first frame.
            initialized: Initialization time of the run.
            hour: Forecast hour.
            work: Scratch directory.

        Returns:
            The frame. Raises NotPublished if the hour isn't available yet, whatever the source raises if
            rendering fails, and ValueError if the frame's size differs from the layer's other frames.
        """
        tiff = work / layer.id / f"{hour:03d}.tiff"
        tiff.parent.mkdir(parents=True, exist_ok=True)
        png, values_png = tiff.with_suffix(".png"), tiff.with_suffix(".values.png")
        try:
            rendered = source.render_frame(model, layer, initialized, hour, tiff)

            with rasterio.open(tiff) as dataset:
                size = (dataset.width, dataset.height)
                if run_layer.dimensions is not None and tuple(run_layer.dimensions) != size:
                    raise ValueError(f"frame size changed at hour {hour} for layer {model.id}/{layer.id}")
                coordinates = image_coordinates(dataset)
                convert_to_png(dataset, png)

            valid = valid_time(initialized, hour)
            frame = Frame(hour, valid, f"{layer.id}/{frame_name(valid)}")
            encoding = run_layer.values or ValueEncoding()
            if rendered.values is not None:
                if rendered.values.shape != (size[1], size[0]):
                    raise ValueError(f"values of {model.id}/{layer.id} hour {hour} don't match the image's grid")
                encode_values_png(rendered.values, values_png, encoding.scale, encoding.offset)
                frame.values = f"{layer.id}/{values_name(valid)}"
                self.store.upload_frame(model.id, initialized, frame.values, values_png)
            self.store.upload_frame(model.id, initialized, frame.path, png)
        finally:
            for path in (tiff, png, values_png):
                path.unlink(missing_ok=True)
        if run_layer.dimensions is None:
            run_layer.dimensions, run_layer.coordinates = size, coordinates
        if frame.values is not None:
            run_layer.values = encoding
        run_layer.legend = rendered.legend
        run_layer.frames.append(frame)
        return frame
