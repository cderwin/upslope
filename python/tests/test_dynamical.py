"""dynamical.org source: config, grids, derived fields, publishing and rendering, against small in-memory datasets."""

import asyncio
import hashlib
import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
import rasterio
import xarray as xr
from attrs import evolve
from PIL import Image
from pyproj import CRS, Transformer

from upslope.clients.forecasts import ForecastStore
from upslope.models.config import (
    ConfigError,
    DynamicalLayerConfig,
    DynamicalSourceConfig,
    FieldConfig,
    HourRange,
    ModelConfig,
    ModelsConfig,
    ValueQuantization,
    VariableConfig,
)
from upslope.models.driver import ForecastDriver
from upslope.models.manifest import RunManifest
from upslope.models.dynamical.catalog import anonymous_credentials, store_url
from upslope.models.dynamical.scales import NODATA_INDEX, SCALES, convert
from upslope.models.dynamical.source import DynamicalSource, grid_window
from upslope.models.source import NotPublished
from upslope.raster import OUTPUT_CRS, decode_lookup, decode_values

RUN = datetime(2026, 11, 8, 0, tzinfo=UTC)
PREVIOUS = RUN - timedelta(hours=6)
LEADS = [0, 1, 2, 3, 6]
REGION = (-126.0, 44.0, -116.0, 49.0)


def lat_lon_dataset(published: dict[datetime, int] | None = None) -> xr.Dataset:
    """A dataset like GFS's: latitude north to south, longitude west to east, two runs.

    Every variable is constant over the grid at each lead time, so derived values are easy to check. A run has
    data up to `published[run]` (default: every lead); later leads are NaN, as unwritten chunks read back.
    """
    published = published or {PREVIOUS: 6, RUN: 6}
    runs = [PREVIOUS, RUN]
    lats = np.arange(52.0, 38.0, -0.5)
    lons = np.arange(-132.0, -108.0, 0.5)
    shape = (len(runs), len(LEADS), len(lats), len(lons))

    def field(per_lead: list[float]) -> np.ndarray:
        data = np.broadcast_to(np.array(per_lead, dtype=np.float32)[None, :, None, None], shape).copy()
        for i, run in enumerate(runs):
            data[i, [j for j, lead in enumerate(LEADS) if lead > published.get(run, -1)]] = np.nan
        return data

    dims = ("init_time", "lead_time", "latitude", "longitude")
    freezing = field([1500.0] * len(LEADS))
    freezing[:, :, :3, :] = 0.0  # at or below the surface, in GFS's convention
    freezing[:, :, 3:6, :] = -50.0  # RRFS's: below the surface
    freezing[:, :, 6:9, :] = np.nan  # HRRR's: missing
    return xr.Dataset(
        {
            "temperature_2m": (dims, field([0.0, 10.0, 20.0, 30.0, 40.0]), {"units": "degree_Celsius"}),
            # run totals: 0, 1, 3, 6 and 12 mm
            "total_precipitation_run_total_surface": (dims, field([0.0, 1.0, 3.0, 6.0, 12.0]), {"units": "kg m-2"}),
            # averaged since the previous lead: 1 mm/h for the first three hours, then 2 mm/h
            "precipitation_surface": (dims, field([np.nan, 1 / 3600, 1 / 3600, 1 / 3600, 2 / 3600]), {"units": "kg m-2 s-1"}),
            "wind_u_10m": (dims, field([3.0] * len(LEADS)), {"units": "m s-1"}),
            "wind_v_10m": (dims, field([4.0] * len(LEADS)), {"units": "m s-1"}),
            "geopotential_height_0c_isotherm": (dims, freezing, {"units": "m"}),
        },
        coords={
            "init_time": [np.datetime64(run.replace(tzinfo=None), "ns") for run in runs],
            "lead_time": [np.timedelta64(lead, "h") for lead in LEADS],
            "latitude": lats,
            "longitude": lons,
            "spatial_ref": ((), 0, {"crs_wkt": CRS.from_epsg(4326).to_wkt()}),
        },
    )


LAMBERT = "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 +R=6371229 +units=m +no_defs"


