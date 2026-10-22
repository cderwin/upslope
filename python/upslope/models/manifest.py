from datetime import datetime
from pathlib import Path
from typing import Self

from attrs import define


@define
class Frame:
    """One image in a layer's forecast sequence.

    Attributes:
        hour: Forecast hour.
        path: Image path (local before export, relative to the manifest after).
    """

    hour: int
    path: str


@define
class ManifestLayer:
    """A forecast variable from one model run, as a sequence of map-ready frames.

    Attributes:
        model: Model name.
        variable: Forecast variable name.
        initialized: Model run initialization time.
        dimensions: Image (width, height) in pixels, if known.
        coordinates: Image corner (lon, lat) pairs in Mapbox order, if known.
        frames: Frames in the layer.
    """

    model: str
    variable: str
    initialized: datetime
    dimensions: tuple[int, int] | None  # width, height
    # [lon, lat] of the image's top-left, top-right, bottom-right and bottom-left corners (Mapbox order)
    coordinates: list[tuple[float, float]] | None
    frames: list[Frame]

    def add_frame(self, image_path: Path, hour: int) -> None:
        """Append a frame to the layer.

        Args:
            image_path: Path to the frame image.
            hour: Forecast hour of the frame.
        """
        self.frames.append(Frame(hour=hour, path=str(image_path)))

    @classmethod
    def new(cls, model: str, variable: str, initialized: datetime) -> Self:
        """Create an empty layer with no frames, dimensions or coordinates.

        Args:
            model: Model name.
            variable: Forecast variable name.
            initialized: Model run initialization time.

        Returns:
            The new ManifestLayer.
        """
        return cls(model=model, variable=variable, initialized=initialized, dimensions=None, coordinates=None, frames=[])


@define
class Manifest:
    """The layers produced by one pipeline run, for export to the web app.

    Attributes:
        started: When the run started.
        finished: When the run finished.
        layers: Layers produced by the run.
    """

    started: datetime
    finished:datetime
    layers: list[ManifestLayer]
