"""Exported forecast runs in the public bucket, laid out like open-meteo's `data_spatial/`.

```
forecasts/export-status.json                          the exporter's health (ExportStatus)
forecasts/index.json                                  every model's pointers and kept runs, for /fx/status (ForecastIndex)
forecasts/<model>/latest.json                         newest complete run (RunPointer)
forecasts/<model>/in-progress.json                    newest started run and each layer's progress (InProgress)
forecasts/<model>/<YYYY>/<MM>/<DD>/<HHMM>Z/manifest.json   the run's layers and frames (RunManifest)
forecasts/<model>/<YYYY>/<MM>/<DD>/<HHMM>Z/<layer>/<YYYY-MM-DDTHHMM>-<digest>.webp  frames, by valid time and content
```

Browsers read these straight from the bucket (`PUBLIC_BUCKET_NAME`), so only forecasts ever go in it. Frames
are named by their content and never change, so they're cached for good; the JSON files are rewritten in
place, so browsers revalidate them every time (cheap, with ETags).

A run's manifest is rewritten as its frames arrive. Frames are uploaded before the manifest that lists them,
and a run is complete before `latest.json` points at it, so a reader that follows the pointers never finds a
missing file.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from attrs import define

from upslope.clients.storage import StorageClient
from upslope.logging import get_logger
from upslope.models.manifest import ExportStatus, ForecastIndex, InProgress, ModelIndex, RunManifest, RunPointer, run_path

logger = get_logger(__name__)

PREFIX = "forecasts"
LATEST = "latest.json"
IN_PROGRESS = "in-progress.json"
MANIFEST = "manifest.json"
EXPORT_STATUS = f"{PREFIX}/export-status.json"
INDEX = f"{PREFIX}/index.json"

# Frames are named by their content, so a cached copy is never stale. JSON files change in place.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
MUTABLE_CACHE_CONTROL = "no-cache"

_RUN_PATH = re.compile(r"^(\d{4})/(\d{2})/(\d{2})/(\d{2})(\d{2})Z$")


def parse_run_path(path: str) -> datetime | None:
    """The initialization time a run folder stands for, or None if `path` isn't a run folder."""
    match = _RUN_PATH.match(path)
    if match is None:
        return None
    year, month, day, hour, minute = map(int, match.groups())
    try:
        return datetime(year, month, day, hour, minute, tzinfo=UTC)
    except ValueError:
        return None


