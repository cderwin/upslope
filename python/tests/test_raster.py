"""Shared raster steps: warping to Web Mercator, the corners of the result and the source grid's footprint."""

import numpy as np
import pytest
import rasterio
from affine import Affine
from PIL import Image

from pyproj import Transformer
from rasterio.io import MemoryFile

from upslope.raster import (
    FOOTPRINT_POINTS_PER_EDGE, LOOKUP_STEP, OUTPUT_CRS, VALUES_NODATA, convert_to_webp, decode_lookup, decode_values,
    encode_lookup, encode_values, footprint, image_coordinates, read_footprint, warp_to_web_mercator, write_geotiff,
)
from upslope.models.uw_wrf.georegistration import WRF_LAMBERT_CRS


def test_warp_places_a_lat_lon_grid(tmp_path) -> None:
    # 0.25 degree cells covering 125W-115W, 49N-44N
    data = np.arange(40 * 20, dtype=np.float32).reshape(1, 20, 40)
    transform = Affine(0.25, 0.0, -125.0, 0.0, -0.25, 49.0)
    warped, dst_transform = warp_to_web_mercator(data, "EPSG:4326", transform, nodata=np.nan)
    assert warped.dtype == np.float32 and not np.isnan(warped).all()

    out = tmp_path / "warped.tiff"
    write_geotiff(out, warped, dst_transform, nodata=np.nan)
    with rasterio.open(out) as dataset:
        assert dataset.crs == OUTPUT_CRS
        (west, north), _, (east, south), _ = image_coordinates(dataset)
    # square output pixels put the far edges within a source cell of the original
    assert (west, north) == (-125.0, 49.0)
    assert abs(east + 115.0) < 0.25 and abs(south - 44.0) < 0.25



def test_webp_keeps_palette_colors_exactly_and_nodata_see_through(tmp_path) -> None:
    indices = np.array([[[0, 1, 255], [1, 0, 255]]], dtype=np.uint8)
    colormap = {0: (31, 120, 180, 255), 1: (254, 224, 139, 255), 255: (0, 0, 0, 0)}
    tiff = tmp_path / "frame.tiff"
    write_geotiff(tiff, indices, Affine(1000.0, 0.0, 0.0, 0.0, -1000.0, 0.0), nodata=255, colormap=colormap)
    with rasterio.open(tiff) as dataset:
        convert_to_webp(dataset, tmp_path / "frame.webp")
    with Image.open(tmp_path / "frame.webp") as webp:
        assert webp.format == "WEBP"
        rgba = np.asarray(webp.convert("RGBA"))
    assert rgba[..., 3].tolist() == [[255, 255, 0], [255, 255, 0]]
    assert [tuple(rgba[0, i, :3]) for i in range(2)] == [(31, 120, 180), (254, 224, 139)]
    assert tuple(rgba[1, 0, :3]) == (254, 224, 139)

GRID = Affine(0.25, 0.0, -125.0, 0.0, -0.25, 49.0)


def test_values_round_trip_to_half_a_step() -> None:
    """Values come back within half a step, NaN as no data, out-of-range values clamped, on their own grid."""
    values = np.array([[-40.0, 0.0, 12.344], [np.nan, 18_500.0, -1e9]])
    data = encode_values(values, 0.1, 30.0, "EPSG:4326", GRID, "°F")
    decoded, crs, transform = decode_values(data)
    assert np.isnan(decoded[1, 0]) and transform == GRID and "WGS 84" in crs
    np.testing.assert_allclose(decoded[0], [-40.0, 0.0, 12.3], atol=1e-9)
    assert decoded[1, 1] == pytest.approx(32767 * 0.1 + 30.0)
    assert decoded[1, 2] == pytest.approx(-32767 * 0.1 + 30.0)
    # a tiled int16 COG that GDAL-based tools read as values in their units
    with MemoryFile(data) as memory, memory.open() as dataset:
        assert dataset.dtypes[0] == "int16" and dataset.nodata == VALUES_NODATA
        assert (dataset.scales[0], dataset.offsets[0], dataset.units[0]) == (0.1, 30.0, "°F")
        assert dataset.profile["compress"] == "deflate" and dataset.profile["tiled"]


