"""Shared steps for one-off migrations of the exported forecasts, run by hand (see README.md).

Migrations share the public bucket with a running exporter, which rewrites the manifests of the runs it follows
(each model's latest complete run onward) and deletes runs past retention. So by default a migration changes
only older runs, which the exporter never writes again, and rewrites each manifest only if it's unchanged since
it was read: a run pruned or rewritten meanwhile is skipped, never recreated or overwritten.
"""

import logging
from collections.abc import Callable, Collection
from datetime import datetime
from pathlib import Path

import click
from attrs import define, field

from upslope.clients.forecasts import ForecastStore
from upslope.clients.storage import PreconditionFailed
from upslope.logging import get_logger
from upslope.models.config import ModelConfig, ModelsConfig
from upslope.models.manifest import RunManifest

logger = get_logger(__name__)


@define(frozen=True)
class KeptRun:
    """A run in storage.

    Attributes:
        model: The run's model.
        initialized: Initialization time (UTC).
        followed: Whether the exporter may still rewrite its manifest: it's the model's latest complete run or
            newer (or the model has no complete run yet).
    """

    model: ModelConfig
    initialized: datetime
    followed: bool


@define
class Summary:
    """What a migration did, for its final report.

    Attributes:
        runs: Runs migrated.
        skipped_followed: Runs left alone because the exporter follows them.
        skipped_changed: Runs left alone because their manifest changed or vanished meanwhile (rerun to finish).
        failed: Runs that failed, with the reason.
        counts: Other totals, by name (e.g. frames converted).
    """

    runs: int = 0
    skipped_followed: int = 0
    skipped_changed: int = 0
    failed: list[str] = field(factory=list)
    counts: dict[str, int] = field(factory=dict)

    def add(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n

    def report(self) -> str:
        parts = [f"{self.runs} runs migrated"] + [f"{n} {name}" for name, n in self.counts.items()]
        if self.skipped_followed:
            parts.append(f"{self.skipped_followed} followed runs skipped (rerun later, or with --include-followed while the exporter is stopped)")
        if self.skipped_changed:
            parts.append(f"{self.skipped_changed} runs changed meanwhile and were skipped (rerun to finish)")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return "; ".join(parts)


def kept_runs(store: ForecastStore, config: ModelsConfig, model_ids: Collection[str] = ()) -> list[KeptRun]:
    """Every run in storage of the configured models (or some), each model's newest first.

    Args:
        store: The exported forecasts.
        config: Models config.
        model_ids: Only these models; all if empty.

    Returns:
        The runs, in config order of their models.
    """
    if unknown := sorted(set(model_ids) - {model.id for model in config.models}):
        raise click.ClickException(f"unknown models {unknown}")
    runs = []
    for model in config.models:
        if model_ids and model.id not in model_ids:
            continue
        latest = store.read_latest(model.id)
        for initialized in store.list_runs(model.id):
            runs.append(KeptRun(model, initialized, latest is None or initialized >= latest.initialized))
    return runs


def rewrite_manifest(store: ForecastStore, manifest: RunManifest, etag: str | None, uploaded: list[str]) -> bool:
    """Write a migrated manifest only if the stored one is unchanged since it was read.

    Args:
        store: The exported forecasts.
        manifest: The migrated manifest.
        etag: The stored manifest's ETag when it was read.
        uploaded: Keys the migration uploaded for this run, deleted again if the write loses.

    Returns:
        Whether it was written.
    """
    try:
        store.write_manifest(manifest, if_match=etag)
        return True
    except PreconditionFailed:
        logger.warning("%s run %s changed meanwhile; left as it was", manifest.model, manifest.path)
        store.storage.delete_keys(uploaded)
        return False


def run_key(store: ForecastStore, run: KeptRun, relative_path: str) -> str:
    """The storage key of a file in a run's folder."""
    return f"{store.run_prefix(run.model.id, run.initialized)}/{relative_path}"


def migrate_runs(
    store: ForecastStore,
    runs: list[KeptRun],
    migrate: Callable[[KeptRun, Summary], None],
    include_followed: bool,
) -> Summary:
    """Apply a migration to each run, skipping followed runs unless asked, and carrying on past failures.

    Args:
        store: The exported forecasts.
        runs: Runs to migrate.
        migrate: Migrates one run; records what it did in the summary.
        include_followed: Also migrate the runs the exporter follows (only with the exporter stopped).

    Returns:
        What was done.
    """
    summary = Summary()
    for run in runs:
        name = f"{run.model.id} {run.initialized:%Y-%m-%d %HZ}"
        if run.followed and not include_followed:
            logger.info("%s: followed by the exporter; skipped", name)
            summary.skipped_followed += 1
            continue
        try:
            migrate(run, summary)
        except Exception as e:
            logger.exception("%s: failed", name)
            summary.failed.append(f"{name}: {e}")
    return summary


def common_options(command: Callable) -> Callable:
    """The options every migration takes."""
    from upslope.cli import DEFAULT_MODELS_CONFIG

    options = [
        click.option("-c", "--config", "config_path", type=click.Path(path_type=Path), default=DEFAULT_MODELS_CONFIG, show_default=True),
        click.option("-m", "--model", "model_ids", multiple=True, help="Only this model (repeatable). Defaults to every model."),
        click.option(
            "--include-followed",
            is_flag=True,
            help="Also migrate the runs the exporter follows. Only with the exporter stopped (fly scale count exporter=0).",
        ),
        click.option("--dry-run", is_flag=True, help="Report what would change without writing anything."),
        click.option("--workers", type=click.IntRange(1, 64), default=8, show_default=True, help="Files converted at once."),
        click.option("--yes", is_flag=True, help="Don't ask for confirmation."),
        click.option("-v", "--verbose", is_flag=True, help="Log per-file detail."),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def start(verbose: bool, include_followed: bool, dry_run: bool, yes: bool, what: str) -> None:
    """Configure logging and confirm before changing production data."""
    from upslope.logging import configure_logging

    configure_logging(verbose)
    if dry_run or yes:
        return
    warning = " INCLUDING the runs the exporter follows: make sure it's stopped" if include_followed else ""
    click.confirm(f"{what} in the configured bucket{warning}. Continue?", abort=True)


def finish(summary: Summary) -> None:
    """Report the outcome; exit non-zero if any run failed."""
    logger.log(logging.WARNING if summary.failed else logging.INFO, "done: %s", summary.report())
    for failure in summary.failed:
        logger.error("  %s", failure)
    if summary.failed:
        raise click.ClickException(f"{len(summary.failed)} runs failed; rerun to retry them")
