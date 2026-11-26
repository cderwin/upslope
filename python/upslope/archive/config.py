"""The forecast archive's config, loaded from `python/config/archive.yaml`.

The archive keeps recent runs of each model as Zarr stores in one standard form: the same variable names,
units, accumulation conventions and encoding whichever model or source a run came from. The config lists those
standard variables (`variables`), the default chunking and retention (`defaults`), and each model: where its runs
come from (`source`), its region and lead times, and which upstream field (and conversion) gives each standard
variable. It is separate from `models.yaml`, which configures the map, so the two can change independently.

Everything is validated at load time; whether the upstream really has a field is checked against the live data
by `upslope archive-check`.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Self

import yaml
from attrs import define, field
from cattrs import transform_error
from cattrs.preconf.pyyaml import make_converter
from cattrs.v import format_exception

from upslope.models.config import DEFAULT_LOOKBACK_HOURS, ConfigError, HourRange, ValueQuantization, check_id

# How a standard variable relates to time: a value at the lead time, or an amount or mean over the interval that
# ends at it (since the variable's previous lead time with data, or the start of the run)
VariableKind = Literal["instant", "sum", "mean"]


@define(frozen=True)
class ArchiveVariable:
    """A standard variable: what every model's runs store under its id.

    Attributes:
        id: Variable name in the stores, e.g. `temperature_2m`.
        name: Description, stored as the array's `long_name`.
        units: Units values are stored in (see `upslope.archive.conversions.UNITS`).
        kind: `instant`, or a `sum` or `mean` over the interval ending at each lead time.
        values: How values are stored: int16 steps, in `units`.
    """

    id: str
    name: str
    units: str
    values: ValueQuantization
    kind: VariableKind = "instant"

    def __attrs_post_init__(self) -> None:
        from upslope.archive.conversions import UNITS

        check_id("variable", self.id)
        if self.units not in UNITS:
            raise ConfigError(f"variable {self.id}: unknown units {self.units!r}; known: {', '.join(sorted(UNITS))}")


@define(frozen=True)
class ChunkShape:
    """An array's inner chunk (what one read decodes): lead times by grid rows by grid columns.

    A model's `chunks` may give only some sizes; the rest come from `defaults`.
    """

    leads: int | None = None
    rows: int | None = None
    cols: int | None = None

    def merged(self, defaults: "ChunkShape") -> "ChunkShape":
        """These sizes, with those left out taken from `defaults`."""
        return ChunkShape(self.leads or defaults.leads, self.rows or defaults.rows, self.cols or defaults.cols)


@define(frozen=True)
class ShardShape:
    """How many grid cells one shard (one stored object) covers; a shard always holds every lead time.

    Each size must be a whole number of chunks. A model's `shards` may give only some sizes.
    """

    rows: int | None = None
    cols: int | None = None

    def merged(self, defaults: "ShardShape") -> "ShardShape":
        """These sizes, with those left out taken from `defaults`."""
        return ShardShape(self.rows or defaults.rows, self.cols or defaults.cols)


@define(frozen=True)
class ArchiveDefaults:
    """Settings each model takes unless it sets its own.

    Attributes:
        chunks: Inner chunk shape.
        shards: Shard size in grid cells.
        keep_runs: How many of a model's newest runs to keep.
        runs_per_check: Most runs of a model archived in one check: a new run, plus a little of any backlog.
    """

    chunks: ChunkShape = ChunkShape(6, 64, 64)
    shards: ShardShape = ShardShape(512, 512)
    keep_runs: int = 8
    runs_per_check: int = 2


@define(frozen=True)
class DynamicalRunsConfig:
    """Runs read from a dynamical.org dataset (https://dynamical.org/catalog/).

    Attributes:
        dataset: Catalog dataset id, e.g. `noaa-hrrr-forecast-48-hour-virtual`.
        kind: Source kind, `dynamical`.
    """

    dataset: str
    kind: Literal["dynamical"] = "dynamical"

    def describe(self) -> str:
        return f"dynamical.org {self.dataset}"


@define(frozen=True)
class OpenMeteoRunsConfig:
    """Runs read from Open-Meteo's open data (`s3://openmeteo/data_run/<domain>/`).

    Attributes:
        domain: Open-Meteo domain, e.g. `ecmwf_ifs025`.
        kind: Source kind, `openmeteo`.
    """

    domain: str
    kind: Literal["openmeteo"] = "openmeteo"

    def describe(self) -> str:
        return f"Open-Meteo {self.domain}"


ArchiveSourceConfig = DynamicalRunsConfig | OpenMeteoRunsConfig


@define(frozen=True)
class TerrainConfig:
    """Where a model's terrain (surface height above sea level) comes from; give exactly one.

    Prefer the model's own `field` where its source publishes one; otherwise Open-Meteo's static surface height for
    the same model (`openmeteo`), used only where its grid lines up with the run's.

    Attributes:
        field: An upstream field of the run, e.g. `geopotential_height_surface` (its first lead time with data).
        openmeteo: An Open-Meteo domain whose `data/<domain>/static/HSURF.om` gives it, e.g. `cmc_gem_hrdps`.
    """

    field: str | None = None
    openmeteo: str | None = None

    def __attrs_post_init__(self) -> None:
        if (self.field is None) == (self.openmeteo is None):
            raise ConfigError("terrain takes one of field or openmeteo")

    def describe(self) -> str:
        """Where the terrain came from, for the stored array's `source` attribute."""
        return self.field if self.field is not None else f"Open-Meteo {self.openmeteo} static HSURF"

# How a standard variable is computed from upstream fields (see SourceField)
Derivation = Literal[
    "value", "run_total", "run_total_mean", "rate", "reset_6h_mean", "grid_wind_u", "grid_wind_v",
    "speed_direction_u", "speed_direction_v", "freezing_level",
]
# Upstream fields each derivation takes: (fewest, most)
_DERIVATION_FIELDS: dict[str, tuple[int, int]] = {
    "grid_wind_u": (2, 2), "grid_wind_v": (2, 2), "speed_direction_u": (2, 2), "speed_direction_v": (2, 2),
    "freezing_level": (1, 2),
}
# The kind of standard variable a derivation gives, where it gives only one (a mean over each interval may also
# stand in for an instant value, e.g. GFS's cloud cover)
_DERIVATION_KINDS: dict[str, tuple[str, ...]] = {"run_total": ("sum",), "rate": ("sum",)}


@define(frozen=True)
class SourceField:
    """Which upstream fields give a standard variable, and how. In the config, a bare name is `{fields: [<name>]}`.

    Attributes:
        fields: Upstream variable names: one, or two for winds (u and v, or speed and direction) and optionally a
            freezing level's surface height.
        derive: How to compute the standard variable (see `upslope.archive.conversions.derive`):
            `value`: the field as it is (an instant value, or already an amount or mean over each interval).
            `run_total`: an amount accumulated since the run started, as the amount over each interval.
            `run_total_mean`: an integral since the run started (e.g. W s m-2), as the mean over each interval.
            `rate`: a rate averaged over each interval (e.g. kg m-2 s-1), as the amount over the interval.
            `reset_6h_mean`: a mean since the last lead time divisible by 6 (GFS), as the mean over each interval.
            `grid_wind_u`, `grid_wind_v`: the eastward or northward wind from grid-relative u and v.
            `speed_direction_u`, `speed_direction_v`: the eastward or northward wind from a speed and the direction
                it blows from (degrees clockwise from true north).
            `freezing_level`: a freezing-level height, with missing, zero or negative heights, or (given a second
                field, the surface height) heights at or below the surface, as 0.
    """

    fields: tuple[str, ...]
    derive: Derivation = "value"

    def __attrs_post_init__(self) -> None:
        fewest, most = _DERIVATION_FIELDS.get(self.derive, (1, 1))
        if not fewest <= len(self.fields) <= most:
            count = str(fewest) if fewest == most else f"{fewest}-{most}"
            raise ConfigError(f"{self.derive!r} takes {count} field(s), not {list(self.fields)}")

    def describe(self) -> str:
        """How the value was made, for the stored array's `source` attribute."""
        fields = ", ".join(self.fields)
        return fields if self.derive == "value" else f"{self.derive}({fields})"


@define(frozen=True)
class ArchiveModel:
    """A model whose runs are archived.

    Attributes:
        id: Stable identifier, used in storage keys; the same as the model's id on the map where there is one.
        source: Where runs come from.
        region: (west, south, east, north) in degrees: the area kept.
        start_times: UTC hours of the runs to archive.
        leads: Lead times every run is stored with, in hours. Runs share this axis, so they line up; times a run
            lacks are left empty.
        variables: Standard variable id -> the upstream fields that give it. Standard variables a model can't give
            are left out.
        chunks: Chunk sizes, over `defaults.chunks`.
        shards: Shard sizes, over `defaults.shards`.
        keep_runs: How many of its newest runs to keep; None for `defaults.keep_runs`.
        lookback_hours: How far back to look for runs to archive.
        attribution: Credit for the data, stored with each run.
        terrain: Where the model's terrain comes from, stored with each run; None if it has none.
        name: Display name, e.g. `HRRR`; defaults to the id.
    """

    id: str
    source: ArchiveSourceConfig
    region: tuple[float, float, float, float]
    start_times: list[int]
    leads: list[HourRange]
    variables: dict[str, SourceField]
    chunks: ChunkShape = ChunkShape()
    shards: ShardShape = ShardShape()
    keep_runs: int | None = None
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS
    attribution: str | None = None
    terrain: TerrainConfig | None = None
    name: str | None = None

    def __attrs_post_init__(self) -> None:
        check_id("model", self.id)
        if not self.start_times or any(not 0 <= hour < 24 for hour in self.start_times):
            raise ConfigError(f"model {self.id}: start_times must be UTC hours from 0 to 23")
        west, south, east, north = self.region
        if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
            raise ConfigError(f"model {self.id}: region must be (west, south, east, north) in degrees")
        hours = self.lead_hours
        if not hours or hours != sorted(set(hours)):
            raise ConfigError(f"model {self.id}: leads must be increasing and not overlap")
        if not self.variables:
            raise ConfigError(f"model {self.id} has no variables")
        if self.keep_runs is not None and self.keep_runs < 1:
            raise ConfigError(f"model {self.id}: keep_runs must be at least 1")

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def lead_hours(self) -> list[int]:
        """Every lead time, in order."""
        return [hour for hours in self.leads for hour in hours.hours]

    def initializations(self, now: datetime) -> list[datetime]:
        """Run initialization times within the lookback window, newest first (see `ModelConfig.initializations`)."""
        now = now.astimezone(UTC)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        earliest = now - timedelta(hours=self.lookback_hours)
        times = []
        while day + timedelta(hours=24) > earliest:
            times.extend(day + timedelta(hours=hour) for hour in self.start_times)
            day -= timedelta(days=1)
        return sorted((t for t in times if earliest < t <= now), reverse=True)


@define(frozen=True)
class ArchiveConfig:
    """The standard variables and every archived model.

    Attributes:
        variables: The standard variables.
        models: The archived models.
        defaults: Settings models take unless they set their own.
    """

    variables: list[ArchiveVariable]
    models: list[ArchiveModel]
    defaults: ArchiveDefaults = ArchiveDefaults()

    def __attrs_post_init__(self) -> None:
        for kind, ids in (("variable", [v.id for v in self.variables]), ("model", [m.id for m in self.models])):
            if duplicates := sorted({i for i in ids if ids.count(i) > 1}):
                raise ConfigError(f"duplicate {kind} ids {duplicates}")
        if self.defaults.keep_runs < 1 or self.defaults.runs_per_check < 1:
            raise ConfigError("defaults: keep_runs and runs_per_check must be at least 1")
        variables = {variable.id: variable for variable in self.variables}
        for model in self.models:
            for variable_id, source in model.variables.items():
                variable = variables.get(variable_id)
                if variable is None:
                    raise ConfigError(f"model {model.id}: {variable_id} isn't one of the variables")
                kinds = _DERIVATION_KINDS.get(source.derive)
                if kinds is not None and variable.kind not in kinds:
                    raise ConfigError(f"model {model.id}: {source.derive!r} gives a {kinds[0]}, but {variable_id} is {variable.kind}")
            self.chunking(model)  # validates the sizes

    def variable(self, variable_id: str) -> ArchiveVariable:
        """Look up a standard variable by id. Raises KeyError if there's none."""
        return next(v for v in self.variables if v.id == variable_id)

    def model(self, model_id: str) -> ArchiveModel | None:
        """Look up a model by id."""
        return next((model for model in self.models if model.id == model_id), None)

    def chunking(self, model: ArchiveModel) -> tuple[ChunkShape, ShardShape]:
        """A model's chunk and shard sizes, with defaults filled in. Raises ConfigError if they don't fit together."""
        chunks = model.chunks.merged(self.defaults.chunks)
        shards = model.shards.merged(self.defaults.shards)
        if min(chunks.leads or 0, chunks.rows or 0, chunks.cols or 0, shards.rows or 0, shards.cols or 0) < 1:
            raise ConfigError(f"model {model.id}: chunk and shard sizes must be positive")
        if (shards.rows or 0) % (chunks.rows or 1) or (shards.cols or 0) % (chunks.cols or 1):
            raise ConfigError(f"model {model.id}: shards ({shards.rows} x {shards.cols}) must be whole chunks ({chunks.rows} x {chunks.cols})")
        return chunks, shards

    def keep_runs(self, model: ArchiveModel) -> int:
        """How many of a model's newest runs to keep."""
        return model.keep_runs or self.defaults.keep_runs

    def fingerprint(self, model: ArchiveModel) -> str:
        """Identify what a run of `model` is stored as: its source, grid, leads, chunking, variables and terrain.

        Returns:
            A short hex digest. A run archived under another digest is archived again (see `ArchiveDriver`).
        """
        chunks, shards = self.chunking(model)
        payload = {
            "source": [model.source.kind, getattr(model.source, "dataset", None) or getattr(model.source, "domain", None)],
            "region": list(model.region),
            "leads": model.lead_hours,
            "chunks": [chunks.leads, chunks.rows, chunks.cols, shards.rows, shards.cols],
            "variables": {
                variable_id: [list(source.fields), source.derive, self.variable(variable_id).units,
                              self.variable(variable_id).kind, self.variable(variable_id).values.fingerprint_payload()]
                for variable_id, source in sorted(model.variables.items())
            },
            "terrain": [model.terrain.field, model.terrain.openmeteo] if model.terrain else None,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    @classmethod
    def load(cls, path: Path) -> Self:
        """Load and validate the config from a YAML file.

        Args:
            path: Path to the YAML file.

        Returns:
            The config. Raises ConfigError (or a cattrs error naming the bad field) if it is invalid.
        """
        converter = make_converter(forbid_extra_keys=True)
        structure_field = converter.get_structure_hook(SourceField)
        converter.register_structure_hook(
            SourceField, lambda value, type_: SourceField((value,)) if isinstance(value, str) else structure_field(value, type_)
        )
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            raise ConfigError(f"invalid {path.name}: the config must be a mapping")
        try:
            return converter.structure(_name_variables(data), cls)
        except BaseExceptionGroup as e:
            raise ConfigError(f"invalid {path.name}: " + "; ".join(transform_error(e, format_exception=_format_error))) from e


def _name_variables(data: dict) -> dict:
    """The config file lists variables as a mapping from id; the config takes a list, in that order."""
    variables = data.get("variables")
    if isinstance(variables, dict):
        data = {**data, "variables": [{"id": key, **(value or {})} for key, value in variables.items()]}
    return data


def _format_error(exc: BaseException, type_: type | None) -> str:
    return str(exc) if isinstance(exc, ConfigError) else format_exception(exc, type_)
