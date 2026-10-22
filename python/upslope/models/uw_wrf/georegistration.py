import warnings
from collections import defaultdict
from pathlib import Path
from typing import Self

import numpy as np
import rasterio
from affine import Affine
from attrs import define
from cattrs.preconf.pyyaml import make_converter
from rasterio.errors import NotGeoreferencedWarning
from rasterio.warp import Resampling, calculate_default_transform, reproject

from pyproj import Geod, Transformer


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


@define
class GeographicPoint:
    short_name: str
    text_name: str
    latitude: float
    longitude: float

    def to_projected(self) -> tuple[float, float]:
        """Project this point into the WRF Lambert CRS.

        Returns:
            The point's (x, y) in meters.
        """
        x, y = _to_projected.transform(self.longitude, self.latitude)
        return x, y


@define
class ControlPoint:
    geographic_point: str
    pixel_x: int
    pixel_y: int

    def pixel_center(self) -> tuple[float, float]:
        """Get the center of the annotated pixel.

        Returns:
            The (col, row) of the pixel center, in corner-based pixel coordinates.
        """
        # Annotations record the (floored) pixel index; transforms address pixel corners.
        return self.pixel_x + 0.5, self.pixel_y + 0.5


@define
class RegistrationConfig:
    """Control points used to fit each domain's pixel-to-map transform.

    Attributes:
        geographic_points: Known landmarks, keyed by short name.
        domains: Per domain, the control points annotated on each image.
    """

    geographic_points: dict[str, GeographicPoint]
    domains: dict[str, dict[str, list[ControlPoint]]]

    def get_control_points(self, domain: str) -> list[ControlPoint]:
        """Collect a domain's control points across all of its images.

        Args:
            domain: Domain name.

        Returns:
            All control points for the domain, or an empty list if it is unknown.
        """
        all_control_points: list[ControlPoint] = []
        for image, control_points in self.domains.get(domain, {}).items():
            all_control_points += control_points

        return all_control_points

    @classmethod
    def load(cls, registration_config: Path) -> Self:
        """Load the config from a YAML file.

        Args:
            registration_config: Path to the YAML config file.

        Returns:
            The parsed RegistrationConfig.
        """
        converter = make_converter()
        return converter.loads(registration_config.read_text(), cls)


@define
class DomainError:
    domain_error: float
    image_errors: dict[str, float]
    point_errors: dict[str, float]


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

    def compute_error(self, config: RegistrationConfig) -> dict[str, DomainError]:
        """Measure how far the fitted transforms place each control point from its true location.

        Args:
            config: Control points to check.

        Returns:
            Per domain, mean errors in meters overall, per image, and per geographic point.
        """
        total_errors: dict[str, DomainError] = {}
        for domain, images in config.domains.items():
            domain_errors: list[float] = []
            image_errors: dict[str, list[float]] = defaultdict(list)
            point_errors: dict[str, list[float]] = defaultdict(list)
            for image, control_points in images.items():
                for control_point in control_points:
                    # Measure on the ellipsoid so errors are true meters regardless of projection scale.
                    projected_x, projected_y = self.domain_transforms[domain] * control_point.pixel_center()
                    projected_lon, projected_lat = _to_geographic.transform(projected_x, projected_y)
                    groundtruth = config.geographic_points[control_point.geographic_point]
                    _, _, error = _geod.inv(projected_lon, projected_lat, groundtruth.longitude, groundtruth.latitude)

                    domain_errors.append(error)
                    image_errors[image].append(error)
                    point_errors[control_point.geographic_point].append(error)

            total_errors[domain] = DomainError(
                domain_error=np.mean(domain_errors).tolist(),
                image_errors={image: np.mean(errors).tolist() for image, errors in image_errors.items()},
                point_errors={point: np.mean(errors).tolist() for point, errors in point_errors.items()},
            )

        return total_errors

    @classmethod
    def build(cls, config: RegistrationConfig) -> Self:
        """Fit each map type's pixel -> Lambert transform.

        The plots are drawn on the WRF grid, so the transform is axis-aligned: a scale and offset per
        axis, with no rotation or shear. Least squares needs two control points; three or more leave
        residuals that measure the fit.

        Args:
            config: Control points to fit. Raises ValueError if a domain has fewer than two.

        Returns:
            A Registration with one transform per domain.
        """
        transforms: dict[str, Affine] = {}
        for domain, images in config.domains.items():
            control_points = [control_point for points in images.values() for control_point in points]
            if len(control_points) < 2:
                raise ValueError(f"map type `{domain}` needs at least two control points")

            pixels = np.array([control_point.pixel_center() for control_point in control_points])
            projected = np.array(
                [config.geographic_points[control_point.geographic_point].to_projected() for control_point in control_points]
            )
            scale_x, offset_x = np.polyfit(pixels[:, 0], projected[:, 0], 1)
            scale_y, offset_y = np.polyfit(pixels[:, 1], projected[:, 1], 1)
            transforms[domain] = Affine(scale_x, 0.0, offset_x, 0.0, scale_y, offset_y)

        return cls(domain_transforms=transforms)
