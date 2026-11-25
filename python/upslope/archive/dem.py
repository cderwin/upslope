"""The elevation model point forecasts adjust temperatures to: USGS 3DEP 1 arc-second, with Copernicus GLO-30 where
3DEP has no data (Canada, and anywhere else it lacks), on one 1 arc-second (~30 m) grid.

3DEP is a bare-earth model, so it's preferred; GLO-30 is a radar surface model whose heights include the tree
canopy. Both come as 1° tiles from public buckets. 3DEP's tiles overlap their neighbours by a few cells (usually 6),
with cell edges on whole arc-seconds; that's the DEM's grid, so its cells are copied as they are.
GLO-30's cell centres are on whole arc-seconds, half a cell off, so each DEM cell takes the mean of the 2 x 2 GLO-30
cells around it. Heights are above sea level (NAVD88 and EGM2008, which differ by well under a metre) in whole
metres; cells neither covers (the sea) are empty. A `source` array records which model each cell came from.

```
archive/dem/index.json                    the current build (DemIndex)
archive/dem/<YYYYMMDDTHHMM>.zarr/         one build (Zarr v3, consolidated metadata)
    elevation                             (y, x) int16 metres, -32768 where unknown
    source                                (y, x) uint8: 0 none, 1 3DEP, 2 GLO-30
    y, x, spatial_ref                     cell centres (latitude north to south, longitude) and the grid
```

Each 1° tile of the grid is one shard, so a build writes a tile at a time and a point reads one chunk.
"""

import io
import math
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
from affine import Affine
from attrs import define, field

from upslope.logging import get_logger, log_duration
from upslope.raster import VALUES_NODATA

logger = get_logger(__name__)

THREEDEP_URL = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/TIFF/current/{name}/USGS_1_{name}.tif"
GLO30_URL = "https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"
ATTRIBUTION = (
    "USGS 3D Elevation Program 1 arc-second DEM (public domain); produced using Copernicus WorldDEM-30 © DLR e.V. "
    "2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; "
    "all rights reserved."
)
# A tile streams for seconds; no single read should stall this long
HTTP_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
HTTP_RETRIES = 3
CELLS_PER_DEGREE = 3600
# Chunk side in cells; a tile (one shard) is a whole number of chunks
CHUNK = 600
# Values of the `source` array
SOURCE_NONE, SOURCE_3DEP, SOURCE_GLO30 = 0, 1, 2
# Tiles built at once: each holds a few tile-sized float arrays (~50 MB each at 1 arc-second)
CONCURRENCY = 4


@define
class Tile:
    """A downloaded source tile: its values (NaN where it has no data) and its corner-based transform."""

    values: np.ndarray
    transform: Affine


def threedep_name(north: int, west: int) -> str:
    """3DEP's name for the 1° tile whose north-west corner is at (north, west), e.g. `n47w122`."""
    return f"{'n' if north >= 0 else 's'}{abs(north):02d}{'w' if west < 0 else 'e'}{abs(west):03d}"


def glo30_name(north: int, west: int) -> str:
    """GLO-30's name for the same tile, named by its south-west corner."""
    south = north - 1
    return (f"Copernicus_DSM_COG_10_{'N' if south >= 0 else 'S'}{abs(south):02d}_00_"
            f"{'W' if west < 0 else 'E'}{abs(west):03d}_00_DEM")


def fetch_tile(client: httpx.Client, url: str) -> Tile | None:
    """Download a GeoTIFF tile whole (blocking); None if the bucket has none (e.g. over the sea)."""
    import rasterio

    for attempt in range(HTTP_RETRIES):
        try:
            response = client.get(url)
            if response.status_code in (403, 404):
                return None
            response.raise_for_status()
            break
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            if attempt == HTTP_RETRIES - 1 or (isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500):
                raise
            logger.warning("retrying %s: %s", url, e)
    with rasterio.MemoryFile(io.BytesIO(response.content)) as memory, memory.open() as dataset:
        values = dataset.read(1, masked=True).astype(np.float32).filled(np.nan)
        return Tile(values, dataset.transform)


