"""Raster steps every forecast source shares: warping to Web Mercator, corner coordinates and map-ready PNGs."""

from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from PIL import Image
from pyproj import Transformer
from rasterio.warp import Resampling, calculate_default_transform, reproject

# Web Mercator: Mapbox draws image overlays linearly between their corners in this CRS, so warped
# frames place exactly.
OUTPUT_CRS = "EPSG:3857"


def warp_to_web_mercator(
    data: np.ndarray,
    src_crs: str,
    src_transform: Affine,
    nodata: float,
    resampling: Resampling = Resampling.nearest,
    min_width: int | None = None,
) -> tuple[np.ndarray, Affine]:
    """Reproject a raster to `OUTPUT_CRS`, at about its native resolution.

    Args:
        data: Pixels, shaped (bands, rows, cols).
        src_crs: CRS of `data` (anything rasterio accepts, e.g. a PROJ string or WKT).
        src_transform: Transform from corner-based (col, row) to `src_crs` coordinates.
        nodata: Value for pixels outside the source, and source pixels to treat as missing.
        resampling: How to resample; nearest for palette indices, which can't be blended.
        min_width: Upsample by a whole factor until the result is at least this many pixels wide, so a
            coarse grid interpolates smoothly instead of showing its cells.

    Returns:
        The warped pixels, shaped (bands, rows, cols), and their transform.
    """
    count, height, width = data.shape
    left, top = src_transform @ (0, 0)
    right, bottom = src_transform @ (width, height)
    dst_transform, dst_width, dst_height = calculate_default_transform(
        src_crs, OUTPUT_CRS, width, height, left=left, bottom=bottom, right=right, top=top
    )
    if min_width is not None and dst_width < min_width:
        factor = -(-min_width // dst_width)
        dst_transform, dst_width, dst_height = calculate_default_transform(
            src_crs, OUTPUT_CRS, width, height, left=left, bottom=bottom, right=right, top=top,
            dst_width=dst_width * factor, dst_height=dst_height * factor,
        )
    warped = np.full((count, dst_height, dst_width), nodata, dtype=data.dtype)
    reproject(
        source=data,
        destination=warped,
        src_transform=src_transform,
        src_crs=src_crs,
        src_nodata=nodata,
        dst_transform=dst_transform,
        dst_crs=OUTPUT_CRS,
        dst_nodata=nodata,
        resampling=resampling,
    )
    return warped, dst_transform


def write_geotiff(
    path: Path,
    data: np.ndarray,
    transform: Affine,
    nodata: float,
    colormap: dict[int, tuple[int, ...]] | None = None,
    crs: str = OUTPUT_CRS,
) -> None:
    """Write a raster as a compressed GeoTIFF.

    Args:
        path: Where to write it.
        data: Pixels, shaped (bands, rows, cols).
        transform: Transform from corner-based (col, row) to `crs` coordinates.
        nodata: Nodata value.
        colormap: Palette for band 1, index -> RGB(A), for paletted images.
        crs: The raster's CRS.
    """
    count, height, width = data.shape
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": count,
        "dtype": data.dtype,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
        if colormap is not None:
            dst.write_colormap(1, colormap)


def image_coordinates(dataset: rasterio.DatasetReader) -> list[tuple[float, float]]:
    """Get the geographic coordinates of a raster's four corners.

    Args:
        dataset: Open georeferenced raster.

    Returns:
        (lon, lat) pairs, rounded to 6 places, in order top-left, top-right, bottom-right, bottom-left.
    """
    to_geographic = Transformer.from_crs(dataset.crs, "EPSG:4326", always_xy=True)
    coordinates = []
    for col, row in ((0, 0), (dataset.width, 0), (dataset.width, dataset.height), (0, dataset.height)):
        x, y = dataset.transform @ (col, row)
        lon, lat = to_geographic.transform(x, y)
        coordinates.append((round(lon, 6), round(lat, 6)))
    return coordinates


def convert_to_png(dataset: rasterio.DatasetReader, png_path: Path) -> None:
    """Save a raster as a PNG, keeping its palette and making nodata pixels transparent.

    Args:
        dataset: Open raster to convert.
        png_path: Path to write the PNG.
    """
    data = dataset.read()
    try:
        cmap = dataset.colormap(1)
    except ValueError:
        cmap = None

    save_options = {}
    if dataset.count == 1 and dataset.nodata is not None:
        # Warping leaves nodata around the frame; make it see-through on the map.
        save_options["transparency"] = int(dataset.nodata)

    if dataset.count == 1 and cmap is not None:
        # Keep the image paletted: smaller files and exactly the source colors.
        image = Image.fromarray(data[0], mode="P")
        image.putpalette([channel for index in range(256) for channel in cmap.get(index, (0, 0, 0, 255))[:3]])
    elif dataset.count == 1:
        image = Image.fromarray(data[0], mode="L")
    else:
        image = Image.fromarray(np.moveaxis(data[:4], 0, -1))

    image.save(png_path, optimize=True, **save_options)


# Largest integer an RGB pixel holds; 0 is reserved for no data
_MAX_VALUE_CODE = (1 << 24) - 1


def encode_values_png(values: np.ndarray, png_path: Path, scale: float, offset: float) -> None:
    """Save numbers as a lossless RGB PNG the browser can read back exactly.

    Pixel integer `n = R * 65536 + G * 256 + B` is 0 for no data (NaN), else `(n - 1) * scale + offset`
    rounded to the nearest step; values outside the encodable range are clamped (see
    `upslope.models.manifest.ValueEncoding`).

    Args:
        values: Values shaped (rows, cols); NaN where there is no data.
        png_path: Path to write the PNG.
        scale: Value of one step.
        offset: Value of `n = 1`.
    """
    missing = ~np.isfinite(values)
    steps = np.rint((np.where(missing, offset, values) - offset) / scale)
    codes = (np.clip(steps, 0, _MAX_VALUE_CODE - 1) + 1).astype(np.uint32)
    codes[missing] = 0
    rgb = np.stack([(codes >> 16) & 0xFF, (codes >> 8) & 0xFF, codes & 0xFF], axis=-1).astype(np.uint8)
    Image.fromarray(rgb, mode="RGB").save(png_path, optimize=True)


def decode_values_png(png_path: Path, scale: float, offset: float) -> np.ndarray:
    """Read back a PNG written by `encode_values_png`, as float64 values with NaN for no data."""
    with Image.open(png_path) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint32)
    codes = (rgb[..., 0] << 16) | (rgb[..., 1] << 8) | rgb[..., 2]
    return np.where(codes == 0, np.nan, (codes.astype(np.float64) - 1) * scale + offset)
