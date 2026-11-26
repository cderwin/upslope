"""Point forecasts: every variable of every model at one point, from each model's newest archived run.

Built for charts that compare the models one variable at a time. Every model's values go on one shared hourly axis
(`PointForecast.times`), grouped by variable and then by model, so a chart for a variable is that variable's
series, one per model, against the same times.

Steps, per model:

1. `read_run_point` reads the point's cell from a run (one series per variable, and the model's terrain there).
2. `adjust_to_elevation` moves temperatures from the model's terrain to the point's elevation (from the DEM,
   `read_elevation`) with the standard lapse rate, when both heights are known; the response says whether it did,
   and if not, why.
3. `build_point_forecast` places each model's series on the shared axis. A model's value sits at the hour it's
   valid for, with null at hours it has no value for. With `interpolate`, the hours between a model's values are
   filled: a sum is split evenly over its interval, a mean repeated over it, and an instant value interpolated
   linearly. Derived variables follow: wind speed and direction, and running totals of precipitation and snowfall.

Reading is blocking (local files); the rest is pure. A run's arrays are read straight from their shard files
(`upslope.archive.shards`), which is several times faster than zarr-python for one cell; an array stored any other
way is read through zarr.
"""

import json
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from attrs import define, field

from upslope.archive.config import ArchiveConfig, ArchiveModel
from upslope.archive.grid import RegionGrid
from upslope.archive.shards import ShardedArray, UnsupportedLayout
from upslope.archive.terrain import TERRAIN_ARRAY
from upslope.logging import get_logger

if TYPE_CHECKING:
    import zarr

logger = get_logger(__name__)

# The standard atmosphere's lapse rate: temperature falls this much per metre of height
LAPSE_RATE = 0.0065
# How fast the dew point of unsaturated air falls as it rises, per metre
DEW_POINT_LAPSE_RATE = 0.0018
ADJUSTMENT_METHOD = "standard_lapse_rate"
# Magnus formula constants (over water, degC)
_MAGNUS_B, _MAGNUS_C = 17.62, 243.12

TEMPERATURE, DEW_POINT, HUMIDITY = "temperature_2m", "dew_point_2m", "relative_humidity_2m"
WIND_U, WIND_V = "wind_u_10m", "wind_v_10m"


@define(frozen=True)
class DerivedVariable:
    """A variable computed from the archived ones.

    Attributes:
        id: Variable id in the response.
        name: Description.
        units: Units.
        kind: `instant`, or `total`: a running total since `PointForecast.accumulation_start`.
        step: Precision values are rounded to.
        source: The archived variable(s) it's computed from.
    """

    id: str
    name: str
    units: str
    kind: str
    step: float
    source: tuple[str, ...]


DERIVED = [
    DerivedVariable("wind_speed_10m", "10 metre wind speed", "m s-1", "instant", 0.05, (WIND_U, WIND_V)),
    DerivedVariable(
        "wind_direction_10m",
        "10 metre wind direction (from, clockwise from north)",
        "degree",
        "instant",
        1,
        (WIND_U, WIND_V),
    ),
    DerivedVariable(
        "precipitation_accumulated",
        "precipitation since the accumulation start",
        "mm",
        "total",
        0.01,
        ("precipitation",),
    ),
    DerivedVariable(
        "snowfall_water_equivalent_accumulated",
        "snowfall water equivalent since the accumulation start",
        "mm",
        "total",
        0.01,
        ("snowfall_water_equivalent",),
    ),
]


# --- reading


@define
class OpenRun:
    """A model's run, opened for point reads.

    Attributes:
        model: The model.
        init_time: When the run started.
        group: The run's Zarr group.
        grid: The run's grid.
        leads: Lead times in hours.
        readers: Array name -> a reader of its shard files, for each gridded array stored in the layout they read;
            other arrays are read through `group`.
    """

    model: ArchiveModel
    init_time: datetime
    group: "zarr.Group"
    grid: RegionGrid
    leads: np.ndarray
    readers: dict[str, ShardedArray] = field(factory=dict)

    @classmethod
    def open(cls, model: ArchiveModel, path: Path) -> "OpenRun":
        """Open a run stored at `path` (blocking)."""
        import zarr
        import zarr.storage

        group = zarr.open_group(
            zarr.storage.LocalStore(path, read_only=True), mode="r", zarr_format=3, use_consolidated=True
        )
        spatial_ref = _array(group, "spatial_ref").attrs
        shape = (_array(group, "y").shape[0], _array(group, "x").shape[0])
        grid = RegionGrid.from_attributes(str(spatial_ref["crs_wkt"]), str(spatial_ref["GeoTransform"]), shape)
        init_time = datetime.fromisoformat(str(group.attrs["init_time"])).astimezone(UTC)
        leads = np.asarray(_array(group, "lead_time")[:], dtype=np.int64)
        return cls(model, init_time, group, grid, leads, _shard_readers(model, path))


