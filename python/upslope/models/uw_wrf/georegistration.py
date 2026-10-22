import warnings
from collections import defaultdict
from pathlib import Path
from typing import Self

import numpy as np
import rasterio
from affine import Affine
from attrs import define
from rasterio.errors import NotGeoreferencedWarning
from rasterio.warp import Resampling, calculate_default_transform, reproject

from pyproj import Geod, Transformer

from upslope.models.uw_wrf.georegistration_document import AnnotationDocument, GeographicPoint, PixelAnnotation


# The UW WRF grids' native projection (WPS namelist: map_proj='lambert', truelat1=30, truelat2=60,
# stand_lon=-121). WRF assumes a spherical earth of radius 6370 km. lat_0 only sets the false origin;
# ref_lat is used for convenience.
WRF_LAMBERT_CRS = (
    "+proj=lcc +lat_1=30 +lat_2=60 +lat_0=45.66480255 +lon_0=-121 +x_0=0 +y_0=0 +R=6370000 +units=m +no_defs"
)

# Web Mercator: Mapbox draws image overlays linearly between their corners in this CRS, so warped
# frames place exactly.
OUTPUT_CRS = "EPSG:3857"

_to_projected = Transformer.from_crs("EPSG:4326", WRF_LAMBERT_CRS, always_xy=True)
_to_geographic = Transformer.from_crs(WRF_LAMBERT_CRS, "EPSG:4326", always_xy=True)
_geod = Geod(ellps="WGS84")


def to_projected(point: GeographicPoint) -> tuple[float, float]:
    """Project a landmark into the WRF Lambert CRS.

    Returns:
        The point's (x, y) in meters.
    """
    x, y = _to_projected.transform(point.longitude, point.latitude)
    return x, y


def fit_transform(control_points: list[tuple[PixelAnnotation, GeographicPoint]]) -> Affine:
    """Fit a pixel -> Lambert transform from control points.

    The plots are drawn on the WRF grid, so the transform is axis-aligned: a scale and offset per
    axis, with no rotation or shear. Least squares needs two control points; three or more leave
    residuals that measure the fit.

    Args:
        control_points: (annotation, landmark) pairs. Raises ValueError if there are fewer than two.

    Returns:
        The transform from corner-based (col, row) to WRF Lambert meters.
    """
    if len(control_points) < 2:
        raise ValueError("needs at least two control points")
    pixels = np.array([annotation.pixel_center() for annotation, _ in control_points])
    projected = np.array([to_projected(point) for _, point in control_points])
    scale_x, offset_x = np.polyfit(pixels[:, 0], projected[:, 0], 1)
    scale_y, offset_y = np.polyfit(pixels[:, 1], projected[:, 1], 1)
    return Affine(scale_x, 0.0, offset_x, 0.0, scale_y, offset_y)


def placement_error(transform: Affine, annotation: PixelAnnotation, point: GeographicPoint) -> float:
    """Distance, in meters on the ellipsoid, between where a transform places an annotation and its landmark."""
    projected_x, projected_y = transform @ annotation.pixel_center()
    projected_lon, projected_lat = _to_geographic.transform(projected_x, projected_y)
    _, _, error = _geod.inv(projected_lon, projected_lat, point.longitude, point.latitude)
    return float(error)


@define
class MaptypeFit:
    """How well a maptype's annotations fit its transform.

    Attributes:
        control_points: Number of usable annotations.
        mean_error_m: Mean placement error in meters, or None if there are too few points to fit.
        fit_threshold_m: The maptype's warning threshold, if set.
        over_threshold: True if the mean error exceeds the threshold.
        image_errors: Mean error per image ID.
        point_errors: Mean error per point key.
        error: Why no transform could be fitted, if so.
    """

    control_points: int
    mean_error_m: float | None
    fit_threshold_m: float | None
    over_threshold: bool
    image_errors: dict[str, float]
    point_errors: dict[str, float]
    error: str | None = None


