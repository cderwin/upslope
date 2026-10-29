"""Forecast models and their image layers, loaded from `python/config/models.yaml`.

Each layer names a URL template for its images, so adding a layer or a model that publishes map
images takes only config. Everything is validated at load time, so a typo fails at startup rather
than as a 404 during an export.
"""

import hashlib
import json
import re
import string
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

from attrs import define, field
from cattrs import transform_error
from cattrs.preconf.pyyaml import make_converter
from cattrs.v import format_exception

# IDs appear in map URLs, storage keys and annotation image IDs, so keep them to a safe alphabet
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Template fields a layer URL may use
URL_FIELDS = frozenset({"init", "hour"})

# How many recent runs to consider when looking for the newest one; covers a late or missing run
DEFAULT_LOOKBACK_HOURS = 48


class ConfigError(ValueError):
    """The models config is invalid."""


def _check_id(kind: str, value: str) -> None:
    if not ID_PATTERN.match(value) or ".." in value:
        raise ConfigError(f"{kind} id {value!r} must be lowercase letters, digits, '.', '_' or '-'")


@define(frozen=True)
class HourRange:
    """Forecast hours a layer is published for: `first` to `last` inclusive, every `step` hours.

    Attributes:
        last: Last forecast hour.
        first: First forecast hour.
        step: Hours between frames.
    """

    last: int
    first: int = 0
    step: int = 3

    def __attrs_post_init__(self) -> None:
        if self.first < 0 or self.step < 1 or self.last < self.first:
            raise ConfigError(f"invalid hours {self.first}..{self.last} every {self.step}")

    @property
    def hours(self) -> list[int]:
        """Every forecast hour in the range, in order."""
        return list(range(self.first, self.last + 1, self.step))


@define(frozen=True)
class LayerConfig:
    """One image layer of a model.

    Attributes:
        id: Stable identifier, used in URLs and storage keys.
        name: Display name.
        url: Image URL template with `{init:<strftime format>}` and `{hour:<format>}` fields.
        hours: Forecast hours the layer is published for.
        maptype: Annotation maptype used to georegister the layer's images.
    """

    id: str
    name: str
    url: str
    hours: HourRange
    maptype: str

    def __attrs_post_init__(self) -> None:
        _check_id("layer", self.id)
        _check_id("maptype", self.maptype)
        try:
            fields = {name for _, name, _, _ in string.Formatter().parse(self.url) if name is not None}
        except ValueError as e:
            raise ConfigError(f"layer {self.id}: malformed url template: {e}") from e
        if fields != URL_FIELDS:
            raise ConfigError(f"layer {self.id}: url must use exactly the fields {sorted(URL_FIELDS)}, not {sorted(fields)}")
        try:
            self.image_url(datetime(2026, 1, 1, tzinfo=UTC), self.hours.first)
        except (ValueError, TypeError) as e:
            raise ConfigError(f"layer {self.id}: url template does not format: {e}") from e

    def image_url(self, initialized: datetime, hour: int) -> str:
        """The URL of one forecast image.

        Args:
            initialized: Initialization time of the model run.
            hour: Forecast hour.

        Returns:
            The formatted URL.
        """
        return self.url.format(init=initialized.astimezone(UTC), hour=hour)

    def fingerprint(self) -> str:
        """Identify the frames this layer produces: its images and how they're placed, not its name.

        Returns:
            A short hex digest; layers that would export identical frames give equal digests.
        """
        payload = {"url": self.url, "hours": self.hours.hours, "maptype": self.maptype}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@define(frozen=True)
class ModelConfig:
    """A forecast model: when it runs, how long its runs are kept, and its layers.

    Attributes:
        id: Stable identifier, used in URLs and storage keys, e.g. `uwwrf_4km`.
        name: Display name.
        start_times: UTC hours the model is initialized at.
        layers: Layers to export from each run.
        retention_days: How long exported runs are kept.
        lookback_hours: How far back to look for a run to export.
    """

    id: str
    name: str
    start_times: list[int]
    layers: list[LayerConfig]
    retention_days: int = 7
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS

    def __attrs_post_init__(self) -> None:
        _check_id("model", self.id)
        if not self.start_times or any(not 0 <= hour < 24 for hour in self.start_times):
            raise ConfigError(f"model {self.id}: start_times must be UTC hours from 0 to 23")
        if not self.layers:
            raise ConfigError(f"model {self.id} has no layers")
        ids = [layer.id for layer in self.layers]
        if duplicates := sorted({i for i in ids if ids.count(i) > 1}):
            raise ConfigError(f"model {self.id}: duplicate layer ids {duplicates}")
        if self.retention_days < 1:
            raise ConfigError(f"model {self.id}: retention_days must be at least 1")

    def initializations(self, now: datetime) -> list[datetime]:
        """Run initialization times within the lookback window, newest first.

        Args:
            now: Current time (timezone-aware). Only runs at or before it are returned.

        Returns:
            Initialization times in UTC.
        """
        now = now.astimezone(UTC)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        earliest = now - timedelta(hours=self.lookback_hours)
        times = []
        while day + timedelta(hours=24) > earliest:
            times.extend(day + timedelta(hours=hour) for hour in self.start_times)
            day -= timedelta(days=1)
        return sorted((t for t in times if earliest < t <= now), reverse=True)

    def layer(self, layer_id: str) -> LayerConfig | None:
        """Look up a layer by id."""
        return next((layer for layer in self.layers if layer.id == layer_id), None)

    def fingerprint(self) -> str:
        """Identify what an export of this model produces: each layer's fingerprint, in order.

        Returns:
            A short hex digest; equal configs give equal digests.
        """
        payload = [[layer.id, layer.fingerprint()] for layer in self.layers]
        return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


@define(frozen=True)
class ModelsConfig:
    """Every configured model.

    Attributes:
        models: The models, in display order.
    """

    models: list[ModelConfig] = field()

    @models.validator
    def _check_models(self, _attribute, models: list[ModelConfig]) -> None:
        ids = [model.id for model in models]
        if duplicates := sorted({i for i in ids if ids.count(i) > 1}):
            raise ConfigError(f"duplicate model ids {duplicates}")

    def model(self, model_id: str) -> ModelConfig | None:
        """Look up a model by id."""
        return next((model for model in self.models if model.id == model_id), None)

    @property
    def maptypes(self) -> set[str]:
        """Every maptype a configured layer uses."""
        return {layer.maptype for model in self.models for layer in model.layers}

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load and validate the config from a YAML file.

        Args:
            path: Path to the YAML file.

        Returns:
            The config. Raises ConfigError (or a cattrs error naming the bad field) if it is invalid.
        """
        converter = make_converter(forbid_extra_keys=True)
        try:
            return converter.loads(path.read_text(), cls)
        except BaseExceptionGroup as e:
            raise ConfigError(f"invalid {path.name}: " + "; ".join(transform_error(e, format_exception=_format_error))) from e


def _format_error(exc: BaseException, type_: type | None) -> str:
    # our own checks already say what's wrong; cattrs' default would only say "invalid value"
    return str(exc) if isinstance(exc, ConfigError) else format_exception(exc, type_)
