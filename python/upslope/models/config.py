"""Forecast models and their image layers, loaded from `python/config/models.yaml`.

Each model names its source (where its frames come from). A UW WRF layer names a URL template for its
images; a dynamical.org layer names the dataset variables it shows and how to color them. So adding a
layer or a model takes only config. Everything is validated at load time, so a typo fails at startup
rather than during an export (a variable missing from a dataset is the exception: it shows up as the
layer's error in the export status, and in `upslope check-layers`).
"""

import hashlib
import json
import re
import string
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

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


# How a dynamical.org layer's field is computed from the dataset's variables (see FieldConfig)
Derivation = Literal["value", "wind_speed", "change", "rate_sum", "freezing_level"]


@define(frozen=True)
class FieldConfig:
    """What a dynamical.org layer shows, computed from one forecast hour's variables (and earlier hours').

    In the config, a bare variable name is short for `{variables: [<name>]}`.

    Attributes:
        variables: Dataset variables: one, or the u and v components for `wind_speed`.
        derive: How to compute the field:
            `value`: the variable as it is.
            `wind_speed`: speed from the u and v components (valid for grid-relative winds too).
            `change`: how much a run total (e.g. `total_precipitation_run_total_surface`) grew over the last `hours`.
            `rate_sum`: a rate averaged since the previous step (e.g. kg m-2 s-1), summed over the last `hours`.
            `freezing_level`: a freezing-level height, with missing, zero or negative heights (at or below sea
                level) as 0.
        hours: Interval for `change` and `rate_sum`.
    """

    variables: tuple[str, ...]
    derive: Derivation = "value"
    hours: int | None = None

    def __attrs_post_init__(self) -> None:
        count = 2 if self.derive == "wind_speed" else 1
        if len(self.variables) != count:
            raise ConfigError(f"field {self.derive!r} takes {count} variable(s), not {list(self.variables)}")
        needs_hours = self.derive in ("change", "rate_sum")
        if needs_hours != (self.hours is not None) or (self.hours is not None and self.hours < 1):
            raise ConfigError(f"field {self.derive!r} {'needs' if needs_hours else 'takes no'} positive `hours`")


