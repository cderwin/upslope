"""What an export writes to storage: for each model run (its manifest), per model (`latest.json`,
`in-progress.json`), and for the exporter as a whole (`export-status.json`, `index.json`)."""

import hashlib
from datetime import datetime, timedelta
from typing import Literal, Self

from attrs import define, field
from cattrs.preconf.json import make_converter

converter = make_converter()


def run_path(initialized: datetime) -> str:
    """A run's folder under its model's prefix, e.g. `2026/10/29/0000Z` (open-meteo style)."""
    return f"{initialized:%Y/%m/%d/%H%M}Z"


def content_digest(data: bytes) -> str:
    """A short hash of a file's content, to name it by: a new file always gets a new name."""
    return hashlib.sha256(data).hexdigest()[:16]


def frame_name(valid: datetime, digest: str) -> str:
    """A frame's file name: its valid time in UTC (open-meteo style) and content digest, e.g.
    `2026-10-29T0600-3f9a1c2b7d4e5f60.png`. Browsers cache frames for good, so a re-exported frame needs a
    new name."""
    return f"{valid:%Y-%m-%dT%H%M}-{digest}.png"


def values_name(valid: datetime, digest: str) -> str:
    """A frame's value image name, beside its image: e.g. `2026-10-29T0600-9b0e4d7a1c2f3e45.values.png`."""
    return f"{valid:%Y-%m-%dT%H%M}-{digest}.values.png"


@define
class Frame:
    """One image in a layer's forecast sequence.

    Attributes:
        hour: Forecast hour.
        valid: Time the frame is valid for (UTC): the run's initialization time plus `hour`.
        path: Image path relative to the run's folder, e.g. `surface-temp/2026-10-29T0600-3f9a1c2b7d4e5f60.png`.
        values: Path of the frame's value image (see `ValueEncoding`), for layers with numeric data.
    """

    hour: int
    valid: datetime
    path: str
    values: str | None = None


@define
class LegendStop:
    """One color of a layer's scale.

    Attributes:
        value: Value (in the legend's units) the color starts at.
        color: CSS hex color, e.g. `#1f78b4`.
    """

    value: float
    color: str


@define
class Legend:
    """How to read a layer's colors, for layers whose colors the export chose.

    Attributes:
        units: Display units of the values, e.g. `°F`.
        stops: Colors in increasing value order; each applies from its value up to the next one's.
        decimals: Decimal places to show values with.
    """

    units: str
    stops: list[LegendStop]
    decimals: int = 0


@define
class ValueEncoding:
    """How a frame's value image stores numbers: a lossless RGB PNG on the same pixel grid as the frame.

    A pixel's integer `n = R * 65536 + G * 256 + B` is no data when 0, and otherwise the value
    `(n - 1) * scale + offset`, in the legend's units.

    Attributes:
        scale: Value of one step.
        offset: Value of `n = 1`.
    """

    scale: float = 0.01
    offset: float = -100_000.0


