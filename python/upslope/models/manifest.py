"""What an export writes to storage for each model run (its manifest), and per model (`latest.json`, `in-progress.json`)."""

from datetime import datetime, timedelta
from typing import Self

from attrs import define, field
from cattrs.preconf.json import make_converter

converter = make_converter()


def run_path(initialized: datetime) -> str:
    """A run's folder under its model's prefix, e.g. `2026/10/29/0000Z` (open-meteo style)."""
    return f"{initialized:%Y/%m/%d/%H%M}Z"


def frame_name(valid: datetime) -> str:
    """A frame's file name: its valid time in UTC, e.g. `2026-10-29T0600.png` (open-meteo style)."""
    return f"{valid:%Y-%m-%dT%H%M}.png"


@define
class Frame:
    """One image in a layer's forecast sequence.

    Attributes:
        hour: Forecast hour.
        valid: Time the frame is valid for (UTC): the run's initialization time plus `hour`.
        path: Image path relative to the run's folder, e.g. `surface-temp/2026-10-29T0600.png`.
    """

    hour: int
    valid: datetime
    path: str


@define
class RunLayer:
    """A layer of one model run, as a sequence of map-ready frames.

    Attributes:
        id: Layer id, e.g. `surface-temp`.
        name: Display name.
        maptype: Annotation maptype the frames were georegistered with.
        fingerprint: `LayerConfig.fingerprint()` of the config the frames were exported with.
        hours: Every forecast hour the layer is published for, exported or not.
        dimensions: Image (width, height) in pixels, once a frame exists.
        coordinates: Image corner (lon, lat) pairs in Mapbox order (top-left, top-right,
            bottom-right, bottom-left), once a frame exists.
        frames: Exported frames, in hour order.
    """

    id: str
    name: str
    maptype: str
    fingerprint: str
    hours: list[int]
    dimensions: tuple[int, int] | None = None
    coordinates: list[tuple[float, float]] | None = None
    frames: list[Frame] = field(factory=list)

    @property
    def complete(self) -> bool:
        """Whether every forecast hour has a frame, whatever order the frames are in."""
        return {frame.hour for frame in self.frames} == set(self.hours)


@define
class RunManifest:
    """A model run's exported layers: `forecasts/<model>/<run path>/manifest.json`.

    Attributes:
        model: Model id, e.g. `uwwrf_4km`.
        name: Model display name.
        initialized: Run initialization time (UTC).
        registration_revision: Revision of the published annotations the frames were georegistered with.
        updated_at: When the manifest was last written.
        layers: The run's layers, in display order.
    """

    model: str
    name: str
    initialized: datetime
    registration_revision: int
    updated_at: datetime
    layers: list[RunLayer] = field(factory=list)

    @property
    def path(self) -> str:
        """The run's folder under its model's prefix."""
        return run_path(self.initialized)

    @property
    def complete(self) -> bool:
        """Whether every layer has every forecast hour."""
        return all(layer.complete for layer in self.layers)

    def layer(self, layer_id: str) -> RunLayer | None:
        """Look up a layer by id."""
        return next((layer for layer in self.layers if layer.id == layer_id), None)

    def to_json(self) -> bytes:
        return converter.dumps(self).encode()

    @classmethod
    def from_json(cls, data: bytes | str) -> Self:
        return converter.loads(data, cls)


@define
class RunPointer:
    """Points at one of a model's runs: `forecasts/<model>/latest.json`.

    Attributes:
        model: Model id.
        initialized: Run initialization time (UTC).
        path: The run's folder under the model's prefix, e.g. `2026/10/29/0000Z`.
        updated_at: When the pointer was written.
    """

    model: str
    initialized: datetime
    path: str
    updated_at: datetime

    @classmethod
    def to(cls, manifest: RunManifest, now: datetime) -> Self:
        return cls(manifest.model, manifest.initialized, manifest.path, now)

    def to_json(self) -> bytes:
        return converter.dumps(self).encode()

    @classmethod
    def from_json(cls, data: bytes | str) -> Self:
        return converter.loads(data, cls)


@define
class LayerProgress:
    """How far a layer of an in-progress run has got.

    Attributes:
        id: Layer id.
        last_hour: The layer's last forecast hour; the layer is complete once it's exported.
        available_hour: Newest hour exported so far, or None before the first.
        progressed_at: When `available_hour` last advanced.
        error: Why the last attempt to get the next hour failed (other than it not being published yet).
        error_at: When that attempt was.
    """

    id: str
    last_hour: int
    available_hour: int | None = None
    progressed_at: datetime | None = None
    error: str | None = None
    error_at: datetime | None = None

    @property
    def complete(self) -> bool:
        return self.available_hour == self.last_hour


@define
class InProgress:
    """The newest run that has started publishing, and how far each layer has got:
    `forecasts/<model>/in-progress.json`. Updated at every check, so it also shows the run once complete.

    Attributes:
        model: Model id.
        initialized: Run initialization time (UTC).
        path: The run's folder under the model's prefix.
        checked_at: When the exporter last checked the run for new hours.
        layers: Each configured layer's progress, in display order.
    """

    model: str
    initialized: datetime
    path: str
    checked_at: datetime
    layers: list[LayerProgress] = field(factory=list)

    @property
    def complete(self) -> bool:
        return all(layer.complete for layer in self.layers)

    def layer(self, layer_id: str) -> LayerProgress | None:
        """Look up a layer by id."""
        return next((layer for layer in self.layers if layer.id == layer_id), None)

    def to_json(self) -> bytes:
        return converter.dumps(self).encode()

    @classmethod
    def from_json(cls, data: bytes | str) -> Self:
        return converter.loads(data, cls)


def valid_time(initialized: datetime, hour: int) -> datetime:
    """The time a forecast hour is valid for."""
    return initialized + timedelta(hours=hour)
