"""Open-Meteo's open data (https://github.com/open-meteo/open-data) as a source of runs for the archive.

Runs are read from `data_run/<domain>/<YYYY>/<MM>/<DD>/<HHMM>Z/`: one `.om` file per variable, shaped
(y, x, time), with `meta.json` written once the run is complete. Each file carries its lead times (`time`,
`forecast_reference_time`), `unit` and `crs_wkt`; the WKT's BBOX holds the first and last cells' latitude and
longitude, which with the file's shape gives the grid (Open-Meteo's grids are regular in their CRS).

A file is downloaded whole and cropped locally: its chunks are time series, so reading a region by ranges takes
thousands of requests, while the whole file (50-180 MB) downloads in seconds.

Each domain's surface height is a static file, `data/<domain>/static/HSURF.om` (y, x), whose CRS (with the same
BBOX convention) is in the `meta.json` beside it. Sea cells hold -999 there.
"""

import json
import re
import shutil
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
from attrs import define, field
from pyproj import Transformer

from upslope.archive.config import ArchiveModel, OpenMeteoRunsConfig
from upslope.archive.conversions import MissingField, UpstreamField
from upslope.archive.grid import RegionGrid
from upslope.archive.sources.base import FieldCache
from upslope.archive.terrain import StaticGrid
from upslope.logging import get_logger, log_duration
from upslope.models.dynamical.source import GridWindow, window_from_coords

logger = get_logger(__name__)

# The open-data bucket (us-west-2), read anonymously over HTTPS
BASE_URL = "https://openmeteo.s3.amazonaws.com/data_run"
STATIC_URL = "https://openmeteo.s3.amazonaws.com/data/{domain}/static"
# What Open-Meteo's static surface height holds over the sea
SEA_HEIGHT = -999.0
# Generous read timeout: a file streams for several seconds, but no single read should stall this long
HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_BBOX = re.compile(r"BBOX\[([^\]]+)\]")


def run_url(domain: str, initialized: datetime) -> str:
    """The folder of one run, e.g. `.../ecmwf_ifs025/2026/11/23/1200Z`."""
    return f"{BASE_URL}/{domain}/{initialized.astimezone(UTC):%Y/%m/%d/%H%M}Z"


def _domain(model: ArchiveModel) -> str:
    if not isinstance(model.source, OpenMeteoRunsConfig):
        raise TypeError(f"model {model.id} isn't an Open-Meteo model")
    return model.source.domain


