"""Raster steps every forecast source shares: warping to Web Mercator, corner coordinates, footprints,
map-ready images, and the value files behind the map's value readout."""

import json
from pathlib import Path
from typing import BinaryIO

import numpy as np
import rasterio
import rasterio.shutil
from affine import Affine
from PIL import Image
from pyproj import Transformer
from rasterio.io import MemoryFile
from rasterio.warp import Resampling, calculate_default_transform, reproject

# Web Mercator: Mapbox draws image overlays linearly between their corners in this CRS, so warped
# frames place exactly.
OUTPUT_CRS = "EPSG:3857"

# GeoTIFF tag holding a frame's footprint (see `footprint`), as JSON
FOOTPRINT_TAG = "UPSLOPE_FOOTPRINT"
# Points along each edge of a footprint: enough to follow an edge that reprojection curves
FOOTPRINT_POINTS_PER_EDGE = 16


def footprint(src_crs: str, src_transform: Affine, width: int, height: int) -> list[tuple[float, float]]:
    """The outline of a raster's grid in (lon, lat): where a warped frame has data, as opposed to the nodata
    that warping leaves around it.

    Args:
        src_crs: CRS of the grid.
        src_transform: Transform from corner-based (col, row) to `src_crs` coordinates.
        width: Grid columns.
        height: Grid rows.

    Returns:
        (lon, lat) pairs rounded to 5 places, clockwise from the top-left corner along the top edge, each
        edge densified so it stays true once reprojected; the ring isn't closed.
    """
    step = np.arange(FOOTPRINT_POINTS_PER_EDGE) / FOOTPRINT_POINTS_PER_EDGE
    n = FOOTPRINT_POINTS_PER_EDGE
    cols = np.concatenate([step * width, np.full(n, width), (1 - step) * width, np.zeros(n)])
    rows = np.concatenate([np.zeros(n), step * height, np.full(n, height), (1 - step) * height])
    a, b, c, d, e, f = src_transform[:6]
    xs, ys = a * cols + b * rows + c, d * cols + e * rows + f
    lons, lats = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True).transform(xs, ys)
    return [(round(float(lon), 5), round(float(lat), 5)) for lon, lat in zip(lons, lats)]