def lambert_dataset() -> xr.Dataset:
    """A dataset like HRRR's: a Lambert conformal grid with y increasing (south to north)."""
    x0, y0 = Transformer.from_crs("EPSG:4326", LAMBERT, always_xy=True).transform(-121.0, 46.5)
    xs = x0 + np.arange(-300, 300) * 3000.0
    ys = y0 + np.arange(-200, 200) * 3000.0
    data = np.full((1, 1, len(ys), len(xs)), 5.0, dtype=np.float32)
    return xr.Dataset(
        {"temperature_2m": (("init_time", "lead_time", "y", "x"), data, {"units": "degree_Celsius"})},
        coords={
            "init_time": [np.datetime64(RUN.replace(tzinfo=None), "ns")],
            "lead_time": [np.timedelta64(0, "h")],
            "x": xs,
            "y": ys,
            "spatial_ref": ((), 0, {"crs_wkt": CRS.from_proj4(LAMBERT).to_wkt()}),
        },
    )


# how test layers store their values, by scale
QUANTIZATION = {
    "temperature_f": ValueQuantization(0.1, -80, 140), "precip_in": ValueQuantization(0.001, 0, 20),
    "wind_mph": ValueQuantization(0.1, 0, 250), "height_ft": ValueQuantization(1, 0, 30000),
    "snow_in": ValueQuantization(0.01, 0, 600),
}


def layer(layer_id: str, field: FieldConfig, scale: str, first: int = 0, last: int = 6, step: int = 3) -> DynamicalLayerConfig:
    return DynamicalLayerConfig(
        layer_id, layer_id.title(), field, scale, HourRange(first=first, last=last, step=step), values=QUANTIZATION.get(scale)
    )


TEMPERATURE = layer("surface-temp", FieldConfig(("temperature_2m",)), "temperature_f")
PRECIP = layer("3-hour-precip", FieldConfig(("total_precipitation_run_total_surface",), "change", 3), "precip_in", first=3)
PRECIP_FROM_RATE = layer("3-hour-precip-rate", FieldConfig(("precipitation_surface",), "rate_sum", 3), "precip_in", first=3)
WIND = layer("10m-wind-speed", FieldConfig(("wind_u_10m", "wind_v_10m"), "wind_speed"), "wind_mph")
FREEZING = layer("freezing-level", FieldConfig(("geopotential_height_0c_isotherm",), "freezing_level"), "height_ft")


def make_model(*layers: DynamicalLayerConfig, region=REGION, dataset: str = "test-dataset") -> ModelConfig:
    return ModelConfig(
        "gfs_test", "GFS test", [0, 6, 12, 18], list(layers) or [TEMPERATURE],
        source=DynamicalSourceConfig(dataset), region=region, attribution="Test data (CC BY 4.0)",
    )


def make_source(dataset: xr.Dataset) -> DynamicalSource:
    return DynamicalSource(open=lambda dataset_id: dataset)


# --- config


def test_config_loads_dynamical_models(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        "default_model: gfs_0.25\n"
        "variables:\n"
        "  - {id: surface-temp, name: T, values: {step: 0.1, min: -80, max: 140}}\n"
        "  - {id: 3-hour-precip, name: P, values: {step: 0.001, min: 0, max: 20}}\n"
        "models:\n"
        "  - id: gfs_0.25\n"
        "    name: GFS\n"
        "    source: {kind: dynamical, dataset: noaa-gfs-forecast-virtual}\n"
        "    region: [-127, 41.5, -113, 50]\n"
        "    start_times: [0, 6, 12, 18]\n"
        "    layers:\n"
        "      - {id: surface-temp, field: temperature_2m, scale: temperature_f, hours: {last: 240}}\n"
        "      - id: 3-hour-precip\n"
        "        field: {derive: change, variables: [total_precipitation_run_total_surface], hours: 3}\n"
        "        scale: precip_in\n"
        "        hours: {first: 3, last: 240}\n"
    )
    (model,) = ModelsConfig.load(path).models
    assert model.source == DynamicalSourceConfig("noaa-gfs-forecast-virtual")
    assert model.layers[0].field == FieldConfig(("temperature_2m",))
    assert model.layers[1].field == FieldConfig(("total_precipitation_run_total_surface",), "change", 3)
    assert model.layers[0].maptype is None
    assert ModelsConfig([model]).maptypes == set()
    # each layer stores values as its variable says
    assert model.layers[0].values == ValueQuantization(0.1, -80, 140)
    assert model.layers[1].values == ValueQuantization(0.001, 0, 20)


