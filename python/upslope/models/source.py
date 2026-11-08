"""Where forecast frames come from: the interface the export driver uses for every kind of model.

A source knows how to find a model's runs and turn one forecast hour of a layer into a georeferenced
image. Everything else (following runs, manifests, pointers, retention) is the driver's job, the same
for every source.
"""

from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np
from attrs import define

from upslope.models.config import LayerConfig, ModelConfig
from upslope.models.manifest import Legend


class NotPublished(Exception):
    """The forecast hour hasn't been published yet; the driver tries again at the next check."""


class SourceUnavailable(Exception):
    """The source can't export anything this check (e.g. no annotations are published); its models are skipped."""


@define
class RenderedFrame:
    """What a source knows about a frame besides its image.

    Attributes:
        values: The frame's numbers in its legend's units, shaped (rows, cols) on the image's pixel grid,
            NaN where there's no data. Exported as a value image so the map can show a value under the cursor.
        legend: How to read the image's colors, if the source chose them.
    """

    values: np.ndarray | None = None
    legend: Legend | None = None


class ForecastSource(Protocol):
    """Finds a model's runs and renders their frames."""

    def check(self) -> AbstractContextManager[None]:
        """Scope one export check: state read once per check (annotations, dataset snapshots) lives inside it."""
        ...

    def latest_complete_run(self, model: ModelConfig, now: datetime) -> datetime | None:
        """The newest run of `model` with every layer's every hour published, or None.

        Raises SourceUnavailable if the source can't export this check, or another exception if upstream
        can't be reached (so an outage is never mistaken for an incomplete run).
        """
        ...

    def revision(self, model: ModelConfig) -> int | None:
        """Revision of the inputs besides the config that a layer's frames depend on, or None.

        Exported layers are reused only while this is unchanged; for UW WRF it is the annotation revision.
        """
        ...

    def render_frame(self, model: ModelConfig, layer: LayerConfig, initialized: datetime, hour: int, out: Path) -> RenderedFrame:
        """Write one forecast hour of a layer as a GeoTIFF in Web Mercator (`upslope.raster.OUTPUT_CRS`).

        A single band is written paletted, with a colormap and a nodata index for pixels outside the frame.

        Args:
            model: Model the layer belongs to.
            layer: Layer to render.
            initialized: Initialization time of the run.
            hour: Forecast hour.
            out: Path to write the GeoTIFF to.

        Returns:
            The frame's values and legend, where the source has them.

        Raises:
            NotPublished: The hour isn't available yet.
        """
        ...
