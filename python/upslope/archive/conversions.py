"""Turning upstream fields into standard variables: derivations, units, and the run's lead-time axis.

Every source reads a field as an `UpstreamField` (its lead times, values over the region and units). A standard
variable is computed from one or two of them (`derive`), converted to the variable's units (`convert_units`),
and placed on the model's lead-time axis (`align`). Accumulations become the amount (or mean) over the interval
ending at each lead time, since the field's previous lead time with data, so summing a variable's values over
any span of a run gives that span's total, whatever the model's step or reset convention.
"""

from collections.abc import Callable

import numpy as np
from attrs import define

from upslope.archive.config import ArchiveVariable, SourceField
from upslope.archive.grid import RegionGrid

# Unit spellings upstreams use -> the spelling the archive uses
_UNIT_ALIASES = {
    "degree_Celsius": "degC", "°C": "degC", "degC": "degC", "K": "K",
    "m s-1": "m s-1", "m/s": "m s-1",
    "kg m-2": "kg m-2", "mm": "mm",
    "kg m-2 s-1": "kg m-2 s-1", "mm/s": "kg m-2 s-1",
    "percent": "%", "%": "%", "1": "1",
    "W m-2": "W m-2", "W/m²": "W m-2", "W s m-2": "W s m-2", "J m-2": "W s m-2",
    "Pa": "Pa", "hPa": "hPa",
    "m": "m", "cm": "cm",
    "degree": "degree", "°": "degree",
}
# (from, to) -> conversion, besides leaving a value as it is when the units are the same
_CONVERSIONS: dict[tuple[str, str], Callable[[np.ndarray], np.ndarray]] = {
    ("K", "degC"): lambda v: v - 273.15,
    ("kg m-2", "mm"): lambda v: v,  # 1 kg of water over 1 m² is 1 mm deep
    ("mm", "kg m-2"): lambda v: v,
    ("Pa", "hPa"): lambda v: v / 100,
    ("cm", "m"): lambda v: v / 100,
    ("1", "%"): lambda v: v * 100,
}
# Units a standard variable may be stored in
UNITS = frozenset({"degC", "m s-1", "mm", "%", "1", "W m-2", "hPa", "m", "degree"})


class MissingField(ValueError):
    """An upstream field a variable needs isn't published by the source."""


@define
class UpstreamField:
    """One upstream field of a run over the model's region.

    Attributes:
        leads: Lead times in hours, increasing.
        values: Values shaped (lead, rows, cols), north up, float32; NaN where there's no data.
        units: The values' units, as the upstream spells them.
    """

    leads: np.ndarray
    values: np.ndarray
    units: str


def canonical_units(units: str) -> str:
    """The archive's spelling of an upstream unit. Raises ValueError for one it doesn't know."""
    try:
        return _UNIT_ALIASES[units.strip()]
    except KeyError:
        raise ValueError(f"unknown units {units!r}") from None


def convert_units(values: np.ndarray, units: str, target: str) -> np.ndarray:
    """Convert values to `target` units. Raises ValueError for a pair it can't convert."""
    source = canonical_units(units)
    if source == target:
        return values
    try:
        return _CONVERSIONS[(source, target)](values)
    except KeyError:
        raise ValueError(f"can't convert {units!r} to {target!r}") from None


def _has_data(values: np.ndarray) -> np.ndarray:
    """Which lead times have any data (a field's later lead times read as NaN until they're published)."""
    return np.asarray(np.isfinite(values).reshape(values.shape[0], -1).any(axis=1))


def _previous_with_data(field: UpstreamField) -> list[int | None]:
    """For each lead time with data, the index of the previous lead time with data (None for the first)."""
    previous: list[int | None] = []
    last: int | None = None
    for i, present in enumerate(_has_data(field.values)):
        previous.append(last if present else None)
        if present:
            last = i
    return previous


def _from_run_total(field: UpstreamField, mean: bool) -> np.ndarray:
    """Amounts (or means, per second) over each interval from totals since the run started."""
    out = np.full_like(field.values, np.nan)
    present = _has_data(field.values)
    for i, previous in enumerate(_previous_with_data(field)):
        if not present[i]:
            continue
        if previous is None:
            if field.leads[i] == 0:
                continue  # a total over no time
            amount, hours = field.values[i], float(field.leads[i])
        else:
            amount, hours = field.values[i] - field.values[previous], float(field.leads[i] - field.leads[previous])
        # totals only grow; a small decrease is the upstream's packing precision
        amount = np.maximum(amount, 0.0)
        out[i] = amount / (hours * 3600) if mean else amount
    return out


def _from_rate(field: UpstreamField) -> np.ndarray:
    """Amounts over each interval from a rate averaged over it (since the upstream's previous lead time)."""
    out = np.full_like(field.values, np.nan)
    for i in range(1, len(field.leads)):
        out[i] = field.values[i] * float(field.leads[i] - field.leads[i - 1]) * 3600
    return out