@define
class RunPoint:
    """One model's series at a point, as stored (before any adjustment).

    Attributes:
        model: The model.
        init_time: When the run started.
        leads: Lead times in hours.
        cell_lon, cell_lat: Centre of the cell the point falls in.
        terrain: The model's terrain height there in metres; None if the run has none.
        values: Variable id -> values per lead time (NaN where there are none).
    """

    model: ArchiveModel
    init_time: datetime
    leads: np.ndarray
    cell_lon: float
    cell_lat: float
    terrain: float | None
    values: dict[str, np.ndarray]


def _array(group: "zarr.Group", name: str) -> "zarr.Array":
    """An array of a group; raises TypeError if the name is a group instead."""
    import zarr

    member = group[name]
    if not isinstance(member, zarr.Array):
        raise TypeError(f"{name} isn't an array")
    return member


def _shard_readers(model: ArchiveModel, path: Path) -> dict[str, ShardedArray]:
    """A shard reader for each array on the grid (dimensions ending y, x) in the run's consolidated metadata."""
    metadata = json.loads((path / "zarr.json").read_bytes())["consolidated_metadata"]["metadata"]
    readers = {}
    for name, array in metadata.items():
        if array.get("node_type") != "array" or (array.get("dimension_names") or [])[-2:] != ["y", "x"]:
            continue
        try:
            readers[name] = ShardedArray.from_metadata(path / name, array)
        except UnsupportedLayout as e:
            # correct either way, just slower: zarr reads whatever layout it was written in
            logger.warning("%s %s: reading %s through zarr: %s", model.id, path.name, name, e)
    return readers


def _read_cell(run: OpenRun, name: str, row: int, col: int) -> tuple[np.ndarray, Mapping]:
    """A cell's raw values over the leading axis (one value for a 2-D array), and the array's attributes."""
    reader = run.readers.get(name)
    if reader is not None:
        raw = reader.series(row, col) if len(reader.layout.shape) == 3 else np.array([reader.value(row, col)])
        return raw, reader.attributes
    array = _array(run.group, name)
    return np.asarray(array[..., row, col]).reshape(-1), array.attrs


def _decode(raw: np.ndarray, attrs: Mapping) -> np.ndarray:
    fill = attrs.get("_FillValue")
    values = raw.astype(np.float64) * float(attrs.get("scale_factor", 1.0)) + float(attrs.get("add_offset", 0.0))
    if fill is not None:
        values[raw == fill] = np.nan
    return values


def read_run_point(run: OpenRun, variables: Iterable[str], lat: float, lon: float) -> RunPoint | None:
    """A point's series of each variable the run has (blocking).

    Returns:
        The series, or None if the point is outside the run's grid.
    """
    cell = run.grid.cell(lon, lat)
    if cell is None:
        return None
    row, col = cell
    values = {}
    for variable_id in variables:
        if variable_id in run.readers or variable_id in run.group:
            values[variable_id] = _decode(*_read_cell(run, variable_id, row, col))
    terrain = None
    if TERRAIN_ARRAY in run.readers or TERRAIN_ARRAY in run.group:
        height = float(_decode(*_read_cell(run, TERRAIN_ARRAY, row, col))[0])
        terrain = height if math.isfinite(height) else None
    cell_lon, cell_lat = run.grid.cell_centre(row, col)
    return RunPoint(run.model, run.init_time, run.leads, cell_lon, cell_lat, terrain, values)


@define
class Elevation:
    """A point's elevation in metres, and where it came from (`usgs_3dep`, `copernicus_glo30` or `request`)."""

    metres: float
    source: str


ELEVATION_SOURCES = {1: "usgs_3dep", 2: "copernicus_glo30"}