@pytest.mark.parametrize(
    "build",
    [
        lambda: FieldConfig(("u",), "wind_speed"),
        lambda: FieldConfig(("a", "b")),
        lambda: FieldConfig(("a",), "change"),
        lambda: FieldConfig(("a",), "value", 3),
        lambda: FieldConfig(("a",), "rate_sum", 0),
        lambda: layer("a", FieldConfig(("a",)), "no-such-scale"),
        # a 3-hour total can't start before hour 3
        lambda: layer("a", FieldConfig(("a",), "change", 3), "precip_in", first=0),
        lambda: make_model(region=None),
        lambda: make_model(region=(-116.0, 44.0, -126.0, 49.0)),
        lambda: ModelConfig("m", "M", [0], [TEMPERATURE]),  # a dynamical layer in a UW WRF model
    ],
)
def test_invalid_dynamical_config_raises(build) -> None:
    with pytest.raises(ConfigError):
        build()


def test_layer_fingerprint_covers_dataset_region_and_scale() -> None:
    base = make_model()
    fingerprint = base.layer_fingerprint(TEMPERATURE)
    assert make_model(dataset="other").layer_fingerprint(TEMPERATURE) != fingerprint
    assert make_model(region=(-127.0, 44.0, -116.0, 49.0)).layer_fingerprint(TEMPERATURE) != fingerprint
    assert base.layer_fingerprint(layer("surface-temp", TEMPERATURE.field, "wind_mph")) != fingerprint
    # display names don't change frames
    renamed = DynamicalLayerConfig("surface-temp", "Temperature", TEMPERATURE.field, TEMPERATURE.scale, TEMPERATURE.hours, TEMPERATURE.values)
    assert base.layer_fingerprint(renamed) == fingerprint
    # how values are stored does, and a layer without values has the fingerprint layers had before values were stored
    assert base.layer_fingerprint(evolve(TEMPERATURE, values=ValueQuantization(0.2, -80, 140))) != fingerprint
    without = evolve(TEMPERATURE, values=None).fingerprint()
    assert without != TEMPERATURE.fingerprint()
    payload = {"field": [["temperature_2m"], "value", None], "scale": SCALES["temperature_f"].fingerprint_payload(), "hours": [0, 3, 6]}
    assert without == hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@pytest.mark.parametrize(
    ("variables", "error"),
    [
        ("  - {id: surface-temp, name: T}\n", "needs `values`"),
        ("  - {id: surface-temp, name: T, values: {step: 0.001, min: -80, max: 140}}\n", "int16 codes"),
        ("  - {id: surface-temp, name: T, values: {step: 0.1, min: 10, max: 10}}\n", "min < max"),
    ],
)
def test_invalid_value_quantization_raises(tmp_path: Path, variables: str, error: str) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        "default_model: gfs_0.25\nvariables:\n" + variables + "models:\n"
        "  - {id: gfs_0.25, name: GFS, source: {kind: dynamical, dataset: d}, region: [-127, 41.5, -113, 50],"
        " start_times: [0], layers: [{id: surface-temp, field: temperature_2m, scale: temperature_f, hours: {last: 6}}]}\n"
    )
    with pytest.raises(ConfigError, match=error):
        ModelsConfig.load(path)


def test_a_variables_layers_must_share_units() -> None:
    """A step only means one thing if every dynamical.org layer of the variable shows the same units."""
    values = ValueQuantization(0.1, 0, 250)
    variable = VariableConfig("surface-temp", "Surface-Temp", values=values)
    fahrenheit = make_model(evolve(TEMPERATURE, values=values))
    mph = make_model(evolve(TEMPERATURE, scale="wind_mph", values=values), dataset="other")
    mph = evolve(mph, id="other")
    with pytest.raises(ConfigError, match="different units"):
        ModelsConfig([fahrenheit, mph], [variable])


# --- grids