def fit_report(document: AnnotationDocument) -> dict[str, MaptypeFit]:
    """Fit every maptype in a document and measure each annotation's placement error.

    Unlike `Registration.build`, maptypes with too few points are reported rather than raising.

    Args:
        document: Annotations to check.

    Returns:
        Per maptype, the fit summary.
    """
    report: dict[str, MaptypeFit] = {}
    for maptype, annotations in document.maptypes.items():
        control_points = document.control_points(maptype)
        threshold = annotations.fit_threshold_m
        try:
            transform = fit_transform(control_points)
        except ValueError as e:
            report[maptype] = MaptypeFit(len(control_points), None, threshold, False, {}, {}, error=str(e))
            continue

        all_errors: list[float] = []
        image_errors: dict[str, list[float]] = defaultdict(list)
        point_errors: dict[str, list[float]] = defaultdict(list)
        for image_id, image in annotations.images.items():
            for key, annotation in image.annotations.items():
                point = document.geographic_points.get(key)
                if point is None:
                    continue
                error = placement_error(transform, annotation, point)
                all_errors.append(error)
                image_errors[image_id].append(error)
                point_errors[key].append(error)

        mean_error = float(np.mean(all_errors))
        report[maptype] = MaptypeFit(
            control_points=len(control_points),
            mean_error_m=mean_error,
            fit_threshold_m=threshold,
            over_threshold=threshold is not None and mean_error > threshold,
            image_errors={image: float(np.mean(errors)) for image, errors in image_errors.items()},
            point_errors={point: float(np.mean(errors)) for point, errors in point_errors.items()},
        )
    return report


def unused_palette_index(data: np.ndarray) -> int:
    """Find a palette index the frame never uses, to mark pixels outside it after warping.

    Args:
        data: The frame's palette-index pixel data.

    Returns:
        The highest unused index. Raises ValueError if all 256 are in use.
    """
    unused = np.setdiff1d(np.arange(256), np.unique(data))
    if not unused.size:
        raise ValueError("every palette index is in use; there is none left to mark nodata")
    return int(unused[-1])


@define
class Registration:
    """Fitted pixel-to-map transforms for placing forecast images on a map.

    Attributes:
        domain_transforms: Per domain, the affine transform from pixel to WRF Lambert meters.
    """

    # Pixel (corner-based col, row) -> WRF Lambert conformal meters, per map type
    domain_transforms: dict[str, Affine]

    def georegister(self, forecast_image: Path, registered_forecast_image: Path, domain: str) -> None:
        """Place a raw forecast frame on the WRF Lambert grid and warp it to Web Mercator.

        Args:
            forecast_image: Path to the raw forecast image (usually a GIF).
            registered_forecast_image: Path to write the georeferenced GeoTIFF.
            domain: Domain whose transform to use. Raises ValueError if unknown.
        """
        if domain not in self.domain_transforms:
            raise ValueError(f"no transform exists for provided domain `{domain}`")

        src_transform = self.domain_transforms[domain]

        # Raw GIFs have no georeferencing by design; we supply the transform ourselves.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            with rasterio.open(forecast_image) as src:
                data = src.read()  # shape: (bands, rows, cols); GIFs are usually 1 paletted band
                try:
                    cmap = src.colormap(1)
                except ValueError:
                    cmap = None
                nodata = src.nodata if src.nodata is not None else unused_palette_index(data)
                count, dtype, width, height = src.count, src.dtypes[0], src.width, src.height

        left, top = src_transform @ (0, 0)
        right, bottom = src_transform * (width, height)
        dst_transform, dst_width, dst_height = calculate_default_transform(
            WRF_LAMBERT_CRS, OUTPUT_CRS, width, height, left=left, bottom=bottom, right=right, top=top
        )
        warped = np.full((count, dst_height, dst_width), nodata, dtype=dtype)
        reproject(
            source=data,
            destination=warped,
            src_transform=src_transform,
            src_crs=WRF_LAMBERT_CRS,
            dst_transform=dst_transform,
            dst_crs=OUTPUT_CRS,
            dst_nodata=nodata,
            resampling=Resampling.nearest,  # palette indices can't be blended
        )

        profile = {
            "driver": "GTiff",
            "height": dst_height,
            "width": dst_width,
            "count": count,
            "dtype": dtype,
            "crs": OUTPUT_CRS,
            "transform": dst_transform,
            "nodata": nodata,
            "compress": "deflate",
        }
        with rasterio.open(registered_forecast_image, "w", **profile) as dst:
            dst.write(warped)
            if cmap is not None:
                dst.write_colormap(1, cmap)  # keeps the GIF's palette so colors render correctly

    @classmethod
    def build(cls, document: AnnotationDocument) -> Self:
        """Fit each maptype's pixel -> Lambert transform.

        Args:
            document: Annotations to fit. Raises ValueError if a maptype has fewer than two control points.

        Returns:
            A Registration with one transform per maptype.
        """
        transforms: dict[str, Affine] = {}
        for maptype in document.maptypes:
            try:
                transforms[maptype] = fit_transform(document.control_points(maptype))
            except ValueError as e:
                raise ValueError(f"map type `{maptype}`: {e}") from e
        return cls(domain_transforms=transforms)