def read_elevation(dem: "zarr.Group", lat: float, lon: float) -> Elevation | None:
    """A point's elevation from the DEM (see `upslope.archive.dem`), or None outside it or where it has no data."""
    spatial_ref, elevation, source = (_array(dem, name) for name in ("spatial_ref", "elevation", "source"))
    rows, cols = elevation.shape
    grid = RegionGrid.from_attributes(
        str(spatial_ref.attrs["crs_wkt"]), str(spatial_ref.attrs["GeoTransform"]), (rows, cols)
    )
    cell = grid.cell(lon, lat)
    if cell is None:
        return None
    height = float(_decode(np.asarray(elevation[cell]).reshape(1), elevation.attrs)[0])
    if not math.isfinite(height):
        return None
    return Elevation(height, ELEVATION_SOURCES.get(int(np.asarray(source[cell]).item()), "unknown"))


# --- elevation adjustment


@define
class Adjustment:
    """Whether a model's values were adjusted to the point's elevation, and how.

    Attributes:
        applied: Whether they were.
        method: How, e.g. `standard_lapse_rate`.
        lapse_rate_k_per_km: The temperature lapse rate used.
        delta_m: The point's elevation less the model's terrain.
        variables: The variables adjusted.
        reason: Why not, when not applied.
    """

    applied: bool
    method: str | None = None
    lapse_rate_k_per_km: float | None = None
    delta_m: float | None = None
    variables: list[str] = field(factory=list)
    reason: str | None = None


def _saturation_pressure(t: np.ndarray) -> np.ndarray:
    return 6.112 * np.exp(_MAGNUS_B * t / (_MAGNUS_C + t))


def dew_point(t: np.ndarray, rh: np.ndarray) -> np.ndarray:
    """Dew point (degC) from temperature (degC) and relative humidity (%)."""
    gamma = np.log(np.clip(rh, 0.1, 100) / 100) + _MAGNUS_B * t / (_MAGNUS_C + t)
    return _MAGNUS_C * gamma / (_MAGNUS_B - gamma)


def relative_humidity(t: np.ndarray, td: np.ndarray) -> np.ndarray:
    """Relative humidity (%) from temperature and dew point (degC)."""
    return np.clip(100 * _saturation_pressure(td) / _saturation_pressure(t), 0, 100)


def adjust_to_elevation(point: RunPoint, elevation: Elevation | None) -> tuple[dict[str, np.ndarray], Adjustment]:
    """A model's values moved from its terrain to the point's elevation with the standard lapse rate.

    Temperature changes by 6.5 K per km and the dew point by 1.8 K per km (never above the temperature); relative
    humidity is recomputed from the two. Nothing changes if either height is unknown.

    Returns:
        The values (a new dict; arrays adjusted are copies) and what was done.
    """
    if point.terrain is None:
        return point.values, Adjustment(False, reason="no model terrain")
    if elevation is None:
        return point.values, Adjustment(False, reason="point elevation unknown")
    if TEMPERATURE not in point.values:
        return point.values, Adjustment(False, reason="no temperature")
    delta = elevation.metres - point.terrain
    values = dict(point.values)
    t = point.values[TEMPERATURE]
    values[TEMPERATURE] = t - LAPSE_RATE * delta
    adjusted = [TEMPERATURE]
    td = point.values.get(DEW_POINT)
    if td is None and HUMIDITY in point.values:
        td = dew_point(t, point.values[HUMIDITY])
    if td is not None:
        td = np.minimum(td - DEW_POINT_LAPSE_RATE * delta, values[TEMPERATURE])
        if DEW_POINT in point.values:
            values[DEW_POINT] = td
            adjusted.append(DEW_POINT)
        if HUMIDITY in point.values:
            values[HUMIDITY] = relative_humidity(values[TEMPERATURE], td)
            adjusted.append(HUMIDITY)
    return values, Adjustment(True, ADJUSTMENT_METHOD, LAPSE_RATE * 1000, round(delta, 1), adjusted)


# --- the hourly axis