def test_grid_window_on_a_lat_lon_grid() -> None:
    dataset = lat_lon_dataset()
    window = grid_window(dataset, REGION)
    lats = dataset.latitude.values[window.rows]
    lons = dataset.longitude.values[window.cols]
    # the region plus two cells of margin each side
    assert (lats.max(), lats.min()) == (50.0, 43.0)
    assert (lons.min(), lons.max()) == (-127.0, -115.0)
    assert not window.flip
    assert window.transform @ (0, 0) == (-127.25, 50.25)


def test_grid_window_on_a_projected_grid_puts_north_up() -> None:
    dataset = lambert_dataset()
    window = grid_window(dataset, REGION)
    assert window.flip and window.transform.e < 0
    to_lambert = Transformer.from_crs("EPSG:4326", LAMBERT, always_xy=True)
    left, top = window.transform @ (0, 0)
    right, bottom = window.transform @ (window.cols.stop - window.cols.start, window.rows.stop - window.rows.start)
    for lon, lat in [(REGION[0], REGION[1]), (REGION[2], REGION[3]), (REGION[0], REGION[3]), (REGION[2], REGION[1])]:
        x, y = to_lambert.transform(lon, lat)
        assert left < x < right and bottom < y < top


def test_grid_window_outside_the_grid_raises() -> None:
    with pytest.raises(ValueError, match="outside the grid"):
        grid_window(lat_lon_dataset(), (10.0, 10.0, 20.0, 20.0))


@contextmanager
def checking(source: DynamicalSource) -> Iterator[None]:
    """A check, for calling the source's blocking methods directly."""
    source.begin_check()
    try:
        yield
    finally:
        source.end_check()


def export(driver: ForecastDriver) -> list[RunManifest]:
    report = asyncio.run(driver.run())
    assert not report.failed, report
    return report.manifests


# --- fields


def values(source: DynamicalSource, model: ModelConfig, layer: DynamicalLayerConfig, hour: int, run: datetime = RUN) -> np.ndarray:
    with checking(source):
        return source.field_values(model, layer, run, hour)


def test_fields_are_derived_and_converted() -> None:
    source = make_source(lat_lon_dataset())
    model = make_model()
    np.testing.assert_allclose(values(source, model, TEMPERATURE, 3), 86.0, rtol=1e-6)  # 30 °C
    # 6 mm of run total minus none at hour 0, then 12 - 6 mm
    np.testing.assert_allclose(values(source, model, PRECIP, 3), 6 / 25.4, rtol=1e-5)
    np.testing.assert_allclose(values(source, model, PRECIP, 6), 6 / 25.4, rtol=1e-5)
    # rates summed over each step's length, across the change from 1- to 3-hour steps
    np.testing.assert_allclose(values(source, model, PRECIP_FROM_RATE, 3), 3 / 25.4, rtol=1e-5)
    np.testing.assert_allclose(values(source, model, PRECIP_FROM_RATE, 6), 6 / 25.4, rtol=1e-5)
    np.testing.assert_allclose(values(source, model, WIND, 0), 5 * 2.2369363, rtol=1e-6)


def test_freezing_levels_at_or_below_sea_level_are_zero() -> None:
    source = make_source(lat_lon_dataset())
    model = make_model(region=(-126.0, 39.0, -116.0, 51.5))
    result = values(source, model, FREEZING, 0)
    assert not np.isnan(result).any()
    # rows 0-8 of the grid held 0, -50 and NaN; the window starts at the grid's first row
    np.testing.assert_allclose(result[:9], 0.0)
    np.testing.assert_allclose(result[9:], 1500 * 3.2808399, rtol=1e-6)


def test_unpublished_hours_raise_not_published() -> None:
    source = make_source(lat_lon_dataset({PREVIOUS: 6, RUN: 2}))
    model = make_model()
    np.testing.assert_allclose(values(source, model, TEMPERATURE, 0), 32.0)
    with pytest.raises(NotPublished):
        values(source, model, TEMPERATURE, 3)
    # a derived field needs every hour it's made from
    with pytest.raises(NotPublished):
        values(source, model, PRECIP, 3)
    # a run the dataset doesn't have yet
    with pytest.raises(NotPublished):
        values(source, model, TEMPERATURE, 0, run=RUN + timedelta(hours=6))