@define
class RunLayer:
    """A layer of one model run, as a sequence of map-ready frames.

    Attributes:
        id: Layer id, e.g. `surface-temp`.
        name: Display name.
        maptype: Annotation maptype the frames were georegistered with (UW WRF only).
        fingerprint: `LayerConfig.fingerprint()` of the config the frames were exported with.
        hours: Every forecast hour the layer is published for, exported or not.
        dimensions: Image (width, height) in pixels, once a frame exists.
        coordinates: Image corner (lon, lat) pairs in Mapbox order (top-left, top-right,
            bottom-right, bottom-left), once a frame exists.
        frames: Exported frames, in hour order.
        legend: How to read the frames' colors; None for images that carry their own legend (UW WRF).
        values: Encoding of the frames' value images, if they have them.
    """

    id: str
    name: str
    maptype: str | None
    fingerprint: str
    hours: list[int]
    dimensions: tuple[int, int] | None = None
    coordinates: list[tuple[float, float]] | None = None
    frames: list[Frame] = field(factory=list)
    legend: Legend | None = None
    values: ValueEncoding | None = None

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
        registration_revision: The source's revision the frames were exported at: for UW WRF, the revision of
            the published annotations they were georegistered with; None for sources without one.
        updated_at: When the manifest was last written.
        layers: The run's layers, in display order.
        attribution: Credit for the data, to show with it (e.g. a CC BY license's attribution).
    """

    model: str
    name: str
    initialized: datetime
    registration_revision: int | None
    updated_at: datetime
    layers: list[RunLayer] = field(factory=list)
    attribution: str | None = None

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


@define
class ModelCheckStatus:
    """What the exporter's last finished check did for one model.

    Attributes:
        id: Model id.
        state: `ok` (checked, whether or not anything was new), `skipped` (its source can't export, e.g. no
            annotations are published) or `failed`.
        frames: Frames exported.
        error: Why the model failed or was skipped.
        layer_errors: Frames that failed and will be retried at the next check, e.g. `surface-temp hour 3: HTTP 503`.
        seconds: How long the model took.
    """

    id: str
    state: Literal["ok", "skipped", "failed"]
    frames: int = 0
    error: str | None = None
    layer_errors: list[str] = field(factory=list)
    seconds: float = 0.0


@define
class ExportStatus:
    """The exporter's health: `forecasts/export-status.json`, written as each check starts and finishes.

    Attributes:
        state: `running` (a check is under way), `ok` (the last check finished and no model failed) or
            `failed` (the last check failed, as a whole or for some model).
        check: The check's number since the exporter started, as its log lines are tagged (`[check 12]`).
        started_at: When the current or last check started.
        finished_at: When the last check finished; None while one is running.
        next_check_at: When the next check is due; None while one is running.
        overdue_after: When to suspect the exporter is stuck or down: the running check's time limit, or a
            little after the next check is due.
        last_success_at: When a check last finished with no model failing.
        consecutive_failures: Failed checks in a row.
        error: Why the check as a whole failed (e.g. it hit its time limit).
        models: Each model's outcome in the last finished check.
    """

    state: Literal["running", "ok", "failed"]
    check: int
    started_at: datetime
    overdue_after: datetime
    finished_at: datetime | None = None
    next_check_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    error: str | None = None
    models: list[ModelCheckStatus] = field(factory=list)

    def to_json(self) -> bytes:
        return converter.dumps(self).encode()

    @classmethod
    def from_json(cls, data: bytes | str) -> Self:
        return converter.loads(data, cls)


@define
class ModelIndex:
    """One model's entry in the index: what its pointers say and which runs are kept.

    Attributes:
        id: Model id.
        latest: `latest.json`, or None if no run is complete.
        in_progress: `in-progress.json`, or None if no run has started.
        runs: Initialization times (UTC) of the runs that have a manifest, newest first.
    """

    id: str
    latest: RunPointer | None = None
    in_progress: InProgress | None = None
    runs: list[datetime] = field(factory=list)


@define
class ForecastIndex:
    """Every model's pointers and kept runs in one object: `forecasts/index.json`.

    The exporter writes it after each model's check, from that model's files in storage, so the server can
    read one object instead of listing every model's runs. The per-model files stay the source of truth.

    Attributes:
        models: Each exported model's entry, in config order.
        updated_at: When the index was last written.
    """

    models: list[ModelIndex] = field(factory=list)
    updated_at: datetime | None = None

    def model(self, model_id: str) -> ModelIndex | None:
        """Look up a model's entry by id."""
        return next((model for model in self.models if model.id == model_id), None)

    def to_json(self) -> bytes:
        return converter.dumps(self).encode()

    @classmethod
    def from_json(cls, data: bytes | str) -> Self:
        return converter.loads(data, cls)


def valid_time(initialized: datetime, hour: int) -> datetime:
    """The time a forecast hour is valid for."""
    return initialized + timedelta(hours=hour)