def file_grid(crs: str, shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Cell-centre coordinates (xs, ys) of a file's grid, in its CRS, from the WKT's BBOX and the grid's shape.

    Raises ValueError if the WKT has no BBOX.
    """
    match = _BBOX.search(crs)
    if match is None:
        raise ValueError("the file's CRS has no BBOX to place its grid")
    lat0, lon0, lat1, lon1 = (float(v) for v in match.group(1).split(","))
    to_grid = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    (x0, x1), (y0, y1) = (np.asarray(v, dtype=np.float64) for v in to_grid.transform([lon0, lon1], [lat0, lat1]))
    rows, cols = shape
    return np.linspace(x0, x1, cols), np.linspace(y0, y1, rows)


def read_om(path: Path, region: tuple[float, float, float, float], initialized: datetime) -> tuple[UpstreamField, GridWindow]:
    """Read a run file's region (blocking): its values as an `UpstreamField`, and the window they cover."""
    from omfiles import OmFileReader

    reader = OmFileReader.from_path(str(path))
    try:
        if len(reader.shape) != 3:
            raise ValueError(f"{path.name} is shaped {reader.shape}, not (y, x, time)")
        crs = reader.get_child_by_name("crs_wkt").read_scalar()
        reference = int(reader.get_child_by_name("forecast_reference_time").read_scalar())
        times = np.asarray(reader.get_child_by_name("time").read_array((slice(None),)), dtype=np.int64)
        units = str(reader.get_child_by_name("unit").read_scalar())
        if reference != int(initialized.timestamp()):
            raise ValueError(f"{path.name} is from the run at {datetime.fromtimestamp(reference, UTC):%Y-%m-%d %HZ}")
        xs, ys = file_grid(crs, reader.shape[:2])
        window = window_from_coords(xs, ys, crs, region)
        data = np.asarray(reader.read_array((window.rows, window.cols, slice(None))), dtype=np.float32)
    finally:
        reader.close()
    values = np.moveaxis(data, 2, 0)
    if window.flip:
        values = values[:, ::-1, :]
    return UpstreamField((times - reference) / 3600.0, np.ascontiguousarray(values), units), window


def read_static_height(path: Path, crs: str) -> StaticGrid:
    """Read a domain's `HSURF.om` (blocking): its surface height in metres, with the sea at 0."""
    from omfiles import OmFileReader

    reader = OmFileReader.from_path(str(path))
    try:
        if len(reader.shape) != 2:
            raise ValueError(f"{path.name} is shaped {reader.shape}, not (y, x)")
        values = np.asarray(reader.read_array((slice(None), slice(None))), dtype=np.float32)
    finally:
        reader.close()
    xs, ys = file_grid(crs, values.shape)
    # the sea's surface is at sea level; Open-Meteo marks it so its own point lookups prefer land cells
    return StaticGrid(np.where(values == SEA_HEIGHT, np.float32(0), values), crs, xs, ys)


@define
class OpenMeteoRunReader:
    """One run of an Open-Meteo domain (see `RunReader`). Files are downloaded to a temporary folder, one at a time."""

    client: httpx.Client
    url: str
    region: tuple[float, float, float, float]
    initialized: datetime
    variables: frozenset[str]
    _folder: Path = field(factory=lambda: Path(tempfile.mkdtemp(prefix="upslope-om-")))
    _window: GridWindow | None = field(default=None, init=False)
    _cache: FieldCache = field(factory=FieldCache)

    @property
    def grid(self) -> RegionGrid:
        if self._window is None:
            raise RuntimeError("the grid is known once a field has been read")
        window = self._window
        return RegionGrid(window.crs, window.transform, (window.rows.stop - window.rows.start, window.cols.stop - window.cols.start))

    def read(self, name: str) -> UpstreamField:
        if (cached := self._cache.get(name)) is not None:
            return cached
        if name not in self.variables:
            raise MissingField(f"{self.url} has no variable {name!r}")
        path = self._folder / f"{name}.om"
        try:
            with log_duration(logger, f"downloaded {name}"):
                self._download(f"{self.url}/{name}.om", path)
            upstream, window = read_om(path, self.region, self.initialized)
        finally:
            path.unlink(missing_ok=True)
        if self._window is None:
            self._window = window
        elif (window.crs, window.transform, window.rows, window.cols) != (self._window.crs, self._window.transform, self._window.rows, self._window.cols):
            raise ValueError(f"{name} is on a different grid from the run's other variables")
        return self._cache.put(name, upstream)

    def _download(self, url: str, path: Path) -> None:
        with self.client.stream("GET", url) as response:
            if response.status_code in (403, 404):
                raise MissingField(f"{url} not found")
            response.raise_for_status()
            with path.open("wb") as out:
                for chunk in response.iter_bytes(1 << 20):
                    out.write(chunk)

    def close(self) -> None:
        self._cache.clear()
        shutil.rmtree(self._folder, ignore_errors=True)


@define
class OpenMeteoRunSource:
    """Runs from Open-Meteo's `data_run` (see `ArchiveSource`).

    Attributes:
        client: HTTP client for the bucket (tests pass one with a mock transport).
    """

    client: httpx.Client = field(factory=lambda: httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True))
    _lock: threading.Lock = field(factory=threading.Lock, init=False)
    _heights: dict[str, StaticGrid] = field(factory=dict, init=False)

    def run_meta(self, model: ArchiveModel, initialized: datetime) -> dict | None:
        """A run's `meta.json` (its variables and valid times), or None if the run isn't complete."""
        response = self.client.get(f"{run_url(_domain(model), initialized)}/meta.json")
        if response.status_code in (403, 404):
            return None
        response.raise_for_status()
        return json.loads(response.content)

    def is_complete(self, model: ArchiveModel, initialized: datetime) -> bool:
        # Open-Meteo writes meta.json once every variable of the run is written
        return self.run_meta(model, initialized) is not None

    def open_run(self, model: ArchiveModel, initialized: datetime) -> OpenMeteoRunReader:
        meta = self.run_meta(model, initialized)
        if meta is None:
            raise MissingField(f"{_domain(model)} has no complete run {initialized:%Y-%m-%d %HZ}")
        variables = frozenset(meta.get("variables", ()))
        return OpenMeteoRunReader(self.client, run_url(_domain(model), initialized), model.region, initialized.astimezone(UTC), variables)

    def surface_height(self, domain: str) -> StaticGrid:
        """A domain's static surface height in metres, read once per check (see `StaticHeights`).

        Raises MissingField if the domain has none.
        """
        with self._lock:
            if (heights := self._heights.get(domain)) is not None:
                return heights
        url = STATIC_URL.format(domain=domain)
        meta = self.client.get(f"{url}/meta.json")
        response = self.client.get(f"{url}/HSURF.om")
        if meta.status_code in (403, 404) or response.status_code in (403, 404):
            raise MissingField(f"{url} has no surface height")
        meta.raise_for_status()
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(prefix="upslope-om-", suffix=".om") as file:
            file.write(response.content)
            file.flush()
            heights = read_static_height(Path(file.name), json.loads(meta.content)["crs_wkt"])
        with self._lock:
            self._heights[domain] = heights
        return heights

    def end_check(self) -> None:
        """Drop the static heights read in the check; nothing else is kept between runs."""
        with self._lock:
            self._heights.clear()