@define(frozen=True)
class DemGrid:
    """The DEM's grid: whole arc-second cells over `bounds`, north up.

    Attributes:
        bounds: (west, south, east, north) in degrees; whole numbers of cells.
        cells_per_degree: Cells per degree (3600; fewer in tests).
    """

    bounds: tuple[float, float, float, float]
    cells_per_degree: int = CELLS_PER_DEGREE

    def __attrs_post_init__(self) -> None:
        west, south, east, north = self.bounds
        if not (west < east and south < north):
            raise ValueError(f"bounds {self.bounds} aren't (west, south, east, north)")
        if any(abs(v * self.cells_per_degree - round(v * self.cells_per_degree)) > 1e-6 for v in self.bounds):
            raise ValueError(f"bounds {self.bounds} aren't on the grid's cell edges")

    @property
    def shape(self) -> tuple[int, int]:
        west, south, east, north = self.bounds
        return round((north - south) * self.cells_per_degree), round((east - west) * self.cells_per_degree)

    @property
    def transform(self) -> Affine:
        step = 1 / self.cells_per_degree
        return Affine(step, 0, self.bounds[0], 0, -step, self.bounds[3])

    def tiles(self) -> list[tuple[int, int]]:
        """(north, west) corners of the whole-degree tiles that touch the bounds."""
        west, south, east, north = self.bounds
        return [(n, w) for n in range(math.ceil(north), math.floor(south), -1) for w in range(math.floor(west), math.ceil(east))]

    def window(self, north: int, west: int) -> tuple[slice, slice, slice, slice]:
        """Where a tile falls: (rows, cols) of the DEM, and the matching (rows, cols) within the tile."""
        n = self.cells_per_degree
        top = round((self.bounds[3] - north) * n)
        left = round((west - self.bounds[0]) * n)
        rows, cols = self.shape
        r0, r1 = max(top, 0), min(top + n, rows)
        c0, c1 = max(left, 0), min(left + n, cols)
        return slice(r0, r1), slice(c0, c1), slice(r0 - top, r1 - top), slice(c0 - left, c1 - left)


def _check_transform(tile: Tile, west: float, north: float, step: float, name: str) -> None:
    t = tile.transform
    if abs(t.a - step) > step * 1e-4 or abs(-t.e - step) > step * 1e-4 or abs(t.c - west) > step * 0.01 or abs(t.f - north) > step * 0.01:
        raise ValueError(f"{name} isn't laid out as expected: transform {tuple(t)[:6]}")


def threedep_cells(tile: Tile, north: int, west: int, n: int) -> np.ndarray:
    """A 3DEP tile's own n x n cells, without the overlap: usually 6 cells on each side, but not always, so it's
    found from the tile's transform (and must be whole cells)."""
    step, t = 1 / n, tile.transform
    rows, cols = round((t.f - north) / step), round((west - t.c) / step)
    _check_transform(tile, west - cols * step, north + rows * step, step, threedep_name(north, west))
    if rows < 0 or cols < 0 or tile.values.shape[0] < rows + n or tile.values.shape[1] < cols + n:
        raise ValueError(f"{threedep_name(north, west)} doesn't cover its degree: transform {tuple(t)[:6]}")
    return tile.values[rows:rows + n, cols:cols + n]


def glo30_cells(tile: Tile, north: int, west: int, n: int) -> np.ndarray:
    """A GLO-30 tile on the DEM's cells: the mean of the 2 x 2 cells around each centre (the tile's last row and
    column stand in for the neighbouring tiles' first)."""
    step = 1 / n
    _check_transform(tile, west - step / 2, north + step / 2, step, glo30_name(north, west))
    if tile.values.shape != (n, n):
        raise ValueError(f"{glo30_name(north, west)} is shaped {tile.values.shape}, not {(n, n)}")
    padded = np.pad(tile.values, ((0, 1), (0, 1)), mode="edge")
    corners = np.stack([padded[:-1, :-1], padded[1:, :-1], padded[:-1, 1:], padded[1:, 1:]])
    present = np.isfinite(corners)
    total = np.where(present, corners, 0).sum(axis=0)
    count = present.sum(axis=0)
    return np.where(count > 0, total / np.maximum(count, 1), np.nan).astype(np.float32)


@define
class DemSummary:
    """What a build covered: cells from each source, and tiles neither had."""

    cells: dict[str, int] = field(factory=lambda: {"3dep": 0, "glo30": 0, "none": 0})
    empty_tiles: list[str] = field(factory=list)


