"""Shared raster steps: warping to Web Mercator and the corners of the result."""

import numpy as np
import pytest
import rasterio
from affine import Affine

from upslope.raster import OUTPUT_CRS, decode_values_png, encode_values_png, image_coordinates, warp_to_web_mercator, write_geotiff


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
