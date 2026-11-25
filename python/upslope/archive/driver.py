"""Archiving runs: for each model, find complete upstream runs it should keep and doesn't have, and archive them.

A model keeps its newest `keep_runs` complete runs. Each check archives at most `runs_per_check` runs per model,
newest first, so a new run is archived promptly and a backlog (a new model, a config change, an outage) fills in
over a few checks. A run archived under an older config (its fingerprint differs) is archived again.

Archiving a run reads each standard variable's upstream fields, standardizes it (`conversions.standardize`) and
writes it to a local Zarr store one variable at a time, with the model's terrain (`archive/terrain.py`), then
uploads the store and lists it in the model's index. A run whose terrain can't be had is archived without it.
All of that is blocking, so each model runs in a thread; models run concurrently, a few at a time.
"""

import asyncio
import shutil
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from attrs import define, field

from upslope.archive.config import ArchiveConfig, ArchiveModel
from upslope.archive.conversions import MissingField, standardize
from upslope.archive.sources.base import ArchiveSource
from upslope.archive.store import ArchivedRun, ArchiveIndex, ArchiveStore, ModelArchiveStatus, run_key
from upslope.archive.terrain import StaticHeights, model_terrain
from upslope.archive.writer import RunWriter
from upslope.logging import get_logger, log_context, log_duration

logger = get_logger(__name__)

# Models archived at once: each holds a variable of a run in memory and keeps a thread busy decoding
MODEL_CONCURRENCY = 2
# Most time one model may take in a check. The thread can't be stopped, so a model still busy when the next check
# starts is skipped until it finishes.
MODEL_TIMEOUT = 40 * 60.0


@define
class RunResult:
    """What archiving one run produced.

    Attributes:
        run: The archived run, as the index lists it.
        missing: Standard variables left out because the upstream doesn't publish their fields.
        dropped: Variable -> upstream lead times left out because they aren't on the model's lead-time axis.
        bytes: Size of the stored run.
        terrain_problem: Why the run has no terrain, if the model has one configured but it couldn't be stored.
    """

    run: ArchivedRun
    missing: list[str]
    dropped: dict[str, list[float]]
    bytes: int
    terrain_problem: str | None = None


