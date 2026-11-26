"""The part of a model's grid an archived run covers: its CRS, north-up transform and shape.

Runs are stored north up on the model's own grid (no resampling), so a point is found with the grid's CRS and
transform, and winds keep the model's grid geometry until they're rotated to true north here.
"""

import math
from functools import cache

import numpy as np
from affine import Affine
from attrs import define, field
from pyproj import Transformer

# How far north (degrees) to step when finding which way true north points on the grid
_NORTH_STEP = 0.01


@cache
def _transformer(source: str, target: str) -> Transformer:
    return Transformer.from_crs(source, target, always_xy=True)


@define(frozen=True)
class RegionGrid:
    """A model's grid over its region.

    Attributes:
        crs: The grid's CRS, as WKT.
        transform: Corner-based (col, row) -> `crs` coordinates, north up.
        shape: (rows, cols).
    """

    crs: str
    transform: Affine
    shape: tuple[int, int]
    _cache: dict = field(factory=dict, eq=False, repr=False)

    @property
    def x(self) -> np.ndarray:
        """Cell-centre x coordinates, west to east."""
        return self.transform.c + (np.arange(self.shape[1]) + 0.5) * self.transform.a

    @property
    def y(self) -> np.ndarray:
        """Cell-centre y coordinates, north to south."""
        return self.transform.f + (np.arange(self.shape[0]) + 0.5) * self.transform.e

    def lon_lat(self) -> tuple[np.ndarray, np.ndarray]:
        """Longitude and latitude of every cell centre, each shaped like the grid."""
        if "lon_lat" not in self._cache:
            xs, ys = np.meshgrid(self.x, self.y)
            lon, lat = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True).transform(xs, ys)
            self._cache["lon_lat"] = (np.asarray(lon), np.asarray(lat))
        return self._cache["lon_lat"]

    def north_angle(self) -> np.ndarray:
        """Angle (radians, clockwise) from the grid's +y axis to true north, at every cell centre.

        Zero everywhere on a latitude/longitude grid; on a Lambert conformal grid, the meridians' convergence.
        """
        if "north" not in self._cache:
            lon, lat = self.lon_lat()
            to_grid = Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)
            x0, y0 = to_grid.transform(lon, lat)
            x1, y1 = to_grid.transform(lon, np.minimum(lat + _NORTH_STEP, 90.0))
            self._cache["north"] = np.arctan2(np.asarray(x1) - x0, np.asarray(y1) - y0)
        return self._cache["north"]

    @property
    def geo_transform(self) -> str:
        """The transform as GDAL's GeoTransform attribute: 'c a b f d e'."""
        t = self.transform
        return " ".join(repr(float(v)) for v in (t.c, t.a, t.b, t.f, t.d, t.e))

    @classmethod
    def from_attributes(cls, crs: str, geo_transform: str, shape: tuple[int, int]) -> "RegionGrid":
        """The grid of a stored run, from its `spatial_ref` attributes (`crs_wkt`, `GeoTransform`) and shape."""
        return cls(crs, Affine.from_gdal(*(float(v) for v in geo_transform.split())), shape)

    def cell(self, lon: float, lat: float) -> tuple[int, int] | None:
        """(row, col) of the cell holding a point, or None if it's outside the grid."""
        x, y = _transformer("EPSG:4326", self.crs).transform(lon, lat)
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        col, row = ~self.transform @ (x, y)
        row, col = math.floor(row), math.floor(col)
        if not (0 <= row < self.shape[0] and 0 <= col < self.shape[1]):
            return None
        return row, col

    def cell_centre(self, row: int, col: int) -> tuple[float, float]:
        """(lon, lat) of a cell's centre."""
        x, y = self.transform @ (col + 0.5, row + 0.5)
        lon, lat = _transformer(self.crs, "EPSG:4326").transform(x, y)
        return float(lon), float(lat)
