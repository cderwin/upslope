"""The export pipeline: follow each model's runs as they're published, rendering and uploading each new frame.

Every check advances each run from the latest complete one onward. A layer resumes after its newest exported
hour and renders hours until one isn't published yet, so a run's frames appear as the model produces them.
Other failures (timeouts, server errors, bad images) are recorded and retried at the next check. Where frames
come from is up to each model's source (`upslope.models.source`); everything else here is the same for all.

Models are checked concurrently, each in its own task: a slow or failing model never holds up the others.
Within a model, runs, layers and hours go in order, since each layer resumes after its newest frame. Storage
and raster work is blocking, so it runs in threads.
"""

import asyncio
import time
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import httpx
import rasterio
from attrs import define, evolve, field

from upslope.clients.forecasts import ForecastStore
from upslope.logging import get_logger, log_context
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
from upslope.models.source import ForecastSource, NotPublished, RenderedFrame, SourceUnavailable
from upslope.raster import convert_to_png, encode_values_png, image_coordinates

logger = get_logger(__name__)

ModelState = Literal["ok", "skipped", "failed"]


def run_name(model: ModelConfig, initialized: datetime) -> str:
    return f"{model.id} {initialized:%Y-%m-%d %HZ}"


def describe(error: BaseException) -> str:
    """A one-line description of an error, for logs and the export status."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, TimeoutError) and not str(error):
        return "timed out"
    return f"{error.__class__.__name__}: {error}" if str(error) else error.__class__.__name__


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


@define
class ModelReport:
    """What one check did for one model.

    Attributes:
        model: Model id.
        state: `ok` (checked, whether or not anything was new); `skipped` (its source can't export this check,
            e.g. no annotations are published); `failed` (the check of the model as a whole failed).
        frames: Frames exported.
        manifests: Manifests written.
        layer_errors: Frames that failed and will be retried at the next check, e.g. `surface-temp hour 3: HTTP 503`.
        error: Why the model failed or was skipped.
        seconds: How long the model took.
    """

    model: str
    state: ModelState = "ok"
    frames: int = 0
    manifests: list[RunManifest] = field(factory=list)
    layer_errors: list[str] = field(factory=list)
    error: str | None = None
    seconds: float = 0.0


@define
class CheckReport:
    """What one check did, per model in config order.

    Attributes:
        models: Each model's report.
    """

    models: list[ModelReport]

    @property
    def manifests(self) -> list[RunManifest]:
        return [manifest for model in self.models for manifest in model.manifests]

    @property
    def failed(self) -> list[str]:
        return [model.model for model in self.models if model.state == "failed"]

    @property
    def frames(self) -> int:
        return sum(model.frames for model in self.models)


@define
class _ModelCheck:
    """One model's state during a check: its frame budget and what it has done."""

    report: ModelReport
    frames_left: int | None


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
        model_timeout: Most seconds one model may take in a check, or None for no limit. A model that takes
            longer is stopped and reported as failed; what it exported so far is kept, and it resumes at the next check.
    """

    config: ModelsConfig
    sources: Mapping[str, ForecastSource]
    store: ForecastStore
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    frames_per_model: int | None = None
    model_timeout: float | None = None

    async def run(self, force: bool = False) -> CheckReport:
        """Check every configured model for new frames and export them, all models at once.

        A model failing doesn't stop the others; each model's outcome is in the report. Models whose source
        can't export this check (e.g. UW WRF before any annotations are published) are skipped.

        Args:
            force: Export every layer of the runs being followed again, even ones already exported with the
                current config and source revision.

        Returns:
            What the check did for each model.
        """
        async with AsyncExitStack() as checks:
            # each kind of source reads its per-check state (annotations, dataset snapshots) once, up front
            unavailable: dict[str, str] = {}
            for kind in dict.fromkeys(model.source.kind for model in self.config.models):
                try:
                    await checks.enter_async_context(self.sources[kind].check())
                except Exception as e:
                    logger.exception("could not start the %s check", kind)
                    unavailable[kind] = f"could not start the check: {describe(e)}"
            reports = await asyncio.gather(
                *(self._check_model(model, force, unavailable.get(model.source.kind)) for model in self.config.models)
            )
        return CheckReport(list(reports))

    async def _check_model(self, model: ModelConfig, force: bool, unavailable: str | None) -> ModelReport:
        """Check one model, turning whatever goes wrong into its report rather than raising."""
        report = ModelReport(model.id)
        start = time.monotonic()
        with log_context(model.id):
            if unavailable is not None:
                report.state, report.error = "failed", unavailable
                return report
            deadline = asyncio.timeout(self.model_timeout)
            try:
                async with deadline:
                    await self.run_model(model, self.sources[model.source.kind], force, report)
            except SourceUnavailable as e:
                report.state, report.error = "skipped", str(e)
                logger.info("skipped: %s", e)
            except TimeoutError as e:
                report.state = "failed"
                if deadline.expired():
                    report.error = f"stopped after {self.model_timeout:.0f}s; resumes at the next check"
                    logger.error("%s", report.error)
                else:  # a timeout from below, e.g. a socket's
                    report.error = describe(e)
                    logger.exception("export failed")
            except Exception as e:
                report.state, report.error = "failed", describe(e)
                logger.exception("export failed")
            report.seconds = time.monotonic() - start
            if report.state == "ok":
                logger.info(
                    "checked in %.1fs: %d frames exported, %d manifests written%s",
                    report.seconds, report.frames, len(report.manifests),
                    f", {len(report.layer_errors)} frames to retry" if report.layer_errors else "",
                )
        return report

    async def run_model(self, model: ModelConfig, source: ForecastSource, force: bool = False, report: ModelReport | None = None) -> list[RunManifest]:
        """Advance every run of a model from its latest complete run onward, oldest first.

        Points `latest.json` at a run once it's complete, `in-progress.json` at the newest run that has
        started, and deletes runs past the model's retention.

        Args:
            model: Model to export.
            source: Where the model's frames come from.
            force: Export every layer again.
            report: Where to record what was done; a new one if omitted.

        Returns:
            The manifests written, oldest run first.
        """
        state = _ModelCheck(report or ModelReport(model.id), self.frames_per_model)
        now = self.now()
        revision = source.revision(model)
        latest = await asyncio.to_thread(self.store.read_latest, model.id)
        # no runs before the newest complete one: they'd never be shown
        floor = latest.initialized if latest is not None else await source.latest_complete_run(model, now)
        runs = sorted(run for run in model.initializations(now) if floor is None or run >= floor)
        previous = await asyncio.to_thread(self.store.read_in_progress, model.id)
        logger.debug(
            "following %d runs from %s", len(runs), "the start" if floor is None else f"{floor:%Y-%m-%d %HZ}"
        )

        newest: InProgress | None = None
        latest_run = latest.initialized if latest is not None else None
        for initialized in runs:
            carried = previous if previous is not None and previous.initialized == initialized else None
            advanced = await self.advance_run(model, source, initialized, revision, state, force, carried)
            if advanced is None:
                continue
            manifest, progress, wrote = advanced
            if wrote:
                state.report.manifests.append(manifest)
            newest = progress
            if manifest.complete and (latest_run is None or latest_run < initialized):
                await asyncio.to_thread(self.store.write_latest, RunPointer.to(manifest, self.now()))
                latest_run = initialized

        if state.frames_left == 0:
            logger.info("exported %d frames this check; continuing at the next", self.frames_per_model)
        if newest is not None:
            await asyncio.to_thread(self.store.write_in_progress, newest)
        if latest_run is not None:
            # keep what the pointers show, even when the model has stopped publishing
            showing = newest or previous
            keep = {latest_run} | ({showing.initialized} if showing is not None else set())
            await asyncio.to_thread(self.store.prune, model.id, model.retention_days, now, keep)
        return state.report.manifests

    async def advance_run(
        self,
        model: ModelConfig,
        source: ForecastSource,
        initialized: datetime,
        revision: int | None,
        state: _ModelCheck,
        force: bool = False,
        previous: InProgress | None = None,
    ) -> tuple[RunManifest, InProgress, bool] | None:
        """Export whatever new frames a run has, writing its manifest after each layer that got any.

        Args:
            model: Model to export.
            source: Where the model's frames come from.
            initialized: Initialization time of the run.
            revision: The source's current revision, recorded in the manifest.
            state: The model's frame budget and report.
            force: Start every layer over.
            previous: The run's progress from the last check, to carry forward when each layer last advanced.

        Returns:
            The manifest, the run's progress, and whether the manifest was written; or None if the run
            hasn't started publishing (nothing is written for it then).
        """
        existing = await asyncio.to_thread(self.store.read_manifest, model.id, initialized)
        reusable = {} if force else reusable_layers(model, existing, revision)
        layers = [reusable.get(layer.id) or self.new_layer(model, layer) for layer in model.layers]
        manifest = RunManifest(model.id, model.name, initialized, revision, self.now(), layers, model.attribution)
        # names, layers or annotations changed, or some layers are starting over
        dirty = existing is None or evolve(existing, updated_at=manifest.updated_at) != manifest
        if existing is not None and len(reusable) < len(model.layers):
            logger.info("%s: re-exporting %d of %d layers", run_name(model, initialized), len(model.layers) - len(reusable), len(model.layers))

        progress = InProgress(model.id, initialized, manifest.path, self.now())
        wrote = False
        with TemporaryDirectory() as tmp_dir_name:
            work = Path(tmp_dir_name)
            for config, run_layer in zip(model.layers, layers):
                before = previous.layer(config.id) if previous is not None else None
                count = len(run_layer.frames)
                progress.layers.append(await self.advance_layer(model, source, config, run_layer, initialized, work, state, before))
                # a new run gets its first manifest once it has a frame
                started = existing is not None or any(layer.frames for layer in layers)
                if len(run_layer.frames) > count or (dirty and started):
                    manifest.updated_at = self.now()
                    await asyncio.to_thread(self.store.write_manifest, manifest)
                    wrote, dirty = True, False

        progress.checked_at = self.now()
        if existing is None and not wrote:
            return None
        if wrote:
            frames = sum(len(layer.frames) for layer in manifest.layers)
            total = sum(len(layer.hours) for layer in manifest.layers)
            logger.info("%s: %d of %d frames%s", run_name(model, initialized), frames, total, " (complete)" if manifest.complete else "")
        return manifest, progress, wrote

    async def advance_layer(
        self,
        model: ModelConfig,
        source: ForecastSource,
        layer: AnyLayerConfig,
        run_layer: RunLayer,
        initialized: datetime,
        work: Path,
        state: _ModelCheck,
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
            state: The model's frame budget and report.
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
            if state.frames_left == 0:
                break
            try:
                await self.export_frame(model, source, layer, run_layer, initialized, hour, work)
            except NotPublished:
                logger.debug("%s hour %d isn't published yet", name, hour)
                break
            except Exception as e:
                # timeouts, half-written images and the like: try again at the next check
                self._record_error(progress, state, name, hour, e)
                break
            progress.available_hour = hour
            progress.progressed_at = self.now()
            state.report.frames += 1
            if state.frames_left is not None:
                state.frames_left -= 1
        if progress.available_hour != available:
            logger.info("%s %s: exported hours %s to %d", name, f"{initialized:%Y-%m-%d %HZ}", "start" if available is None else available, progress.available_hour)
        return progress

    def _record_error(self, progress: LayerProgress, state: _ModelCheck, name: str, hour: int, error: Exception) -> None:
        progress.error = f"hour {hour}: {describe(error)}"
        progress.error_at = self.now()
        state.report.layer_errors.append(f"{progress.id} {progress.error}")
        logger.warning("%s: could not export hour %d (%s); will retry", name, hour, describe(error), exc_info=not isinstance(error, httpx.HTTPError))

    @staticmethod
    def new_layer(model: ModelConfig, layer: AnyLayerConfig) -> RunLayer:
        """An empty run layer for a configured layer."""
        return RunLayer(layer.id, layer.name, layer.maptype, model.layer_fingerprint(layer), layer.hours.hours)

    async def export_frame(
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
        try:
            rendered = await source.render_frame(model, layer, initialized, hour, tiff)
            frame, size, coordinates, encoding = await asyncio.to_thread(
                self._upload_frame, model, layer, run_layer, initialized, hour, tiff, rendered
            )
        finally:
            for path in (tiff, tiff.with_suffix(".png"), tiff.with_suffix(".values.png")):
                path.unlink(missing_ok=True)
        if run_layer.dimensions is None:
            run_layer.dimensions, run_layer.coordinates = size, coordinates
        if frame.values is not None:
            run_layer.values = encoding
        run_layer.legend = rendered.legend
        run_layer.frames.append(frame)
        return frame

    def _upload_frame(
        self,
        model: ModelConfig,
        layer: AnyLayerConfig,
        run_layer: RunLayer,
        initialized: datetime,
        hour: int,
        tiff: Path,
        rendered: RenderedFrame,
    ) -> tuple[Frame, tuple[int, int], list, ValueEncoding]:
        """Convert a rendered GeoTIFF to the frame's PNG (and value image) and upload them (blocking)."""
        png, values_png = tiff.with_suffix(".png"), tiff.with_suffix(".values.png")
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
        return frame, size, coordinates, encoding