def hourly(leads: np.ndarray, values: np.ndarray, kind: str, interpolate: bool) -> np.ndarray:
    """A series on hours since the run started (0 to the last lead time).

    Without `interpolate`, each value sits at its lead time and other hours are NaN. With it, a sum is split evenly
    over the hours of its interval (since the previous lead time with data, or the run's start), a mean is repeated
    over them, and an instant value is interpolated linearly between lead times with data.
    """
    out = np.full(int(leads[-1]) + 1 if len(leads) else 0, np.nan)
    present = np.isfinite(values)
    data_leads, data = leads[present].astype(np.int64), values[present]
    if not interpolate or not len(data_leads):
        out[data_leads] = data
        return out
    if kind == "instant":
        hours = np.arange(data_leads[0], data_leads[-1] + 1)
        out[hours] = np.interp(hours, data_leads, data)
        return out
    previous = np.concatenate([[0], data_leads[:-1]])
    for start, end, value in zip(previous, data_leads, data, strict=True):
        span = max(int(end - start), 1)
        out[end - span + 1 : end + 1] = value / span if kind == "sum" else value
    return out


def _round(values: np.ndarray, step: float) -> list[float | None]:
    decimals = max(0, -math.floor(math.log10(step))) if step < 1 else 0
    rounded = np.round(values, decimals)
    return [None if not math.isfinite(v) else float(v) for v in rounded.tolist()]


# --- the response


@define
class PointInfo:
    lat: float
    lon: float
    elevation_m: float | None
    elevation_source: str | None


@define
class ModelSteps:
    """A run of a model's lead times at one step: up to `until_hour`, every `step` hours."""

    until_hour: int
    step: int


@define
class CellInfo:
    lat: float
    lon: float
    elevation_m: float | None


@define
class ModelInfo:
    """A model in a point forecast.

    Attributes:
        name: Display name.
        init_time: When its run started.
        steps: Its lead times' steps, so a chart can tell its own values from interpolated ones.
        cell: The grid cell the point falls in: its centre and the model's terrain height there.
        adjustment: Whether its values were adjusted to the point's elevation.
        attribution: Credit for the data.
    """

    name: str
    init_time: datetime
    steps: list[ModelSteps]
    cell: CellInfo
    adjustment: Adjustment
    attribution: str | None = None


@define
class VariableSeries:
    """One variable for every model that has it, on the forecast's hourly axis.

    Attributes:
        name: Description.
        units: Units.
        kind: `instant`; `sum` or `mean` over the interval ending at each time (an hour when interpolated, else
            since the model's previous value); or `total` since `PointForecast.accumulation_start`.
        series: Model id -> one value per hour of `PointForecast.times`, null where it has none.
    """

    name: str
    units: str
    kind: str
    series: dict[str, list[float | None]] = field(factory=dict)


@define
class PointForecast:
    """Every model's forecast at a point.

    Attributes:
        point: The point and its elevation.
        times: The hourly axis every series follows, as Unix seconds.
        interpolated: Whether hours between a model's values were filled.
        accumulation_start: When running totals start (the newest run's start, so every model's total covers the
            same hours), as Unix seconds.
        models: Model id -> the model's run and how its values were made.
        variables: Variable id -> its series.
        missing: Model id -> why it isn't in the forecast.
    """

    point: PointInfo
    times: list[int]
    interpolated: bool
    accumulation_start: int | None
    models: dict[str, ModelInfo]
    variables: dict[str, VariableSeries]
    missing: dict[str, str] = field(factory=dict)


def _steps(model: ArchiveModel) -> list[ModelSteps]:
    return [ModelSteps(hours.last, hours.step) for hours in model.leads]


def _hour(t: datetime) -> int:
    return int(t.timestamp()) // 3600