def test_config_that_doesnt_fit_the_dataset_raises() -> None:
    source = make_source(lat_lon_dataset())
    model = make_model()
    with pytest.raises(ValueError, match="no variable"):
        values(source, model, layer("snow", FieldConfig(("snowfall",)), "snow_in"), 0)
    with pytest.raises(ValueError, match="isn't a lead time"):
        values(source, model, TEMPERATURE, 4)
    with pytest.raises(ValueError, match="convert"):
        values(source, model, layer("t", FieldConfig(("temperature_2m",)), "wind_mph"), 0)
    with pytest.raises(RuntimeError, match="check"):
        source.field_values(model, TEMPERATURE, RUN, 0)


def test_latest_complete_run_needs_the_last_hour() -> None:
    model = make_model(TEMPERATURE)
    now = RUN + timedelta(hours=4)
    source = make_source(lat_lon_dataset({PREVIOUS: 6, RUN: 3}))
    with checking(source):
        assert source.find_latest_complete_run(model, now) == PREVIOUS
    source = make_source(lat_lon_dataset())

    async def latest() -> datetime | None:
        async with source.check():
            return await source.latest_complete_run(model, now)

    assert asyncio.run(latest()) == RUN
    with checking(source):
        assert source.layer_problems(model, TEMPERATURE, RUN) == []
    source = make_source(lat_lon_dataset({PREVIOUS: 6, RUN: 3}))
    with checking(source):
        assert source.layer_problems(model, TEMPERATURE, RUN) == ["hours [6] aren't published yet"]
        assert source.layer_problems(model, layer("x", FieldConfig(("nope",)), "snow_in"), RUN) == ["test-dataset has no variable 'nope'"]



# --- opening datasets concurrently

WAIT = 5  # seconds; a test fails rather than hangs if a thread never gets through


class GatedOpen:
    """Opens datasets that wait for `release` (those in `slow`), recording how many are open at once."""

    def __init__(self, dataset: xr.Dataset, slow: set[str]) -> None:
        self.dataset = dataset
        self.slow = slow
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls: list[str] = []
        self.active = 0
        self.most_active = 0
        self._lock = threading.Lock()

    def __call__(self, dataset_id: str) -> xr.Dataset:
        with self._lock:
            self.calls.append(dataset_id)
            self.active += 1
            self.most_active = max(self.most_active, self.active)
        try:
            if dataset_id in self.slow:
                self.started.set()
                assert self.release.wait(WAIT), "never released"
            return self.dataset
        finally:
            with self._lock:
                self.active -= 1


def test_a_slow_dataset_doesnt_block_other_datasets_or_the_end_of_the_check() -> None:
    opener = GatedOpen(lat_lon_dataset(), slow={"slow-dataset"})
    source = DynamicalSource(open=opener)
    slow, fast = make_model(dataset="slow-dataset"), make_model(dataset="fast-dataset")
    source.begin_check()
    stuck = threading.Thread(target=source.field_values, args=(slow, TEMPERATURE, RUN, 0), daemon=True)
    stuck.start()
    assert opener.started.wait(WAIT)
    # another dataset opens and reads while the slow one is still opening
    np.testing.assert_allclose(source.field_values(fast, TEMPERATURE, RUN, 0), 32.0)
    # and the check can end (on the event loop, in the exporter) without waiting for it
    source.end_check()
    assert stuck.is_alive()  # neither waited for it

    # the next check opens the dataset afresh rather than waiting on, or reusing, the stale open
    source.begin_check()
    opener.slow.clear()
    np.testing.assert_allclose(source.field_values(slow, TEMPERATURE, RUN, 0), 32.0)
    opener.release.set()
    stuck.join(WAIT)
    assert not stuck.is_alive()
    assert opener.calls.count("slow-dataset") == 2
    np.testing.assert_allclose(source.field_values(slow, TEMPERATURE, RUN, 0), 32.0)
    assert opener.calls.count("slow-dataset") == 2
    source.end_check()


def test_a_dataset_is_opened_once_by_concurrent_readers() -> None:
    opener = GatedOpen(lat_lon_dataset(), slow={"test-dataset"})
    source = DynamicalSource(open=opener)
    model = make_model()
    with checking(source):
        readers = [threading.Thread(target=source.field_values, args=(model, TEMPERATURE, RUN, 0)) for _ in range(3)]
        for reader in readers:
            reader.start()
        assert opener.started.wait(WAIT)
        opener.release.set()
        for reader in readers:
            reader.join(WAIT)
    assert opener.calls == ["test-dataset"]