def lookup_at(lookup: np.ndarray, x: float, y: float) -> tuple[float, float]:
    """Interpolate a lookup at display pixel position (x, y), as the map does."""
    i, j = x / LOOKUP_STEP, y / LOOKUP_STEP
    i0, j0 = int(i), int(j)
    fi, fj = i - i0, j - j0
    corners = lookup[:, j0 : j0 + 2, i0 : i0 + 2]
    weights = np.array([[(1 - fi) * (1 - fj), fi * (1 - fj)], [(1 - fi) * fj, fi * fj]])
    return float((corners[0] * weights).sum()), float((corners[1] * weights).sum())


@pytest.mark.parametrize(
    ("crs", "transform", "width", "height"),
    [
        ("EPSG:4326", GRID, 40, 20),  # a lat/lon grid, like GFS
        (WRF_LAMBERT_CRS, Affine(3000.0, 0.0, -600_000.0, 0.0, -3000.0, 500_000.0), 400, 330),  # Lambert, like HRRR
    ],
)
def test_lookup_finds_the_grid_cell_under_a_display_pixel(crs, transform, width, height) -> None:
    data = np.zeros((1, height, width), dtype=np.float32)
    # frames are at least 600 pixels wide, as the dynamical.org source renders them
    warped, display = warp_to_web_mercator(data, crs, transform, nodata=np.nan, min_width=600)
    size = (warped.shape[2], warped.shape[1])
    lookup = decode_lookup(encode_lookup(display, size, crs, transform))
    assert lookup.shape == (2, -(-size[1] // LOOKUP_STEP) + 1, -(-size[0] // LOOKUP_STEP) + 1)
    to_grid = Transformer.from_crs(OUTPUT_CRS, crs, always_xy=True)
    rng = np.random.default_rng(0)
    for x, y in zip(rng.uniform(0, size[0], 50), rng.uniform(0, size[1], 50)):
        expected = ~transform @ to_grid.transform(*(display @ (x, y)))
        col, row = lookup_at(lookup, x, y)
        # well within a cell: a fraction of the display pixels a cell spans
        assert abs(col - expected[0]) < 0.02 and abs(row - expected[1]) < 0.02


def test_footprint_traces_the_grids_edges() -> None:
    """A lat/lon grid's footprint is its rectangle, clockwise from the top-left, each edge densified."""
    transform = Affine(0.25, 0.0, -125.0, 0.0, -0.25, 49.0)
    ring = footprint("EPSG:4326", transform, 40, 20)
    n = FOOTPRINT_POINTS_PER_EDGE
    assert len(ring) == 4 * n
    assert [ring[0], ring[n], ring[2 * n], ring[3 * n]] == [(-125.0, 49.0), (-115.0, 49.0), (-115.0, 44.0), (-125.0, 44.0)]
    assert all(lat == 49.0 for _, lat in ring[:n]) and all(lon == -115.0 for lon, _ in ring[n : 2 * n])


def test_footprint_follows_a_projected_grid() -> None:
    """A Lambert grid's edges aren't lines of latitude: the top edge's points don't all share one."""
    transform = Affine(4000.0, 0.0, -1_000_000.0, 0.0, -4000.0, 1_000_000.0)
    ring = footprint(WRF_LAMBERT_CRS, transform, 500, 500)
    top = [lat for _, lat in ring[:FOOTPRINT_POINTS_PER_EDGE]]
    assert max(top) - min(top) > 0.1


def test_footprint_is_stored_with_the_geotiff(tmp_path) -> None:
    transform = Affine(0.25, 0.0, -125.0, 0.0, -0.25, 49.0)
    ring = footprint("EPSG:4326", transform, 40, 20)
    out = tmp_path / "frame.tiff"
    write_geotiff(out, np.zeros((1, 2, 2), dtype=np.uint8), transform, nodata=255, crs="EPSG:4326", extent=ring)
    with rasterio.open(out) as dataset:
        assert read_footprint(dataset) == ring
    write_geotiff(out, np.zeros((1, 2, 2), dtype=np.uint8), transform, nodata=255, crs="EPSG:4326")
    with rasterio.open(out) as dataset:
        assert read_footprint(dataset) is None