def build_point_forecast(
    config: ArchiveConfig,
    lat: float,
    lon: float,
    points: list[RunPoint],
    elevation: Elevation | None,
    interpolate: bool = False,
    missing: Mapping[str, str] | None = None,
) -> PointForecast:
    """Put every model's series at a point on one hourly axis, adjusted to the point's elevation where possible.

    Args:
        config: The archive config (variables' names, units, kinds and precision).
        lat, lon: The point.
        points: Each model's series there, in the order models should be listed.
        elevation: The point's elevation, if known.
        interpolate: Fill the hours between each model's values.
        missing: Models left out, and why.

    Returns:
        The forecast.
    """
    missing = dict(missing or {})
    models: dict[str, ModelInfo] = {}
    hourly_values: dict[str, dict[str, np.ndarray]] = {}  # model -> variable -> per hour since its run started
    spread: dict[str, dict[str, np.ndarray]] = {}  # model -> sum variable -> split evenly, for running totals
    starts: dict[str, int] = {}
    for point in points:
        values, adjustment = adjust_to_elevation(point, elevation)
        model = point.model
        if not any(np.isfinite(v).any() for v in values.values()):
            missing[model.id] = "no data at this point"
            continue
        models[model.id] = ModelInfo(
            model.display_name,
            point.init_time,
            _steps(model),
            CellInfo(round(point.cell_lat, 5), round(point.cell_lon, 5), point.terrain),
            adjustment,
            model.attribution,
        )
        kinds = {v: config.variable(v).kind for v in values}
        hourly_values[model.id] = {
            v: hourly(point.leads, series, kinds[v], interpolate) for v, series in values.items()
        }
        spread[model.id] = {
            v: hourly(point.leads, series, "sum", True) for v, series in values.items() if kinds[v] == "sum"
        }
        starts[model.id] = _hour(point.init_time)

    # the axis: from the first hour any model has data to the last
    first, last = None, None
    for model_id, by_variable in hourly_values.items():
        for series in by_variable.values():
            hours = np.nonzero(np.isfinite(series))[0]
            if hours.size:
                lo, hi = starts[model_id] + int(hours[0]), starts[model_id] + int(hours[-1])
                first, last = lo if first is None else min(first, lo), hi if last is None else max(last, hi)
    if first is None or last is None:
        times: list[int] = []
        length = 0
    else:
        times = [hour * 3600 for hour in range(first, last + 1)]
        length = last - first + 1

    def on_axis(model_id: str, series: np.ndarray) -> np.ndarray:
        out = np.full(length, np.nan)
        offset = starts[model_id] - (first or 0)
        lo, hi = max(0, offset), min(length, offset + len(series))
        if lo < hi:
            out[lo:hi] = series[lo - offset : hi - offset]
        return out

    accumulation_start = max(starts.values()) if starts else None
    variables: dict[str, VariableSeries] = {}
    for variable in config.variables:
        for model_id, by_variable in hourly_values.items():
            if variable.id in by_variable:
                entry = variables.setdefault(variable.id, VariableSeries(variable.name, variable.units, variable.kind))
                entry.series[model_id] = _round(on_axis(model_id, by_variable[variable.id]), variable.values.step)
    for derived in DERIVED:
        for model_id, by_variable in hourly_values.items():
            series = _derive(derived, by_variable, spread[model_id], starts[model_id], accumulation_start)
            if series is not None:
                entry = variables.setdefault(derived.id, VariableSeries(derived.name, derived.units, derived.kind))
                entry.series[model_id] = _round(on_axis(model_id, series), derived.step)

    return PointForecast(
        PointInfo(
            lat,
            lon,
            None if elevation is None else round(elevation.metres, 1),
            None if elevation is None else elevation.source,
        ),
        times,
        interpolate,
        None if accumulation_start is None else accumulation_start * 3600,
        models,
        variables,
        missing,
    )


def _derive(
    derived: DerivedVariable,
    values: dict[str, np.ndarray],
    spread: dict[str, np.ndarray],
    start: int,
    accumulation_start: int | None,
) -> np.ndarray | None:
    if any(source not in values for source in derived.source):
        return None
    match derived.id:
        case "wind_speed_10m":
            return np.hypot(values[WIND_U], values[WIND_V])
        case "wind_direction_10m":
            u, v = values[WIND_U], values[WIND_V]
            direction = np.degrees(np.arctan2(-u, -v)) % 360
            return np.where(np.hypot(u, v) > 0, direction, np.where(np.isfinite(u + v), 0.0, np.nan))
        case _:
            # a running total of the amounts split evenly by hour, from the newest run's start, shown where the
            # sum itself has a value (every hour when interpolated). An hour's amount is for the hour ending then, so
            # the hours counted are those after the start.
            source = derived.source[0]
            amounts = spread[source]
            skip = max(0, (accumulation_start or start) - start)
            hours = np.arange(len(amounts))
            counted = np.where((hours > skip) & np.isfinite(amounts), amounts, 0.0)
            totals = np.cumsum(counted)
            last_data = np.nonzero(np.isfinite(amounts))[0]
            shown = np.isfinite(values[source]) & (hours >= skip)
            if last_data.size:
                shown &= hours <= last_data[-1]
            return np.where(shown, totals, np.nan)