@define(frozen=True)
class DynamicalLayerConfig:
    """One layer of a dynamical.org model: a field of the dataset, colored with a named scale.

    Attributes:
        id: Stable identifier, used in URLs and storage keys.
        name: Display name.
        field: The dataset variables shown, and how they're combined.
        scale: Color scale (a key of `upslope.models.dynamical.scales.SCALES`), which also sets the display units.
        hours: Forecast hours to export; each must be a lead time of the dataset.
    """

    id: str
    name: str
    field: FieldConfig
    scale: str
    hours: HourRange

    def __attrs_post_init__(self) -> None:
        from upslope.models.dynamical.scales import SCALES

        _check_id("layer", self.id)
        if self.scale not in SCALES:
            raise ConfigError(f"layer {self.id}: unknown scale {self.scale!r}; known: {', '.join(sorted(SCALES))}")
        if self.field.hours is not None and self.hours.first < self.field.hours:
            raise ConfigError(f"layer {self.id}: a {self.field.hours}-hour {self.field.derive} starts at hour {self.field.hours}")

    @property
    def maptype(self) -> None:
        """No annotation maptype: dynamical.org data is already georeferenced."""
        return None

    def fingerprint(self) -> str:
        """Identify the frames this layer produces within its model: its field, colors and hours, not its name.

        Returns:
            A short hex digest; `ModelConfig.layer_fingerprint` adds the model's dataset and region.
        """
        from upslope.models.dynamical.scales import SCALES

        payload = {
            "field": [list(self.field.variables), self.field.derive, self.field.hours],
            "scale": SCALES[self.scale].fingerprint_payload(),
            "hours": self.hours.hours,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


AnyLayerConfig = LayerConfig | DynamicalLayerConfig


@define(frozen=True)
class UwWrfSourceConfig:
    """Map images published by UW WRF, placed on the map with the georegistration annotations.

    Attributes:
        kind: Source kind, `uw_wrf`.
    """

    kind: Literal["uw_wrf"] = "uw_wrf"


@define(frozen=True)
class DynamicalSourceConfig:
    """A forecast dataset from dynamical.org's catalog (https://dynamical.org/catalog/), rendered from its data.

    Attributes:
        dataset: Catalog dataset id, e.g. `noaa-gfs-forecast-virtual`.
        kind: Source kind, `dynamical`.
    """

    dataset: str
    kind: Literal["dynamical"] = "dynamical"


SourceConfig = UwWrfSourceConfig | DynamicalSourceConfig


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
        source: Where the model's frames come from.
        attribution: Credit for the data, shown with it on the map (e.g. a CC BY license's attribution).
        region: (west, south, east, north) in degrees: the area to render, for sources with gridded data.
    """

    id: str
    name: str
    start_times: list[int]
    layers: list[AnyLayerConfig]
    retention_days: int = 7
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS
    source: SourceConfig = field(factory=UwWrfSourceConfig)
    attribution: str | None = None
    region: tuple[float, float, float, float] | None = None

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
        layer_type = DynamicalLayerConfig if isinstance(self.source, DynamicalSourceConfig) else LayerConfig
        if wrong := [layer.id for layer in self.layers if not isinstance(layer, layer_type)]:
            raise ConfigError(f"model {self.id}: layers {wrong} don't fit a {self.source.kind} source")
        if isinstance(self.source, DynamicalSourceConfig):
            if self.region is None:
                raise ConfigError(f"model {self.id}: a dynamical model needs a region")
            west, south, east, north = self.region
            if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
                raise ConfigError(f"model {self.id}: region must be (west, south, east, north) in degrees")
        elif self.region is not None:
            raise ConfigError(f"model {self.id}: only dynamical models take a region")

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

    def layer(self, layer_id: str) -> AnyLayerConfig | None:
        """Look up a layer by id."""
        return next((layer for layer in self.layers if layer.id == layer_id), None)

    def layer_fingerprint(self, layer: AnyLayerConfig) -> str:
        """Identify the frames a layer of this model produces, including what the model contributes.

        A UW WRF layer's frames depend only on the layer; a dynamical layer's also on the dataset and region.

        Returns:
            A short hex digest; exported layers are reused only while it is unchanged.
        """
        if isinstance(self.source, UwWrfSourceConfig):
            return layer.fingerprint()
        payload = [layer.fingerprint(), self.source.dataset, list(self.region or ())]
        return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]

    def fingerprint(self) -> str:
        """Identify what an export of this model produces: each layer's fingerprint, in order.

        Returns:
            A short hex digest; equal configs give equal digests.
        """
        payload = [[layer.id, self.layer_fingerprint(layer)] for layer in self.layers]
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
        """Every maptype a configured layer uses (UW WRF layers; the others need no annotations)."""
        return {layer.maptype for model in self.models for layer in model.layers if isinstance(layer, LayerConfig)}

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load and validate the config from a YAML file.

        Args:
            path: Path to the YAML file.

        Returns:
            The config. Raises ConfigError (or a cattrs error naming the bad field) if it is invalid.
        """
        converter = make_converter(forbid_extra_keys=True)
        structure_field = converter.get_structure_hook(FieldConfig)
        converter.register_structure_hook(
            FieldConfig, lambda value, type_: FieldConfig((value,)) if isinstance(value, str) else structure_field(value, type_)
        )
        try:
            return converter.loads(path.read_text(), cls)
        except BaseExceptionGroup as e:
            raise ConfigError(f"invalid {path.name}: " + "; ".join(transform_error(e, format_exception=_format_error))) from e


def _format_error(exc: BaseException, type_: type | None) -> str:
    # our own checks already say what's wrong; cattrs' default would only say "invalid value"
    return str(exc) if isinstance(exc, ConfigError) else format_exception(exc, type_)