def test_finding_the_latest_run_shares_the_render_limit() -> None:
    """Every model looking for its latest run at once can't take more threads than rendering may."""
    opener = GatedOpen(lat_lon_dataset(), slow={f"dataset-{n}" for n in range(3)})
    source = DynamicalSource(open=opener, concurrency=1)
    models = [make_model(dataset=f"dataset-{n}") for n in range(3)]

    async def find() -> list[datetime | None]:
        async with source.check():
            asyncio.get_running_loop().call_later(0.2, opener.release.set)
            return await asyncio.gather(*(source.latest_complete_run(m, RUN + timedelta(hours=7)) for m in models))

    assert asyncio.run(find()) == [RUN] * 3
    assert opener.most_active == 1


# --- rendering


def test_render_frame_writes_a_paletted_web_mercator_frame_and_its_values(tmp_path: Path) -> None:
    source = make_source(lat_lon_dataset())
    model = make_model()
    out = tmp_path / "frame.tiff"

    async def render():
        async with source.check():
            return await source.render_frame(model, TEMPERATURE, RUN, 3, out)

    frame = asyncio.run(render())
    with rasterio.open(out) as dataset:
        assert dataset.crs == OUTPUT_CRS and dataset.count == 1 and dataset.nodata == NODATA_INDEX
        assert dataset.width >= 600  # upsampled from the coarse grid
        indices = dataset.read(1)
        assert dataset.colormap(1)[int(indices[indices != NODATA_INDEX][0])][:3] == (253, 174, 97)  # 80-90 °F
        # the values stay on the model's grid: far fewer cells than the upsampled image has pixels
        assert frame.values.size * 50 < dataset.width * dataset.height
    with checking(source):
        window = grid_window(source._dataset(model), model.region)
    assert frame.values.shape == (window.rows.stop - window.rows.start, window.cols.stop - window.cols.start)
    assert (frame.crs, frame.transform) == (window.crs, window.transform)
    np.testing.assert_allclose(frame.values, 86.0, rtol=1e-5)
    assert frame.legend == SCALES["temperature_f"].legend()


def test_render_frame_on_a_projected_grid(tmp_path: Path) -> None:
    model = make_model(layer("surface-temp", FieldConfig(("temperature_2m",)), "temperature_f", last=0))
    source = make_source(lambert_dataset())
    out = tmp_path / "frame.tiff"
    with checking(source):
        frame = source.render(model, model.layers[0], RUN, 0, out)
    # the grid is rotated against Web Mercator, so the image's corners are empty, but the region is covered
    to_mercator = Transformer.from_crs("EPSG:4326", OUTPUT_CRS, always_xy=True)
    to_grid = Transformer.from_crs("EPSG:4326", frame.crs, always_xy=True)
    with rasterio.open(out) as dataset:
        for lon, lat in [(REGION[0], REGION[1]), (REGION[2], REGION[3]), (-121.0, 46.5)]:
            row, col = dataset.index(*to_mercator.transform(lon, lat))
            assert dataset.read(1)[row, col] != NODATA_INDEX
            # and the values cover it on the Lambert grid they came from
            grid_col, grid_row = ~frame.transform @ to_grid.transform(lon, lat)
            assert frame.values[int(grid_row), int(grid_col)] == pytest.approx(41.0)
    np.testing.assert_allclose(frame.values[np.isfinite(frame.values)], 41.0, rtol=1e-5)


