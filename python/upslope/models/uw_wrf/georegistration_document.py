"""The georegistration annotation document: landmarks, and where they appear on each maptype's images.

The admin API edits a draft copy of this document and publishes it; the WRF export fits each
maptype's pixel-to-map transform from the published copy.
"""

import re
from datetime import datetime

from attrs import Factory, define
from cattrs.preconf.json import make_converter

converter = make_converter(forbid_extra_keys=True)

# Keys for points, maptypes and images appear in URLs and S3 keys, so keep them to a safe alphabet.
KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def is_valid_key(value: str) -> bool:
    """Check that a point, maptype or image key is safe to use in a URL path and an S3 key."""
    return bool(KEY_PATTERN.match(value)) and ".." not in value


@define
class GeographicPoint:
    """A landmark on the earth.

    Attributes:
        name: Display name, e.g. `Cape Flattery, WA`.
        latitude: Degrees north.
        longitude: Degrees east.
    """

    name: str
    latitude: float
    longitude: float


@define
class PixelAnnotation:
    """Where a landmark appears in an image.

    Attributes:
        pixel_x: Column of the annotated pixel.
        pixel_y: Row of the annotated pixel.
    """

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
class ImageSource:
    """The forecast frame an image was promoted from.

    Attributes:
        model: Manifest model name, e.g. `uwwrf_4km`.
        variable: Forecast variable, e.g. `surface-temp`.
        hour: Forecast hour.
        initialized: Initialization time of the model run current when the frame was downloaded.
        promoted_at: When the image was added.
    """

    model: str
    variable: str
    hour: int
    initialized: datetime
    promoted_at: datetime


@define
class AnnotatedImage:
    """A raw forecast image and the landmarks annotated on it.

    Attributes:
        width: Image width in pixels.
        height: Image height in pixels.
        source: Where the image came from; None for images carried over from the original repo data.
        annotations: Pixel annotations, keyed by geographic point key (at most one per point).
    """

    width: int
    height: int
    source: ImageSource | None = None
    annotations: dict[str, PixelAnnotation] = Factory(dict)


@define
class MaptypeAnnotations:
    """The annotated images of one maptype, which all share a pixel-to-map transform.

    Attributes:
        images: Images keyed by image ID.
        fit_threshold_m: Mean fit error, in meters, above which publishing warns. None never warns.
    """

    images: dict[str, AnnotatedImage] = Factory(dict)
    fit_threshold_m: float | None = None


@define
class AnnotationDocument:
    """Landmarks and their pixel annotations across every maptype.

    Attributes:
        revision: Increases with every change to the draft. A published copy keeps the revision of the
            draft it was published from.
        geographic_points: Landmarks keyed by point key.
        maptypes: Annotated images per maptype (the `maptype` names in wrf_codes.yaml).
    """

    revision: int = 0
    geographic_points: dict[str, GeographicPoint] = Factory(dict)
    maptypes: dict[str, MaptypeAnnotations] = Factory(dict)

    def control_points(self, maptype: str) -> list[tuple[PixelAnnotation, GeographicPoint]]:
        """Collect a maptype's annotations, across all of its images, with their landmarks.

        Annotations that refer to a missing point are skipped.

        Args:
            maptype: Maptype name.

        Returns:
            (annotation, landmark) pairs, or an empty list if the maptype is unknown.
        """
        pairs = []
        for image in self.maptypes.get(maptype, MaptypeAnnotations()).images.values():
            for key, annotation in image.annotations.items():
                point = self.geographic_points.get(key)
                if point is not None:
                    pairs.append((annotation, point))
        return pairs

    def dangling_references(self) -> list[str]:
        """List annotations that refer to a point that does not exist, as `maptype/image/point`."""
        return [
            f"{maptype}/{image_id}/{key}"
            for maptype, annotations in self.maptypes.items()
            for image_id, image in annotations.images.items()
            for key in image.annotations
            if key not in self.geographic_points
        ]

    def usage_count(self, point_key: str) -> int:
        """Count the annotations that refer to a point."""
        return sum(
            point_key in image.annotations
            for annotations in self.maptypes.values()
            for image in annotations.images.values()
        )

    def to_json(self) -> bytes:
        return converter.dumps(self, indent=2).encode() + b"\n"

    @classmethod
    def from_json(cls, data: bytes | str) -> "AnnotationDocument":
        return converter.loads(data, cls)