@define
class ForecastStore:
    """Reads and writes exported runs under `forecasts/` in the bucket.

    Attributes:
        storage: The bucket.
    """

    storage: StorageClient

    @staticmethod
    def model_prefix(model: str) -> str:
        return f"{PREFIX}/{model}"

    @classmethod
    def run_prefix(cls, model: str, initialized: datetime) -> str:
        """Key prefix of a run's folder, e.g. `forecasts/uwwrf_4km/2026/10/29/0000Z`."""
        return f"{cls.model_prefix(model)}/{run_path(initialized)}"

    def _put_json(self, key: str, data: bytes, if_match: str | None = None) -> None:
        self.storage.put_bytes(key, data, "application/json", if_match=if_match, cache_control=MUTABLE_CACHE_CONTROL)

    def read_manifest(self, model: str, initialized: datetime) -> RunManifest | None:
        """A run's manifest, or None if the run hasn't been exported."""
        versioned = self.read_manifest_versioned(model, initialized)
        return None if versioned is None else versioned[0]

    def read_manifest_versioned(self, model: str, initialized: datetime) -> tuple[RunManifest, str | None] | None:
        """A run's manifest and its ETag, to rewrite it only if nothing else has meanwhile (see `write_manifest`)."""
        stored = self.storage.get_bytes(f"{self.run_prefix(model, initialized)}/{MANIFEST}")
        return None if stored is None else (RunManifest.from_json(stored.data), stored.etag)

    def write_manifest(self, manifest: RunManifest, if_match: str | None = None) -> None:
        """Write a run's manifest. Upload every frame it lists first.

        Args:
            manifest: The manifest.
            if_match: Only write if the stored manifest still has this ETag; raises PreconditionFailed if it
                changed or was deleted (e.g. the run was pruned).
        """
        key = f"{self.run_prefix(manifest.model, manifest.initialized)}/{MANIFEST}"
        self._put_json(key, manifest.to_json(), if_match=if_match)

    def upload_frame(self, model: str, initialized: datetime, relative_path: str, local_path: Path) -> None:
        """Upload a frame image to `<run folder>/<relative_path>`."""
        self.storage.upload_file(local_path, f"{self.run_prefix(model, initialized)}/{relative_path}", cache_control=IMMUTABLE_CACHE_CONTROL)

    def read_latest(self, model: str) -> RunPointer | None:
        """The model's newest complete run, or None if none has been exported."""
        stored = self.storage.get_bytes(f"{self.model_prefix(model)}/{LATEST}")
        return None if stored is None else RunPointer.from_json(stored.data)

    def write_latest(self, pointer: RunPointer) -> None:
        """Point `latest.json` at a run. Write the run's manifest first."""
        self._put_json(f"{self.model_prefix(pointer.model)}/{LATEST}", pointer.to_json())
        logger.info("%s latest run is now %s", pointer.model, pointer.path)

    def read_in_progress(self, model: str) -> InProgress | None:
        """The model's newest started run and its progress, or None if no run has started."""
        stored = self.storage.get_bytes(f"{self.model_prefix(model)}/{IN_PROGRESS}")
        return None if stored is None else InProgress.from_json(stored.data)

    def write_in_progress(self, progress: InProgress) -> None:
        """Write `in-progress.json`. Write the run's manifest first."""
        self._put_json(f"{self.model_prefix(progress.model)}/{IN_PROGRESS}", progress.to_json())

    def read_export_status(self) -> ExportStatus | None:
        """The exporter's status, or None if it has never run."""
        stored = self.storage.get_bytes(EXPORT_STATUS)
        return None if stored is None else ExportStatus.from_json(stored.data)

    def write_export_status(self, status: ExportStatus) -> None:
        """Write the exporter's status."""
        self._put_json(EXPORT_STATUS, status.to_json())

    def read_model_index(self, model: str) -> ModelIndex:
        """A model's index entry, from its pointers and a listing of its runs (slow: it lists every frame)."""
        return ModelIndex(model, self.read_latest(model), self.read_in_progress(model), self.list_runs(model))

    def read_index(self) -> ForecastIndex | None:
        """The index of every model's pointers and runs, or None if it hasn't been written."""
        stored = self.storage.get_bytes(INDEX)
        return None if stored is None else ForecastIndex.from_json(stored.data)

    def write_index(self, index: ForecastIndex) -> None:
        """Write the index."""
        self._put_json(INDEX, index.to_json())

    def list_runs(self, model: str) -> list[datetime]:
        """Initialization times of the model's runs that have a manifest, newest first."""
        prefix = f"{self.model_prefix(model)}/"
        runs = set()
        for key in self.storage.list_keys(prefix):
            folder, _, name = key.removeprefix(prefix).rpartition("/")
            if name == MANIFEST and (initialized := parse_run_path(folder)) is not None:
                runs.add(initialized)
        return sorted(runs, reverse=True)

    def prune(self, model: str, retention_days: int, now: datetime, keep: set[datetime] = frozenset()) -> list[datetime]:
        """Delete the model's runs initialized more than `retention_days` before `now`.

        Args:
            model: Model id.
            retention_days: How many days of runs to keep.
            now: Current time.
            keep: Runs to keep regardless of age, e.g. the ones `latest.json` points at.

        Returns:
            The runs deleted.
        """
        cutoff = now - timedelta(days=retention_days)
        prefix = f"{self.model_prefix(model)}/"
        doomed: dict[datetime, list[str]] = {}
        for key in self.storage.list_keys(prefix):
            parts = key.removeprefix(prefix).split("/")
            if len(parts) < 5:  # latest.json, in-progress.json and other model-level files
                continue
            initialized = parse_run_path("/".join(parts[:4]))
            if initialized is not None and initialized < cutoff and initialized not in keep:
                doomed.setdefault(initialized, []).append(key)
        # delete each run's manifest last, so an interrupted prune never leaves a listed run with missing frames
        for initialized, keys in sorted(doomed.items()):
            keys.sort(key=lambda key: key.endswith(f"/{MANIFEST}"))
            self.storage.delete_keys(keys)
            logger.info("deleted %s run %s (%d objects), older than %d days", model, run_path(initialized), len(keys), retention_days)
        return sorted(doomed)