def test_driver_exports_a_dynamical_model(memory_storage) -> None:
    """End to end: a run publishing partway exports what it has, with legends, values and attribution."""
    model = make_model(TEMPERATURE, PRECIP)
    store = ForecastStore(memory_storage)
    source = make_source(lat_lon_dataset({PREVIOUS: 6, RUN: 3}))
    driver = ForecastDriver(ModelsConfig([model]), {"dynamical": source}, store, now=lambda: RUN + timedelta(hours=4))
    complete, partial = export(driver)
    assert (complete.initialized, complete.complete, partial.initialized, partial.complete) == (PREVIOUS, True, RUN, False)
    assert complete.registration_revision is None and complete.attribution == "Test data (CC BY 4.0)"
    assert [(l.id, [f.hour for f in l.frames]) for l in partial.layers] == [("surface-temp", [0, 3]), ("3-hour-precip", [3])]
    temperature = partial.layer("surface-temp")
    assert temperature.maptype is None and temperature.legend.units == "°F" and temperature.values is not None
    assert store.read_latest("gfs_test").initialized == PREVIOUS

    prefix = "forecasts/gfs_test/2026/11/08/0000Z"
    data, content_type = memory_storage.objects[f"{prefix}/{temperature.frames[1].values}"]
    assert content_type == "image/tiff"
    values, crs, transform = decode_values(data)
    np.testing.assert_allclose(values, 86.0)  # 30 °C, to a tenth of a degree
    assert (temperature.values.scale, temperature.values.offset) == (0.1, 30.0)
    # the lookup maps the middle of the image into the values' grid
    lookup = decode_lookup(memory_storage.objects[f"{prefix}/{temperature.values.lookup}"][0])
    col, row = lookup[:, lookup.shape[1] // 2, lookup.shape[2] // 2]
    assert 0 < col < values.shape[1] and 0 < row < values.shape[0]


# --- catalog


def test_store_url_reads_the_stac_collection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://stac.dynamical.org/noaa-gfs-forecast-virtual/collection.json"
        return httpx.Response(200, json={"assets": {"icechunk-https": {"href": "https://bucket/gfs/v0.1.0.icechunk"}}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert store_url("noaa-gfs-forecast-virtual", client) == "https://bucket/gfs/v0.1.0.icechunk"


def test_virtual_chunk_buckets_are_pinned_to_aws(monkeypatch) -> None:
    """AWS_ENDPOINT_URL_S3 names our own bucket; upstream chunks must not be fetched from there."""
    import icechunk

    from upslope.models.dynamical.catalog import pin_aws_endpoints

    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://minio:9000")
    config = icechunk.RepositoryConfig.default()
    config.set_virtual_chunk_container(icechunk.VirtualChunkContainer("s3://noaa-gfs-bdp-pds/", icechunk.s3_store(region="us-east-1")))
    config.set_virtual_chunk_container(icechunk.VirtualChunkContainer("gs://ecmwf-open-data/", icechunk.gcs_store()))
    pin_aws_endpoints(config)
    s3 = config.get_virtual_chunk_container("s3://noaa-gfs-bdp-pds/").store
    assert s3[0].endpoint_url == "https://s3.us-east-1.amazonaws.com" and s3[0].region == "us-east-1"
    assert isinstance(config.get_virtual_chunk_container("gs://ecmwf-open-data/").store, icechunk.ObjectStoreConfig.Gcs)


def test_anonymous_credentials_by_bucket_kind() -> None:
    assert anonymous_credentials("s3://noaa-gfs-bdp-pds/") is not None
    assert anonymous_credentials("gs://ecmwf-open-data/") is not None
    with pytest.raises(ValueError):
        anonymous_credentials("az://somewhere/")


def test_unit_conversions() -> None:
    np.testing.assert_allclose(convert(np.array([0.0, 100.0]), "degree_Celsius", SCALES["temperature_f"]), [32.0, 212.0])
    np.testing.assert_allclose(convert(np.array([25.4]), "kg m-2", SCALES["precip_in"]), [1.0])
    np.testing.assert_allclose(convert(np.array([0.0254]), "m", SCALES["snow_in"]), [1.0])
    np.testing.assert_allclose(convert(np.array([0.3048]), "m", SCALES["height_ft"]), [1.0])


def test_scale_colors_values() -> None:
    scale = SCALES["precip_in"]
    indices = scale.colorize(np.array([np.nan, 0.0, 0.01, 0.07, 10.0]))
    assert list(indices) == [NODATA_INDEX, NODATA_INDEX, 0, 1, len(scale.stops) - 1]
    temperature = SCALES["temperature_f"]
    # not see-through: values under the first stop get its color
    assert temperature.colorize(np.array([-40.0]))[0] == 0
