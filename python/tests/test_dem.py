"""The elevation model build (`upslope.archive.dem`) and its publishing, offline, on a coarse grid."""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest
import xarray as xr
from affine import Affine

from upslope.archive.dem import (
    GLO30_URL,
    SOURCE_3DEP,
    SOURCE_GLO30,
    SOURCE_NONE,
    THREEDEP_URL,
    DemBuilder,
    DemGrid,
    Tile,
    fetch_tile,
    glo30_cells,
    glo30_name,
    threedep_cells,
    threedep_name,
)
from upslope.archive.store import ArchiveStore, DemIndex

N = 12  # cells per degree
STEP = 1 / N


def threedep_tile(north: int, west: int, values: np.ndarray, overlap: int = 6) -> Tile:
    """A 3DEP-style tile: the n x n cells plus the overlap on each side, cell edges on the grid."""
    padded = np.pad(values.astype(np.float32), overlap, constant_values=-1.0)  # overlap: never used
    return Tile(padded, Affine(STEP, 0, west - overlap * STEP, 0, -STEP, north + overlap * STEP))


def glo30_tile(north: int, west: int, values: np.ndarray) -> Tile:
    """A GLO-30-style tile: n x n cells centred on the grid's cell corners."""
    return Tile(values.astype(np.float32), Affine(STEP, 0, west - STEP / 2, 0, -STEP, north + STEP / 2))


def test_tile_names() -> None:
    assert threedep_name(47, -122) == "n47w122"
    assert glo30_name(47, -122) == "Copernicus_DSM_COG_10_N46_00_W122_00_DEM"  # named by its south-west corner


def test_grid_windows_and_bounds() -> None:
    grid = DemGrid((-122.0, 45.5, -120.0, 47.0), N)
    assert grid.shape == (18, 24)
    assert grid.tiles() == [(47, -122), (47, -121), (46, -122), (46, -121)]
    rows, cols, tile_rows, tile_cols = grid.window(46, -121)  # cut in half by the south edge
    assert (rows, cols) == (slice(12, 18), slice(12, 24)) and (tile_rows, tile_cols) == (slice(0, 6), slice(0, 12))
    with pytest.raises(ValueError, match="cell edges"):
        DemGrid((-122.01, 46, -121, 47), N)


def test_threedep_cells_follow_each_tiles_overlap() -> None:
    values = np.arange(N * N, dtype=np.float32).reshape(N, N)
    for overlap in (6, 2, 0):
        np.testing.assert_array_equal(threedep_cells(threedep_tile(47, -122, values, overlap), 47, -122, N), values)
    half_off = Tile(values, Affine(STEP, 0, -122 - STEP / 2, 0, -STEP, 47 + STEP / 2))
    with pytest.raises(ValueError, match="laid out"):
        threedep_cells(half_off, 47, -122, N)
    with pytest.raises(ValueError, match="doesn't cover"):
        threedep_cells(Tile(values[:6], Affine(STEP, 0, -122, 0, -STEP, 47)), 47, -122, N)


def test_glo30_cells_average_the_four_around_each_centre() -> None:
    values = np.arange(N * N, dtype=np.float32).reshape(N, N)
    cells = glo30_cells(glo30_tile(47, -122, values), 47, -122, N)
    assert cells[0, 0] == pytest.approx(values[:2, :2].mean())
    assert cells[-1, -1] == values[-1, -1]  # the edge stands in for the next tile
    shifted = Tile(values, Affine(STEP, 0, -122.0, 0, -STEP, 47.0))  # on the grid's edges: not GLO-30's layout
    with pytest.raises(ValueError, match="laid out"):
        glo30_cells(shifted, 47, -122, N)


