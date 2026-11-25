"""A model's terrain: the height above sea level of each grid cell's surface, stored with every archived run.

Point forecasts adjust temperatures from a cell's terrain height to the point's own elevation, so the terrain must
be the model's own: the surface its forecast was made over. It comes from the run itself where the source publishes
it (`TerrainConfig.field`), or else from Open-Meteo's static surface height for the same model
(`TerrainConfig.openmeteo`). That file is on Open-Meteo's copy of the model's grid, so it's used only where that
grid's cells line up with the run's; a terrain shifted by part of a cell would describe another surface.
"""

from typing import Protocol

import numpy as np
from attrs import define
from pyproj import Transformer

from upslope.archive.config import ArchiveModel
from upslope.archive.conversions import MissingField, convert_units, time_invariant
from upslope.archive.grid import RegionGrid
from upslope.archive.sources.base import RunReader
from upslope.models.config import ValueQuantization

# The array each run stores the terrain in, and how: whole metres from below the Dead Sea to above Everest
TERRAIN_ARRAY = "surface_height"
TERRAIN_VALUES = ValueQuantization(1, -500, 9000)
# Most a static grid's cell centres may sit from the run's, as a fraction of a cell
ALIGNMENT_TOLERANCE = 0.1


@define
class StaticGrid:
    """A time-invariant field on a regular grid in its CRS, such as Open-Meteo's static surface height.

    Attributes:
        values: Shaped (rows, cols), in the order of `ys` and `xs`; NaN where there's no data.
        crs: The grid's CRS, as WKT.
        xs: Cell-centre x coordinates, evenly spaced.
        ys: Cell-centre y coordinates, evenly spaced.
    """

    values: np.ndarray
    crs: str
    xs: np.ndarray
    ys: np.ndarray


class StaticHeights(Protocol):
    """Where Open-Meteo's static surface heights come from (`OpenMeteoRunSource`)."""

    def surface_height(self, domain: str) -> StaticGrid:
        """A domain's static surface height in metres (blocking). Raises MissingField if it has none."""
        ...


def sample_static(static: StaticGrid, grid: RegionGrid, tolerance: float = ALIGNMENT_TOLERANCE) -> np.ndarray:
    """A static field's values at every cell of a run's grid, when the two grids' cells line up.

    Args:
        static: The static field.
        grid: The run's grid.
        tolerance: Most a run cell's centre may sit from a static cell's, as a fraction of a cell.

    Returns:
        Values shaped like `grid`. Raises ValueError if the run's cells don't line up with the static grid's, or
        fall outside it.
    """
    lon, lat = grid.lon_lat()
    x, y = Transformer.from_crs("EPSG:4326", static.crs, always_xy=True).transform(lon, lat)
    cols = (np.asarray(x) - static.xs[0]) / (static.xs[1] - static.xs[0])
    rows = (np.asarray(y) - static.ys[0]) / (static.ys[1] - static.ys[0])
    col, row = np.rint(cols).astype(np.int64), np.rint(rows).astype(np.int64)
    if col.min() < 0 or row.min() < 0 or col.max() >= len(static.xs) or row.max() >= len(static.ys):
        raise ValueError("the run's grid reaches beyond the static grid")
    offset = float(max(np.abs(cols - col).max(), np.abs(rows - row).max()))
    if offset > tolerance:
        raise ValueError(f"the run's cells sit up to {offset:.2f} cells from the static grid's, so the grids differ")
    return static.values[row, col]


def model_terrain(model: ArchiveModel, reader: RunReader, heights: StaticHeights | None) -> tuple[np.ndarray, str]:
    """A model's terrain over a run's grid (blocking).

    Args:
        model: The model; its `terrain` says where the terrain comes from.
        reader: The run, already read from (its grid is known).
        heights: Open-Meteo's static heights, for models whose terrain comes from there.

    Returns:
        The terrain in metres, shaped like the run's grid (NaN where unknown), and where it came from. Raises
        MissingField if the model has no terrain or its source doesn't publish it, ValueError if it doesn't fit
        the run's grid.
    """
    terrain = model.terrain
    if terrain is None:
        raise MissingField(f"{model.id} has no terrain configured")
    if terrain.field is not None:
        upstream = reader.read(terrain.field)
        values = convert_units(time_invariant(upstream), upstream.units, "m")
    else:
        if heights is None or terrain.openmeteo is None:
            raise MissingField("Open-Meteo's static heights aren't available")
        values = sample_static(heights.surface_height(terrain.openmeteo), reader.grid)
    if values.shape != reader.grid.shape:
        raise ValueError(f"the terrain is shaped {values.shape}, not like the run's grid {reader.grid.shape}")
    return np.asarray(values, dtype=np.float32), terrain.describe()
