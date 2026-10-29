"""Exported forecast runs in the storage bucket, laid out like open-meteo's `data_spatial/`.

```
forecasts/<model>/latest.json                         newest complete run (RunPointer)
forecasts/<model>/in-progress.json                    newest started run and each layer's progress (InProgress)
forecasts/<model>/<YYYY>/<MM>/<DD>/<HHMM>Z/manifest.json   the run's layers and frames (RunManifest)
forecasts/<model>/<YYYY>/<MM>/<DD>/<HHMM>Z/<layer>/<YYYY-MM-DDTHHMM>.png   frames, named by valid time
```

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
from upslope.models.manifest import InProgress, RunManifest, RunPointer, run_path

logger = get_logger(__name__)

PREFIX = "forecasts"
LATEST = "latest.json"
IN_PROGRESS = "in-progress.json"
MANIFEST = "manifest.json"

# Prefixes of the storage layout before forecasts/; only `delete_legacy` touches them
LEGACY_PREFIXES = ("web_exports/", "model_runs/")

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

    def read_manifest(self, model: str, initialized: datetime) -> RunManifest | None:
        """A run's manifest, or None if the run hasn't been exported."""
        stored = self.storage.get_bytes(f"{self.run_prefix(model, initialized)}/{MANIFEST}")
        return None if stored is None else RunManifest.from_json(stored.data)

    def write_manifest(self, manifest: RunManifest) -> None:
        """Write a run's manifest. Upload every frame it lists first."""
        key = f"{self.run_prefix(manifest.model, manifest.initialized)}/{MANIFEST}"
        self.storage.put_bytes(key, manifest.to_json(), "application/json")

    def upload_frame(self, model: str, initialized: datetime, relative_path: str, local_path: Path) -> None:
        """Upload a frame image to `<run folder>/<relative_path>`."""
        self.storage.upload_file(local_path, f"{self.run_prefix(model, initialized)}/{relative_path}")

    def read_latest(self, model: str) -> RunPointer | None:
        """The model's newest complete run, or None if none has been exported."""
        stored = self.storage.get_bytes(f"{self.model_prefix(model)}/{LATEST}")
        return None if stored is None else RunPointer.from_json(stored.data)

    def write_latest(self, pointer: RunPointer) -> None:
        """Point `latest.json` at a run. Write the run's manifest first."""
        self.storage.put_bytes(f"{self.model_prefix(pointer.model)}/{LATEST}", pointer.to_json(), "application/json")
        logger.info("%s latest run is now %s", pointer.model, pointer.path)

    def read_in_progress(self, model: str) -> InProgress | None:
        """The model's newest started run and its progress, or None if no run has started."""
        stored = self.storage.get_bytes(f"{self.model_prefix(model)}/{IN_PROGRESS}")
        return None if stored is None else InProgress.from_json(stored.data)

    def write_in_progress(self, progress: InProgress) -> None:
        """Write `in-progress.json`. Write the run's manifest first."""
        self.storage.put_bytes(f"{self.model_prefix(progress.model)}/{IN_PROGRESS}", progress.to_json(), "application/json")

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

    def delete_legacy(self) -> int:
        """Delete everything under the storage layout's old prefixes (`web_exports/`, `model_runs/`).

        Returns:
            How many objects were deleted.
        """
        deleted = 0
        for prefix in LEGACY_PREFIXES:
            keys = self.storage.list_keys(prefix)
            if keys:
                self.storage.delete_keys(keys)
                logger.info("deleted %d objects under %s", len(keys), prefix)
            deleted += len(keys)
        return deleted