def read_footprint(dataset: rasterio.DatasetReader) -> list[tuple[float, float]] | None:
    """The footprint `write_geotiff` stored with a raster, if any."""
    stored = dataset.tags().get(FOOTPRINT_TAG)
    return None if stored is None else [tuple(point) for point in json.loads(stored)]


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
    extent: list[tuple[float, float]] | None = None,
) -> None:
    """Write a raster as a compressed GeoTIFF.

    Args:
        path: Where to write it.
        data: Pixels, shaped (bands, rows, cols).
        transform: Transform from corner-based (col, row) to `crs` coordinates.
        nodata: Nodata value.
        colormap: Palette for band 1, index -> RGB(A), for paletted images.
        crs: The raster's CRS.
        extent: The source grid's footprint (see `footprint`), to store with it.
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
        if extent is not None:
            dst.update_tags(**{FOOTPRINT_TAG: json.dumps(extent)})


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


def save_webp(image: Image.Image, fp: Path | BinaryIO) -> None:
    """Save an image as a lossless WebP, the format of every frame the map shows.

    Lossless, so palette colors stay exact and never blend at their edges. Method 4 balances size and speed:
    method 6 is 30-50x slower for files only a few percent smaller.

    Args:
        image: Image in any mode; it's saved as RGBA (WebP has no palette), keeping its transparency.
        fp: Path or binary file to write.
    """
    image.convert("RGBA").save(fp, "WEBP", lossless=True, method=4, quality=75)


def convert_to_webp(dataset: rasterio.DatasetReader, webp_path: Path) -> None:
    """Save a raster as a lossless WebP (see `save_webp`), applying its palette and making nodata pixels transparent.

    Args:
        dataset: Open raster to convert.
        webp_path: Path to write the WebP.
    """
    data = dataset.read()
    try:
        cmap = dataset.colormap(1)
    except ValueError:
        cmap = None

    if dataset.count == 1 and cmap is not None:
        image = Image.fromarray(data[0], mode="P")
        image.putpalette([channel for index in range(256) for channel in cmap.get(index, (0, 0, 0, 255))[:3]])
    elif dataset.count == 1:
        image = Image.fromarray(data[0], mode="L")
    else:
        image = Image.fromarray(np.moveaxis(data[:4], 0, -1))
    if dataset.count == 1 and dataset.nodata is not None:
        # Warping leaves nodata around the frame; make it see-through on the map.
        image.info["transparency"] = int(dataset.nodata)

    save_webp(image, webp_path)


# int16 code for no data in value files; values take the codes -32767..32767
VALUES_NODATA = -32768
# Display pixels between the samples of a layer's lookup: projections are smooth, so interpolating between
# samples this far apart is accurate to a small fraction of a grid cell
LOOKUP_STEP = 8
# Lookup positions are rounded to this fraction of a cell, which is plenty and compresses far better
_LOOKUP_RESOLUTION = 64


def _cog_bytes(data: np.ndarray, crs: str, transform: Affine, nodata: float, predictor: int, **band) -> bytes:
    """A deflate-compressed Cloud-Optimized GeoTIFF of `data` (bands, rows, cols), tiled so a reader can range-read."""
    count, height, width = data.shape
    profile = {"driver": "GTiff", "count": count, "height": height, "width": width, "dtype": data.dtype, "crs": crs,
               "transform": transform, "nodata": nodata}
    with MemoryFile() as source:
        with source.open(**profile) as dataset:
            dataset.write(data)
            for key, value in band.items():
                setattr(dataset, key, value)
            with MemoryFile() as cog:
                rasterio.shutil.copy(
                    dataset, cog.name, driver="COG", compress="DEFLATE", predictor=predictor, blocksize=256, overviews="NONE"
                )
                return cog.read()


def encode_values(values: np.ndarray, step: float, offset: float, crs: str, transform: Affine, units: str) -> bytes:
    """Store a frame's numbers as an int16 GeoTIFF on the grid they came from (the model's native grid).

    A value `v` becomes code `round((v - offset) / step)`, clamped to -32767..32767; NaN becomes `VALUES_NODATA`.
    The file carries GDAL's scale, offset and units, so GDAL-based tools read the values themselves.

    Args:
        values: Values shaped (rows, cols), north up; NaN where there's no data.
        step: Value of one code.
        offset: Value of code 0.
        crs: The grid's CRS.
        transform: Corner-based (col, row) -> `crs` coordinates.
        units: The values' units, e.g. `°F`.

    Returns:
        The GeoTIFF's bytes.
    """
    missing = ~np.isfinite(values)
    codes = np.clip(np.rint((np.where(missing, offset, values) - offset) / step), -32767, 32767).astype(np.int16)
    codes[missing] = VALUES_NODATA
    return _cog_bytes(codes[np.newaxis], crs, transform, VALUES_NODATA, predictor=2, scales=(step,), offsets=(offset,), units=(units,))


def decode_values(data: bytes) -> tuple[np.ndarray, str, Affine]:
    """Read back a file written by `encode_values`: float64 values (NaN for no data), its CRS and its transform."""
    with MemoryFile(data) as memory, memory.open() as dataset:
        codes = dataset.read(1)
        step, offset = dataset.scales[0], dataset.offsets[0]
        return np.where(codes == VALUES_NODATA, np.nan, codes * step + offset), dataset.crs.to_wkt(), dataset.transform


def encode_lookup(
    display_transform: Affine, display_size: tuple[int, int], grid_crs: str, grid_transform: Affine, step: int = LOOKUP_STEP
) -> bytes:
    """Where a frame's display pixels fall on the grid of its values, sampled every `step` pixels.

    The map finds the value under the cursor from the display pixel there: it interpolates between the
    surrounding samples to get a position on the values' grid, then between the cells around it. So it needs
    no projection math, whatever the grid's projection.

    Args:
        display_transform: The frame image's transform (corner-based (col, row) -> Web Mercator).
        display_size: The frame image's (width, height).
        grid_crs: CRS of the values' grid.
        grid_transform: Corner-based (col, row) -> `grid_crs` coordinates of the values' grid.
        step: Display pixels between samples.

    Returns:
        A two-band float32 GeoTIFF: band 1 the grid column, band 2 the grid row (continuous, corner-based: cell
        `(c, r)` spans `c..c+1`, `r..r+1`) at display pixel position `(i * step, j * step)` for sample `(i, j)`;
        NaN where the projection has no answer. Its transform places sample `(i, j)` at that pixel corner.
    """
    width, height = display_size
    xs = np.arange(-(-width // step) + 1) * step
    ys = np.arange(-(-height // step) + 1) * step
    cols, rows = np.meshgrid(xs.astype(np.float64), ys.astype(np.float64))
    a, b, c, d, e, f = display_transform[:6]
    mx, my = a * cols + b * rows + c, d * cols + e * rows + f
    gx, gy = Transformer.from_crs(OUTPUT_CRS, grid_crs, always_xy=True).transform(mx, my, errcheck=False)
    inverse = ~grid_transform
    grid_cols = inverse.a * gx + inverse.b * gy + inverse.c
    grid_rows = inverse.d * gx + inverse.e * gy + inverse.f
    samples = np.stack([grid_cols, grid_rows]).astype(np.float64)
    samples[~np.isfinite(samples)] = np.nan
    samples = (np.round(samples * _LOOKUP_RESOLUTION) / _LOOKUP_RESOLUTION).astype(np.float32)
    return _cog_bytes(samples, OUTPUT_CRS, display_transform @ Affine.scale(step), np.nan, predictor=3)


def decode_lookup(data: bytes) -> np.ndarray:
    """Read back a file written by `encode_lookup`: (2, rows, cols) grid columns and rows."""
    with MemoryFile(data) as memory, memory.open() as dataset:
        return dataset.read()