def archive_run(
    config: ArchiveConfig,
    model: ArchiveModel,
    source: ArchiveSource,
    initialized: datetime,
    folder: Path,
    now: datetime,
    heights: StaticHeights | None = None,
) -> tuple[RunResult, Path]:
    """Read and standardize one run into a local store (blocking).

    Args:
        config: The archive config.
        model: The model.
        source: Its source.
        initialized: When the run started.
        folder: Folder to write the store in.
        now: When it's archived.
        heights: Open-Meteo's static heights, for models whose terrain comes from there.

    Returns:
        The result (its size not yet known) and the store's folder. Raises MissingField if the source has none of
        the variables, ValueError if a variable doesn't fit its config.
    """
    reader = source.open_run(model, initialized)
    writer: RunWriter | None = None
    written: list[str] = []
    missing: list[str] = []
    dropped: dict[str, list[float]] = {}
    path = folder / run_key(initialized)
    chunks, shards = config.chunking(model)
    try:
        for variable_id, source_field in model.variables.items():
            variable = config.variable(variable_id)
            try:
                fields = [reader.read(name) for name in source_field.fields]
            except MissingField as e:
                logger.warning("%s left out: %s", variable_id, e)
                missing.append(variable_id)
                continue
            try:
                values, left_out = standardize(variable, source_field, fields, reader.grid, model.lead_hours)
            except ValueError as e:
                raise ValueError(f"{variable_id}: {e}") from e
            if left_out:
                dropped[variable_id] = left_out
            if writer is None:
                attributes = {
                    "model": model.id,
                    "source": model.source.describe(),
                    "fingerprint": config.fingerprint(model),
                    "archived_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                }
                if model.attribution:
                    attributes["attribution"] = model.attribution
                writer = RunWriter(path, reader.grid, model.lead_hours, chunks, shards, initialized, attributes)
            writer.write(variable, values, source_field.describe())
            written.append(variable_id)
        if writer is None:
            raise MissingField(f"{model.source.describe()} has none of {model.id}'s variables")
        terrain_problem = None
        if model.terrain is not None:
            try:
                terrain, terrain_source = model_terrain(model, reader, heights)
                writer.write_terrain(terrain, terrain_source)
            except (MissingField, ValueError) as e:
                # the forecast is still worth keeping; point forecasts just can't adjust it to an elevation
                logger.warning("terrain left out: %s", e)
                terrain_problem = str(e)
        writer.finish()
    finally:
        reader.close()
    run = ArchivedRun(initialized, config.fingerprint(model), now, written)
    return RunResult(run, missing, dropped, 0, terrain_problem), path


def _sorted_runs(runs: list[ArchivedRun]) -> list[ArchivedRun]:
    return sorted(runs, key=lambda run: run.init_time, reverse=True)


@define
class ArchiveDriver:
    """Archives every model's runs (see the module docstring).

    Attributes:
        config: The archive config.
        sources: Source of each kind (`dynamical`, `openmeteo`).
        store: The archive in the bucket.
        heights: Open-Meteo's static surface heights, for models whose terrain comes from there.
        concurrency: Models archived at once.
        model_timeout: Most seconds a model may take in a check; None for no limit.
        now: Clock, for tests.
    """

    config: ArchiveConfig
    sources: dict[str, ArchiveSource]
    store: ArchiveStore
    heights: StaticHeights | None = None
    concurrency: int = MODEL_CONCURRENCY
    model_timeout: float | None = MODEL_TIMEOUT
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    # models whose archive thread is still running (possibly from a check that timed out)
    _busy: set[str] = field(factory=set, init=False)

    async def run(self, models: list[str] | None = None, force: bool = False) -> list[ModelArchiveStatus]:
        """Run one check: archive what each model is missing.

        Args:
            models: Model ids to check; None for every model.
            force: Archive the newest runs again even if they're archived under the current config.

        Returns:
            Each model's outcome.
        """
        selected = [m for m in self.config.models if models is None or m.id in models]
        limit = asyncio.Semaphore(self.concurrency)

        async def one(model: ArchiveModel) -> ModelArchiveStatus:
            async with limit:
                with log_context(model.id):
                    return await self._run_model(model, force)

        try:
            return list(await asyncio.gather(*(one(model) for model in selected)))
        finally:
            for source in self.sources.values():
                await asyncio.to_thread(source.end_check)

    async def _run_model(self, model: ArchiveModel, force: bool) -> ModelArchiveStatus:
        if model.id in self._busy:
            logger.warning("still archiving from an earlier check; skipped")
            return ModelArchiveStatus(model.id, "skipped", error="still archiving from an earlier check")
        start = time.monotonic()
        self._busy.add(model.id)
        task = asyncio.ensure_future(asyncio.to_thread(self.archive_model, model, force))
        task.add_done_callback(lambda _: self._busy.discard(model.id))
        try:
            status = await asyncio.wait_for(asyncio.shield(task), self.model_timeout)
        except TimeoutError:
            error = f"still archiving after {self.model_timeout:.0f}s; it finishes in the background"
            logger.error("%s", error)
            return ModelArchiveStatus(model.id, "failed", error=error, seconds=round(time.monotonic() - start, 1))
        except Exception as e:
            logger.exception("archiving failed")
            return ModelArchiveStatus(model.id, "failed", error=f"{type(e).__name__}: {e}", seconds=round(time.monotonic() - start, 1))
        status.seconds = round(time.monotonic() - start, 1)
        return status

    def archive_model(self, model: ArchiveModel, force: bool = False) -> ModelArchiveStatus:
        """Archive the runs a model is missing (blocking). Raises if reading, writing or uploading fails.

        Args:
            model: The model.
            force: Archive its newest runs again even if they're up to date.

        Returns:
            The model's outcome.
        """
        source = self.sources[model.source.kind]
        index = self.store.read_index(model.id)
        fingerprint = self.config.fingerprint(model)
        keep = self.config.keep_runs(model)
        per_check = self.config.defaults.runs_per_check
        archived: list[str] = []
        warnings: list[str] = []
        considered = 0
        for initialized in model.initializations(self.now()):
            if considered >= keep or len(archived) >= per_check:
                break
            existing = index.run(initialized)
            if existing is not None and existing.fingerprint == fingerprint and not force:
                considered += 1
                continue
            if not source.is_complete(model, initialized):
                continue
            if existing is not None:
                # stop listing it before its objects are replaced
                index.runs.remove(existing)
                self._write_index(index)
            result = self._archive_one(model, source, initialized)
            if result.missing:
                warnings.append(f"{initialized:%Y-%m-%d %HZ}: no upstream data for {', '.join(result.missing)}")
            for variable_id, leads in result.dropped.items():
                warnings.append(f"{initialized:%Y-%m-%d %HZ}: {variable_id} lead times {leads} aren't on the lead axis")
            if result.terrain_problem:
                warnings.append(f"{initialized:%Y-%m-%d %HZ}: no terrain: {result.terrain_problem}")
            index.runs = _sorted_runs([*index.runs, result.run])
            self._prune(model, index, keep)
            archived.append(f"{initialized:%Y%m%dT%H}")
            considered += 1
            force = False  # only the newest run is forced; older ones are archived again only if outdated
        self._remove_strays(model, index)
        if not archived:
            logger.info("nothing new to archive")
        return ModelArchiveStatus(model.id, "ok", runs=archived, warnings=warnings)

    def _archive_one(self, model: ArchiveModel, source: ArchiveSource, initialized: datetime) -> RunResult:
        folder = Path(tempfile.mkdtemp(prefix="upslope-archive-"))
        try:
            with log_duration(logger, f"archived {initialized:%Y-%m-%d %HZ}"):
                result, path = archive_run(self.config, model, source, initialized, folder, self.now(), self.heights)
                result.bytes = self.store.upload_run(model.id, initialized, path)
            logger.info(
                "archived %s: %d variables, %.1f MB", f"{initialized:%Y-%m-%d %HZ}", len(result.run.variables), result.bytes / 1e6
            )
            return result
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _write_index(self, index: ArchiveIndex) -> None:
        index.updated_at = self.now()
        self.store.write_index(index)

    def _prune(self, model: ArchiveModel, index: ArchiveIndex, keep: int) -> None:
        """List the new run and drop the oldest beyond `keep`; they're deleted once the index stops listing them."""
        removed = index.runs[keep:]
        index.runs = index.runs[:keep]
        self._write_index(index)
        for run in removed:
            logger.info("removing %s", f"{run.init_time:%Y-%m-%d %HZ}")
            self.store.delete_run(model.id, run.init_time)

    def _remove_strays(self, model: ArchiveModel, index: ArchiveIndex) -> None:
        """Delete runs in the bucket the index doesn't list (left by an archive that stopped part way)."""
        for initialized in self.store.list_run_folders(model.id):
            if index.run(initialized) is None:
                logger.info("removing unlisted run %s", f"{initialized:%Y-%m-%d %HZ}")
                self.store.delete_run(model.id, initialized)
