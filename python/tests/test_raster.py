"""Shared raster steps: warping to Web Mercator, the corners of the result and the source grid's footprint."""

import numpy as np
import pytest
import rasterio
from affine import Affine
from PIL import Image

from upslope.raster import (
    FOOTPRINT_POINTS_PER_EDGE, OUTPUT_CRS, convert_to_webp, decode_values_png, encode_values_png, footprint,
    image_coordinates, read_footprint, warp_to_web_mercator, write_geotiff,
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

def test_value_png_round_trips(tmp_path) -> None:
    """Values come back to the nearest step, NaN as no data, and out-of-range values clamped."""
    values = np.array([[-40.0, 0.0, 12.344], [np.nan, 18_500.0, 1e9]])
    path = tmp_path / "values.png"
    encode_values_png(values, path, scale=0.01, offset=-100_000.0)
    decoded = decode_values_png(path, scale=0.01, offset=-100_000.0)
    assert np.isnan(decoded[1, 0])
    np.testing.assert_allclose(decoded[0], [-40.0, 0.0, 12.34], atol=1e-6)
    assert decoded[1, 1] == pytest.approx(18_500.0)
    assert decoded[1, 2] == pytest.approx((2**24 - 2) * 0.01 - 100_000.0)


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