@define
class DemBuilder:
    """Builds the DEM into a local Zarr store, a tile at a time.

    Attributes:
        grid: The DEM's grid.
        fetch: Downloads a tile by URL, None if there's none; defaults to `fetch_tile` over HTTPS.
        concurrency: Tiles built at once.
    """

    grid: DemGrid
    fetch: Callable[[str], Tile | None] = field(factory=lambda: _http_fetch())
    concurrency: int = CONCURRENCY
    _lock: threading.Lock = field(factory=threading.Lock, init=False)

    def build(self, path: Path) -> DemSummary:
        """Write the DEM to a store at `path` (replaced if it exists) and consolidate its metadata (blocking)."""
        import zarr
        import zarr.storage
        from pyproj import CRS
        from zarr.codecs import BloscCodec

        n = self.grid.cells_per_degree
        shard = (n, n)
        chunk = (min(CHUNK, n), min(CHUNK, n))
        if n % chunk[0]:
            raise ValueError(f"{n} cells per degree isn't a whole number of {chunk[0]}-cell chunks")
        crs = CRS.from_epsg(4326).to_wkt()
        group = zarr.open_group(zarr.storage.LocalStore(path), mode="w", zarr_format=3, attributes={
            "title": "Elevation for point forecasts",
            "sources": "USGS 3DEP 1 arc-second, else Copernicus GLO-30",
            "attribution": ATTRIBUTION,
            "built_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "Conventions": "CF-1.10",
        })
        t = self.grid.transform
        rows, cols = self.grid.shape
        group.create_array("y", data=t.f + (np.arange(rows) + 0.5) * t.e, dimension_names=["y"],
                           attributes={"standard_name": "latitude", "units": "degrees_north"})
        group.create_array("x", data=t.c + (np.arange(cols) + 0.5) * t.a, dimension_names=["x"],
                           attributes={"standard_name": "longitude", "units": "degrees_east"})
        group.create_array("spatial_ref", data=np.array(0, dtype=np.int32), dimension_names=[],
                           attributes={"crs_wkt": crs, "spatial_ref": crs,
                                       "GeoTransform": " ".join(repr(float(v)) for v in (t.c, t.a, t.b, t.f, t.d, t.e))})
        compressors = BloscCodec(cname="zstd", clevel=5, shuffle="shuffle")
        elevation = group.create_array(
            "elevation", shape=(rows, cols), dtype="int16", chunks=chunk, shards=shard, fill_value=VALUES_NODATA,
            compressors=compressors, dimension_names=["y", "x"],
            attributes={"long_name": "surface elevation above sea level", "standard_name": "surface_altitude",
                        "units": "m", "_FillValue": VALUES_NODATA, "grid_mapping": "spatial_ref", "source": "see source"},
        )
        source = group.create_array(
            "source", shape=(rows, cols), dtype="uint8", chunks=chunk, shards=shard, fill_value=SOURCE_NONE,
            compressors=compressors, dimension_names=["y", "x"],
            attributes={"long_name": "elevation source", "flag_values": [SOURCE_NONE, SOURCE_3DEP, SOURCE_GLO30],
                        "flag_meanings": "none usgs_3dep_1_arc_second copernicus_glo30", "grid_mapping": "spatial_ref"},
        )
        summary = DemSummary()
        tiles = self.grid.tiles()
        with log_duration(logger, f"built {len(tiles)} DEM tiles"), ThreadPoolExecutor(self.concurrency) as pool:
            for _ in pool.map(lambda tile: self._build_tile(tile, elevation, source, summary), tiles):
                pass
        zarr.consolidate_metadata(group.store)
        return summary

    def _build_tile(self, corner: tuple[int, int], elevation, source, summary: DemSummary) -> None:
        north, west = corner
        n = self.grid.cells_per_degree
        rows, cols, tile_rows, tile_cols = self.grid.window(north, west)
        values = np.full((n, n), np.nan, dtype=np.float32)
        origin = np.full((n, n), SOURCE_NONE, dtype=np.uint8)
        threedep = self.fetch(THREEDEP_URL.format(name=threedep_name(north, west)))
        if threedep is not None:
            values = threedep_cells(threedep, north, west, n).copy()
            origin[np.isfinite(values)] = SOURCE_3DEP
        missing = ~np.isfinite(values[tile_rows, tile_cols])
        if missing.any():
            glo = self.fetch(GLO30_URL.format(name=glo30_name(north, west)))
            if glo is not None:
                fill = glo30_cells(glo, north, west, n)
                gap = ~np.isfinite(values) & np.isfinite(fill)
                values[gap] = fill[gap]
                origin[gap] = SOURCE_GLO30
        values, origin = values[tile_rows, tile_cols], origin[tile_rows, tile_cols]
        codes = np.where(np.isfinite(values), np.clip(np.rint(values), -32767, 32767), VALUES_NODATA).astype(np.int16)
        elevation[rows, cols] = codes
        source[rows, cols] = origin
        counts = np.bincount(origin.ravel(), minlength=3)
        with self._lock:
            summary.cells["none"] += int(counts[SOURCE_NONE])
            summary.cells["3dep"] += int(counts[SOURCE_3DEP])
            summary.cells["glo30"] += int(counts[SOURCE_GLO30])
            if counts[SOURCE_NONE] == origin.size:
                summary.empty_tiles.append(threedep_name(north, west))
        logger.info("tile %s: %d 3DEP, %d GLO-30, %d empty cells", threedep_name(north, west), counts[SOURCE_3DEP],
                    counts[SOURCE_GLO30], counts[SOURCE_NONE])


def _http_fetch() -> Callable[[str], Tile | None]:
    client = httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True)
    return lambda url: fetch_tile(client, url)