def _from_reset_6h_mean(field: UpstreamField) -> np.ndarray:
    """Means over each interval from means since the last lead time divisible by 6 (GFS's convention)."""
    out = np.full_like(field.values, np.nan)
    for i in range(1, len(field.leads)):
        lead, previous = float(field.leads[i]), float(field.leads[i - 1])
        reset = (np.ceil(lead / 6) - 1) * 6  # the window this mean started at
        if previous <= reset:
            out[i] = field.values[i]
        else:
            out[i] = (field.values[i] * (lead - reset) - field.values[i - 1] * (previous - reset)) / (lead - previous)
    return out


def _common_leads(a: UpstreamField, b: UpstreamField) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The lead times both fields have, and each field's values at them."""
    leads, ia, ib = np.intersect1d(a.leads, b.leads, return_indices=True)
    return leads, a.values[ia], b.values[ib]


def derive(source: SourceField, fields: list[UpstreamField], grid: RegionGrid) -> UpstreamField:
    """Compute a standard variable from its upstream fields, in the upstream's units (see `SourceField`).

    Args:
        source: Which fields, and how they're combined.
        fields: The fields, in `source.fields` order.
        grid: The region's grid, for rotating grid-relative winds.

    Returns:
        The variable, at the first field's lead times (or both fields', for winds). Raises ValueError if the
        units don't fit the derivation.
    """
    first = fields[0]
    units = first.units
    match source.derive:
        case "value":
            values = first.values
        case "run_total":
            values = _from_run_total(first, mean=False)
        case "run_total_mean":
            if canonical_units(units) != "W s m-2":
                raise ValueError(f"run_total_mean needs an integral in W s m-2, not {units!r}")
            values, units = _from_run_total(first, mean=True), "W m-2"
        case "rate":
            if not canonical_units(units).endswith(" s-1"):
                raise ValueError(f"rate needs a rate (per second), not {units!r}")
            values, units = _from_rate(first), canonical_units(units).removesuffix(" s-1")
        case "reset_6h_mean":
            values = _from_reset_6h_mean(first)
        case "grid_wind_u" | "grid_wind_v":
            leads, u, v = _common_leads(first, fields[1])
            angle = grid.north_angle().astype(np.float32)
            if source.derive == "grid_wind_u":
                values = u * np.cos(angle) - v * np.sin(angle)
            else:
                values = u * np.sin(angle) + v * np.cos(angle)
            return UpstreamField(leads, values.astype(np.float32), units)
        case "speed_direction_u" | "speed_direction_v":
            leads, speed, direction = _common_leads(first, fields[1])
            if canonical_units(fields[1].units) != "degree":
                raise ValueError(f"a wind direction must be in degrees, not {fields[1].units!r}")
            # the direction is where the wind blows from, so the wind blows the opposite way
            radians = np.deg2rad(direction)
            values = -speed * (np.sin(radians) if source.derive == "speed_direction_u" else np.cos(radians))
            return UpstreamField(leads, values.astype(np.float32), units)
        case "freezing_level":
            present = _has_data(first.values)[:, None, None]
            height = first.values
            # models mark a freezing level at or below the surface as NaN, 0, a negative height, or (RRFS) a height
            # under the terrain; all are 0, but only at lead times that have data
            below = ~np.isfinite(height) | (height <= 0)
            if len(fields) > 1:
                surface = _surface(fields[1])
                below |= np.isfinite(surface) & (height <= surface)
            values = np.where(present & below, 0.0, height)
        case _:
            raise ValueError(f"unknown derivation {source.derive!r}")
    return UpstreamField(first.leads, values.astype(np.float32), units)


def _surface(field: UpstreamField) -> np.ndarray:
    """A time-invariant field (e.g. surface height), from its first lead time with data; some models publish it
    at lead time 0 only."""
    present = np.nonzero(_has_data(field.values))[0]
    if not present.size:
        return np.full(field.values.shape[1:], np.nan, dtype=np.float32)
    return field.values[present[0]]


def align(field: UpstreamField, leads: list[int]) -> tuple[np.ndarray, list[float]]:
    """Place a field on the model's lead-time axis.

    Args:
        field: The field.
        leads: The model's lead times, in hours.

    Returns:
        Values shaped (len(leads), rows, cols), NaN at lead times the field lacks, and the field's lead times
        that aren't on the axis (and so are left out).
    """
    out = np.full((len(leads), *field.values.shape[1:]), np.nan, dtype=np.float32)
    index = {lead: i for i, lead in enumerate(leads)}
    dropped = []
    for j, lead in enumerate(field.leads):
        i = index.get(int(lead)) if float(lead).is_integer() else None
        if i is None:
            if np.isfinite(field.values[j]).any():
                dropped.append(float(lead))
            continue
        out[i] = field.values[j]
    return out, dropped


def standardize(variable: ArchiveVariable, source: SourceField, fields: list[UpstreamField], grid: RegionGrid, leads: list[int]) -> tuple[np.ndarray, list[float]]:
    """A standard variable on the model's lead-time axis, in its units: `derive`, `convert_units`, then `align`.

    Returns:
        The values, shaped (len(leads), rows, cols), and the upstream lead times left out (see `align`).
    """
    derived = derive(source, fields, grid)
    derived.values = convert_units(derived.values, derived.units, variable.units).astype(np.float32)
    return align(derived, leads)