def test_build_prefers_3dep_and_fills_gaps_from_glo30(tmp_path: Path) -> None:
    rows = np.arange(N, dtype=np.float32)[:, None] * 100 + np.zeros((1, N), dtype=np.float32)
    with_gap = rows.copy()
    with_gap[:, :4] = np.nan  # e.g. over the border
    tiles = {
        THREEDEP_URL.format(name="n47w122"): threedep_tile(47, -122, rows),
        THREEDEP_URL.format(name="n47w121"): threedep_tile(47, -121, with_gap, overlap=2),  # some tiles overlap less
        GLO30_URL.format(name=glo30_name(47, -121)): glo30_tile(47, -121, np.full((N, N), 500.0)),
        # GLO-30 only, cut by the south edge
        GLO30_URL.format(name=glo30_name(46, -122)): glo30_tile(46, -122, np.full((N, N), 250.0)),
        # (46, -121): the sea, in neither
    }
    fetched = []
    grid = DemGrid((-122.0, 45.5, -120.0, 47.0), N)
    builder = DemBuilder(grid, fetch=lambda url: fetched.append(url) or tiles.get(url))
    summary = builder.build(tmp_path / "dem.zarr")

    assert GLO30_URL.format(name=glo30_name(47, -122)) not in fetched  # 3DEP had it all
    assert summary.cells == {"3dep": 12 * 12 + 12 * 8, "glo30": 12 * 4 + 6 * 12, "none": 6 * 12}
    assert summary.empty_tiles == ["n46w121"]
    dem = xr.open_zarr(tmp_path / "dem.zarr", consolidated=True)
    elevation, source = dem["elevation"].values, dem["source"].values
    assert elevation.shape == (18, 24) and dem["elevation"].attrs["units"] == "m"
    np.testing.assert_array_equal(elevation[:12, 0], rows[:, 0])  # north up, 3DEP as it is
    assert (source[:12, :12] == SOURCE_3DEP).all()
    assert (elevation[:12, 12:16] == 500).all() and (source[:12, 12:16] == SOURCE_GLO30).all()
    assert (elevation[12:, :12] == 250).all() and (source[12:, :12] == SOURCE_GLO30).all()
    assert np.isnan(elevation[12:, 12:]).all() and (source[12:, 12:] == SOURCE_NONE).all()
    assert dem["y"].values[0] == pytest.approx(47 - STEP / 2) and dem["x"].values[0] == pytest.approx(-122 + STEP / 2)
    assert "3D Elevation Program" in dem.attrs["attribution"] and "Copernicus" in dem.attrs["attribution"]


def test_fetch_tile_reads_geotiffs_and_skips_missing_tiles() -> None:
    from rasterio.io import MemoryFile

    values = np.array([[1.5, -999999.0], [3.0, 4.0]], dtype=np.float32)
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=2, height=2, count=1, dtype="float32", nodata=-999999.0,
                         crs="EPSG:4269", transform=Affine(STEP, 0, -122, 0, -STEP, 47)) as dataset:
            dataset.write(values, 1)
        content = memory.read()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content) if request.url.path == "/tile.tif" else httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    tile = fetch_tile(client, "https://bucket.test/tile.tif")
    assert tile is not None and tile.transform.c == -122
    np.testing.assert_array_equal(tile.values, [[1.5, np.nan], [3.0, 4.0]])
    assert fetch_tile(client, "https://bucket.test/sea.tif") is None


def test_publishing_a_build_replaces_the_previous_one(memory_storage, tmp_path: Path) -> None:
    store = ArchiveStore(memory_storage)
    assert store.read_dem_index() is None
    builder = DemBuilder(DemGrid((-122.0, 46.0, -121.0, 47.0), N),
                         fetch=lambda url: threedep_tile(47, -122, np.ones((N, N))) if "USGS" in url else None)
    builder.build(tmp_path / "a.zarr")
    first = DemIndex("20261125T1200.zarr", datetime(2026, 11, 25, 12, tzinfo=UTC), [-122, 46, -121, 47], {"3dep": 144})
    store.publish_dem(tmp_path / "a.zarr", first)
    assert store.read_dem_index() == first
    keys = memory_storage.list_keys("archive/dem/20261125T1200.zarr/")
    assert keys and memory_storage.puts[-1] == "archive/dem/index.json"
    assert [k for k in memory_storage.puts if k.startswith("archive/dem/2")][-1].endswith("1200.zarr/zarr.json")  # root last

    second = DemIndex("20261126T1200.zarr", datetime(2026, 11, 26, 12, tzinfo=UTC), [-122, 46, -121, 47], {"3dep": 144})
    store.publish_dem(tmp_path / "a.zarr", second)
    assert store.read_dem_index() == second
    assert memory_storage.list_keys("archive/dem/20261125T1200.zarr/") == []
    assert memory_storage.list_keys("archive/dem/20261126T1200.zarr/")
