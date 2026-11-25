"""The forecast archive: config, conversions, writing runs, the store and driver, and both sources, offline."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
import xarray as xr
from affine import Affine
from pyproj import CRS, Transformer

from upslope.archive.config import (
    ArchiveConfig,
    ArchiveDefaults,
    ArchiveModel,
    ArchiveVariable,
    ChunkShape,
    DynamicalRunsConfig,
    OpenMeteoRunsConfig,
    ShardShape,
    SourceField,
    TerrainConfig,
)
from upslope.archive.conversions import MissingField, UpstreamField, align, convert_units, derive, standardize
from upslope.archive.driver import ArchiveDriver
from upslope.archive.grid import RegionGrid
from upslope.archive.loop import ArchiveLoop
from upslope.archive.sources.dynamical import DynamicalRunSource
from upslope.archive.sources.openmeteo import STATIC_URL, OpenMeteoRunSource, run_url
from upslope.archive.store import STATUS, ArchiveStore
from upslope.archive.terrain import StaticGrid, sample_static
from upslope.archive.writer import RunWriter
from upslope.cli import DEFAULT_ARCHIVE_CONFIG
from upslope.models.config import ConfigError, HourRange, ValueQuantization

RUN = datetime(2026, 11, 8, 0, tzinfo=UTC)
NOW = RUN + timedelta(hours=5)
REGION = (-126.0, 44.0, -116.0, 49.0)
LAMBERT = "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 +R=6371229 +units=m +no_defs"

TEMPERATURE = ArchiveVariable("temperature_2m", "2 metre temperature", "degC", ValueQuantization(0.05, -80, 60))
PRECIPITATION = ArchiveVariable("precipitation", "precipitation", "mm", ValueQuantization(0.01, 0, 300), kind="sum")
WIND_U = ArchiveVariable("wind_u_10m", "eastward wind", "m s-1", ValueQuantization(0.05, -150, 150))
WIND_V = ArchiveVariable("wind_v_10m", "northward wind", "m s-1", ValueQuantization(0.05, -150, 150))
VARIABLES = [TEMPERATURE, PRECIPITATION, WIND_U, WIND_V]


def lat_lon_grid(shape: tuple[int, int] = (10, 20)) -> RegionGrid:
    return RegionGrid(CRS.from_epsg(4326).to_wkt(), Affine(0.5, 0, -126.0, 0, -0.5, 49.0), shape)


def field(leads: list[float], per_lead: list[float], units: str, shape: tuple[int, int] = (2, 3)) -> UpstreamField:
    values = np.broadcast_to(np.array(per_lead, dtype=np.float32)[:, None, None], (len(leads), *shape)).copy()
    return UpstreamField(np.array(leads, dtype=np.float64), values, units)


def model(source=None, leads: list[HourRange] | None = None, variables: dict | None = None, **kwargs) -> ArchiveModel:
    return ArchiveModel(
        id="test",
        source=source or DynamicalRunsConfig("test-dataset"),
        region=REGION,
        start_times=[0, 6, 12, 18],
        leads=leads or [HourRange(last=6, step=3)],
        variables=variables or {
            "temperature_2m": SourceField(("temperature_2m",)),
            "precipitation": SourceField(("total_precipitation_run_total_surface",), "run_total"),
        },
        **kwargs,
    )


def config(*models: ArchiveModel, **defaults) -> ArchiveConfig:
    return ArchiveConfig(VARIABLES, list(models), ArchiveDefaults(ChunkShape(2, 4, 4), ShardShape(8, 8), **defaults))


# --- config


def test_repo_config_loads() -> None:
    loaded = ArchiveConfig.load(DEFAULT_ARCHIVE_CONFIG)
    ids = {m.id for m in loaded.models}
    assert {"hrrr_3km", "rrfs_3km", "gfs_0.25", "aifs_0.25", "hrdps_2.5km", "ifs_0.25", "nbm_2.5km"} <= ids
    gfs = loaded.model("gfs_0.25")
    assert len(gfs.lead_hours) == 209 and gfs.lead_hours[120:122] == [120, 123]
    chunks, shards = loaded.chunking(gfs)
    assert (chunks.leads, chunks.rows, shards.cols) == (24, 64, 512)  # a per-model override over the defaults
    assert isinstance(loaded.model("ifs_0.25").source, OpenMeteoRunsConfig)


def test_config_rejects_bad_models() -> None:
    with pytest.raises(ConfigError, match="isn't one of the variables"):
        config(model(variables={"snow_depth": SourceField(("snow",))}))
    with pytest.raises(ConfigError, match="whole chunks"):
        config(model(shards=ShardShape(rows=6)))
    with pytest.raises(ConfigError, match="gives a sum"):
        config(model(variables={"temperature_2m": SourceField(("t",), "run_total")}))
    with pytest.raises(ConfigError, match="takes 2 field"):
        SourceField(("u",), "grid_wind_u")
    with pytest.raises(ConfigError, match="increasing"):
        model(leads=[HourRange(last=6), HourRange(first=3, last=9)])


def test_config_load_reads_shorthand_and_errors(tmp_path: Path) -> None:
    path = tmp_path / "archive.yaml"
    path.write_text(
        "variables:\n  temperature_2m: {name: t, units: degC, values: {step: 0.1, min: -80, max: 60}}\n"
        "models:\n  - {id: m, source: {kind: openmeteo, domain: d}, region: [-126, 44, -116, 49], start_times: [0],"
        " leads: [{last: 6}], variables: {temperature_2m: temperature_2m}}\n"
    )
    loaded = ArchiveConfig.load(path)
    assert loaded.models[0].variables["temperature_2m"] == SourceField(("temperature_2m",))
    path.write_text(path.read_text().replace("units: degC", "units: furlongs"))
    with pytest.raises(ConfigError, match="unknown units"):
        ArchiveConfig.load(path)


def test_fingerprint_follows_what_is_stored() -> None:
    base = config(model())
    assert base.fingerprint(base.models[0]) == config(model()).fingerprint(model())
    assert config(model(chunks=ChunkShape(leads=4))).fingerprint(model(chunks=ChunkShape(leads=4))) != base.fingerprint(base.models[0])
    other = model(variables={"temperature_2m": SourceField(("t2m",))})
    assert config(other).fingerprint(other) != base.fingerprint(base.models[0])
    # adding terrain archives runs again, which is how existing runs gain it
    with_terrain = model(terrain=TerrainConfig(field="geopotential_height_surface"))
    assert config(with_terrain).fingerprint(with_terrain) != base.fingerprint(base.models[0])


def test_terrain_config() -> None:
    for wrong in (TerrainConfig, lambda: TerrainConfig(field="h", openmeteo="d")):
        with pytest.raises(ConfigError, match="one of field or openmeteo"):
            wrong()
    loaded = ArchiveConfig.load(DEFAULT_ARCHIVE_CONFIG)
    assert all(m.terrain is not None for m in loaded.models)
    assert loaded.model("hrrr_3km").terrain == TerrainConfig(field="geopotential_height_surface")
    assert loaded.model("hrdps_2.5km").terrain == TerrainConfig(openmeteo="cmc_gem_hrdps")


# --- terrain


def static_lat_lon(offset: float = 0.25, south: float = 30.0) -> StaticGrid:
    """A 0.5° static grid, rows south to north like Open-Meteo's: height = 10 x latitude + longitude. The default
    offset puts its cell centres on `lat_lon_grid`'s."""
    lats, lons = np.arange(south, 60.0, 0.5) + offset, np.arange(-140.0, -100.0, 0.5) + offset
    return StaticGrid((10 * lats[:, None] + lons[None, :]).astype(np.float32), CRS.from_epsg(4326).to_wkt(), lons, lats)


def test_static_terrain_is_sampled_only_where_the_grids_line_up() -> None:
    grid = lat_lon_grid((10, 20))
    lon, lat = grid.lon_lat()
    np.testing.assert_allclose(sample_static(static_lat_lon(), grid), 10 * lat + lon)  # north up, like the run
    with pytest.raises(ValueError, match="0.50 cells"):
        sample_static(static_lat_lon(offset=0.0), grid)  # cells half a cell apart: another model's grid
    with pytest.raises(ValueError, match="beyond the static grid"):
        sample_static(static_lat_lon(south=46.0), grid)


# --- conversions


def test_run_total_becomes_amounts_per_interval() -> None:
    total = field([0, 1, 2, 3, 6], [0, 1, 3, 2.99, 9], "kg m-2")
    out = derive(SourceField(("p",), "run_total"), [total], lat_lon_grid())
    np.testing.assert_allclose(out.values[:, 0, 0], [np.nan, 1, 2, 0, 6.01], atol=1e-5)  # a small decrease is 0
    # a field starting after lead 0 (amount since the run started), with a lead that has no data
    total = field([3, 6, 9], [2, np.nan, 5], "mm")
    out = derive(SourceField(("p",), "run_total"), [total], lat_lon_grid())
    np.testing.assert_allclose(out.values[:, 0, 0], [2, np.nan, 3])


def test_rates_and_means_become_interval_values() -> None:
    rate = field([0, 1, 3], [np.nan, 1 / 3600, 2 / 3600], "kg m-2 s-1")
    out = derive(SourceField(("p",), "rate"), [rate], lat_lon_grid())
    np.testing.assert_allclose(out.values[:, 0, 0], [np.nan, 1, 4])
    assert out.units == "kg m-2"
    # GFS: means since the last lead divisible by 6; an interval's mean is what the window's mean gained
    reset = field([0, 1, 2, 6, 7, 120, 123, 126], [np.nan, 10, 20, 30, 50, 5, 40, 60], "percent")
    out = derive(SourceField(("c",), "reset_6h_mean"), [reset], lat_lon_grid())
    np.testing.assert_allclose(out.values[:, 0, 0], [np.nan, 10, 30, (30 * 6 - 20 * 2) / 4, 50, 5, 40, 80], equal_nan=True)
    integral = field([0, 6, 12], [0, 6 * 3600 * 100, 6 * 3600 * 300], "W s m-2")
    out = derive(SourceField(("r",), "run_total_mean"), [integral], lat_lon_grid())
    np.testing.assert_allclose(out.values[:, 0, 0], [np.nan, 100, 200])
    assert out.units == "W m-2"


def test_reset_6h_mean_value_before_120_is_windowed() -> None:
    # lead 120 closes a window, so its value covers 114-120 and isn't differenced with 119
    reset = field([118, 119, 120], [10, 20, 30], "percent")
    out = derive(SourceField(("c",), "reset_6h_mean"), [reset], lat_lon_grid())
    np.testing.assert_allclose(out.values[1:, 0, 0], [(20 * 5 - 10 * 4) / 1, (30 * 6 - 20 * 5) / 1])


def test_grid_winds_are_rotated_to_true_north() -> None:
    to_grid = Transformer.from_crs("EPSG:4326", LAMBERT, always_xy=True)
    x, y = to_grid.transform(-121.0, 46.5)
    grid = RegionGrid(CRS.from_proj4(LAMBERT).to_wkt(), Affine(3000, 0, x - 3000, 0, -3000, y + 3000), (2, 2))
    u, v = field([0], [3.0], "m s-1", (2, 2)), field([0], [4.0], "m s-1", (2, 2))
    east = derive(SourceField(("u", "v"), "grid_wind_u"), [u, v], grid).values[0]
    north = derive(SourceField(("u", "v"), "grid_wind_v"), [u, v], grid).values[0]
    np.testing.assert_allclose(np.hypot(east, north), 5.0, rtol=1e-5)  # rotation keeps the speed
    # NCEP's rotation for this grid: the cone constant times the longitude from the central meridian
    lon, _ = grid.lon_lat()
    alpha = np.sin(np.deg2rad(38.5)) * np.deg2rad(lon + 97.5)
    np.testing.assert_allclose(east, np.cos(alpha) * 3 + np.sin(alpha) * 4, atol=1e-3)
    np.testing.assert_allclose(north, -np.sin(alpha) * 3 + np.cos(alpha) * 4, atol=1e-3)
    # on a latitude/longitude grid, grid-relative is earth-relative
    np.testing.assert_allclose(derive(SourceField(("u", "v"), "grid_wind_u"), [u, v], lat_lon_grid((2, 2))).values, 3.0)


def test_speed_and_direction_become_components() -> None:
    speed = field([0, 1], [10, 10], "m/s")
    direction = field([0, 1], [270, 0], "°")  # from the west, then from the north
    u = derive(SourceField(("s", "d"), "speed_direction_u"), [speed, direction], lat_lon_grid()).values[:, 0, 0]
    v = derive(SourceField(("s", "d"), "speed_direction_v"), [speed, direction], lat_lon_grid()).values[:, 0, 0]
    np.testing.assert_allclose(u, [10, 0], atol=1e-5)
    np.testing.assert_allclose(v, [0, -10], atol=1e-5)


def test_freezing_level_conventions() -> None:
    height = field([0, 1, 2], [1500, 1500, np.nan], "m", (1, 4))
    height.values[:2, 0, 0] = np.nan  # HRRR: at or below ground
    height.values[:2, 0, 1] = -50  # RRFS: below sea level
    height.values[:2, 0, 2] = 800  # RRFS: under the terrain
    surface = field([0, 1, 2], [1000, np.nan, np.nan], "m", (1, 4))  # published at lead 0 only
    out = derive(SourceField(("h", "s"), "freezing_level"), [height, surface], lat_lon_grid()).values
    np.testing.assert_allclose(out[0, 0], [0, 0, 0, 1500])
    assert np.isnan(out[2]).all()  # a lead time with no data stays missing


def test_units_and_alignment() -> None:
    np.testing.assert_allclose(convert_units(np.array([101325.0]), "Pa", "hPa"), [1013.25])
    np.testing.assert_allclose(convert_units(np.array([283.15]), "K", "degC"), [10.0])
    with pytest.raises(ValueError, match="can't convert"):
        convert_units(np.array([1.0]), "m", "mm")
    values, dropped = align(field([0, 3, 4, 6], [1, 2, 3, 4], "m"), [0, 3, 6, 9])
    np.testing.assert_allclose(values[:, 0, 0], [1, 2, 4, np.nan])
    assert dropped == [4.0]
    values, _ = standardize(TEMPERATURE, SourceField(("t",)), [field([0, 3], [273.15, 274.15], "K")], lat_lon_grid(), [0, 3, 6])
    np.testing.assert_allclose(values[:, 0, 0], [0, 1, np.nan], atol=1e-5)


# --- writing a run


def test_writer_stores_standard_values_with_configured_chunks(tmp_path: Path) -> None:
    import zarr

    grid = lat_lon_grid((10, 20))
    leads = [0, 3, 6]
    values = np.linspace(-20, 30, 3 * 10 * 20, dtype=np.float32).reshape(3, 10, 20)
    values[1, 2, 3] = np.nan
    values[2, 0, 0] = -100  # below the variable's min: clamped
    writer = RunWriter(tmp_path / "run.zarr", grid, leads, ChunkShape(2, 4, 4), ShardShape(8, 8), RUN, {"model": "test"})
    writer.write(TEMPERATURE, values, "temperature_2m")
    writer.finish()

    array = zarr.open_group(tmp_path / "run.zarr", mode="r")["temperature_2m"]
    assert array.chunks == (2, 4, 4) and array.shards == (4, 8, 8)
    dataset = xr.open_zarr(tmp_path / "run.zarr", consolidated=True)
    read = dataset["temperature_2m"].values
    assert np.isnan(read[1, 2, 3])
    assert read[2, 0, 0] == pytest.approx(-80)
    values[2, 0, 0] = -80
    np.testing.assert_allclose(read[np.isfinite(values)], values[np.isfinite(values)], atol=0.025 + 1e-4)
    assert dataset.attrs["model"] == "test" and dataset.attrs["init_time"] == "2026-11-08T00:00:00Z"
    assert dataset["y"].values[0] == 48.75 and dataset["x"].values[0] == -125.75  # north up, cell centres
    assert dataset["temperature_2m"].attrs["units"] == "degC"
    assert "GeoTransform" in dataset["spatial_ref"].attrs


# --- driver and store


class FakeReader:
    def __init__(self, grid: RegionGrid, fields: dict[str, UpstreamField]) -> None:
        self.grid, self.fields, self.closed = grid, fields, False

    def read(self, name: str) -> UpstreamField:
        if name not in self.fields:
            raise MissingField(name)
        return self.fields[name]

    def close(self) -> None:
        self.closed = True


class FakeSource:
    """Runs at the given times are complete; every run has a temperature and a run-total precipitation."""

    def __init__(self, complete: set[datetime]) -> None:
        self.complete = complete
        self.opened: list[datetime] = []
        self.ended = 0

    def is_complete(self, model: ArchiveModel, initialized: datetime) -> bool:
        return initialized in self.complete

    def open_run(self, model: ArchiveModel, initialized: datetime) -> FakeReader:
        self.opened.append(initialized)
        shape = (10, 20)
        return FakeReader(lat_lon_grid(shape), {
            "temperature_2m": field([0, 3, 6], [initialized.hour, 1, 2], "degree_Celsius", shape),
            "total_precipitation_run_total_surface": field([0, 3, 6], [0, 1, 3], "kg m-2", shape),
            # published at lead time 3 only
            "geopotential_height_surface": field([0, 3, 6], [np.nan, 1234, np.nan], "m", shape),
        })

    def end_check(self) -> None:
        self.ended += 1


def download_run(storage, model_id: str, initialized: datetime, folder: Path) -> xr.Dataset:
    """An uploaded run, copied to a local folder and opened."""
    prefix = ArchiveStore.run_prefix(model_id, initialized)
    for key in storage.list_keys(prefix):
        local = folder / key.removeprefix(prefix)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(storage.objects[key][0])
    return xr.open_zarr(folder, consolidated=True)


def driver(storage, complete: set[datetime], archive_config: ArchiveConfig | None = None, now: datetime = NOW) -> ArchiveDriver:
    return ArchiveDriver(archive_config or config(model()), {"dynamical": FakeSource(complete)}, ArchiveStore(storage), now=lambda: now)


def test_driver_archives_newest_complete_runs_and_fills_the_backlog(memory_storage) -> None:
    runs = [RUN - timedelta(hours=6 * i) for i in range(6)]
    archive = driver(memory_storage, set(runs[1:]), config(model(), keep_runs=3))  # the newest is still running

    statuses = asyncio.run(archive.run())
    assert statuses[0].state == "ok" and statuses[0].runs == ["20261107T18", "20261107T12"]  # runs_per_check: 2
    index = archive.store.read_index("test")
    assert [run.init_time for run in index.runs] == [runs[1], runs[2]]
    assert index.runs[0].variables == ["temperature_2m", "precipitation"]

    statuses = asyncio.run(archive.run())  # the backlog: one more to keep 3
    assert statuses[0].runs == ["20261107T06"]
    assert asyncio.run(archive.run())[0].runs == []  # nothing new

    archive.sources["dynamical"].complete.add(runs[0])  # the newest run completes: the oldest kept is removed
    assert asyncio.run(archive.run())[0].runs == ["20261108T00"]
    index = archive.store.read_index("test")
    assert [run.init_time for run in index.runs] == runs[:3]
    assert archive.store.list_run_folders("test") == runs[:3]


def test_driver_uploads_the_root_metadata_last_and_round_trips(memory_storage, tmp_path: Path) -> None:
    archive = driver(memory_storage, {RUN})
    asyncio.run(archive.run())
    prefix = ArchiveStore.run_prefix("test", RUN)
    run_puts = [key for key in memory_storage.puts if key.startswith(prefix)]
    assert run_puts[-1] == prefix + "zarr.json"
    assert run_puts.index(prefix + "precipitation/zarr.json") > run_puts.index(prefix + "precipitation/c/0/0/0")
    assert memory_storage.puts[-1] == "archive/test/index.json"
    # the uploaded objects are a readable store
    dataset = download_run(memory_storage, "test", RUN, tmp_path)
    np.testing.assert_allclose(dataset["precipitation"].values[:, 0, 0], [np.nan, 1, 2], atol=0.006)
    np.testing.assert_allclose(dataset["temperature_2m"].values[:, 0, 0], [0, 1, 2], atol=0.03)


def test_driver_archives_again_when_the_config_changes(memory_storage) -> None:
    asyncio.run(driver(memory_storage, {RUN}).run())
    changed = model(chunks=ChunkShape(leads=4))
    archive = driver(memory_storage, {RUN}, config(changed))
    assert asyncio.run(archive.run())[0].runs == ["20261108T00"]
    assert archive.store.read_index("test").runs[0].fingerprint == archive.config.fingerprint(changed)
    assert asyncio.run(archive.run())[0].runs == []
    assert asyncio.run(archive.run(force=True))[0].runs == ["20261108T00"]


def test_driver_stores_the_terrain_or_says_why_not(memory_storage, tmp_path: Path) -> None:
    with_terrain = model(terrain=TerrainConfig(field="geopotential_height_surface"))
    status = asyncio.run(driver(memory_storage, {RUN}, config(with_terrain)).run())[0]
    assert status.state == "ok" and status.warnings == []
    terrain = download_run(memory_storage, "test", RUN, tmp_path / "with")["surface_height"]
    assert terrain.dims == ("y", "x") and terrain.attrs["units"] == "m"
    assert terrain.attrs["source"] == "geopotential_height_surface"
    np.testing.assert_array_equal(terrain.values, 1234)  # its first lead time with data

    # a model whose source lacks the field is still archived, without terrain, and the status says why
    lacking = model(terrain=TerrainConfig(field="orography"))
    status = asyncio.run(driver(memory_storage, {RUN}, config(lacking)).run())[0]
    assert status.state == "ok" and status.runs == ["20261108T00"]
    assert "no terrain" in status.warnings[0] and "orography" in status.warnings[0]
    dataset = download_run(memory_storage, "test", RUN, tmp_path / "without")
    assert "surface_height" not in dataset and "temperature_2m" in dataset


def test_driver_reports_missing_fields_and_removes_strays(memory_storage) -> None:
    variables = {"temperature_2m": SourceField(("temperature_2m",)), "wind_u_10m": SourceField(("u", "v"), "grid_wind_u")}
    stray = ArchiveStore.run_prefix("test", RUN - timedelta(days=3)) + "zarr.json"
    memory_storage.put_bytes(stray, b"{}", "application/json")
    archive = driver(memory_storage, {RUN}, config(model(variables=variables)))
    status = asyncio.run(archive.run())[0]
    assert status.state == "ok" and "no upstream data for wind_u_10m" in status.warnings[0]
    assert archive.store.read_index("test").runs[0].variables == ["temperature_2m"]
    assert stray not in memory_storage.objects


def test_driver_confines_a_failing_model(memory_storage) -> None:
    class Broken(FakeSource):
        def is_complete(self, model, initialized):
            raise RuntimeError("upstream down")

    good = model()
    bad = ArchiveModel(**{**{a: getattr(good, a) for a in ("region", "start_times", "leads", "variables")}, "id": "bad", "source": OpenMeteoRunsConfig("d")})
    archive = ArchiveDriver(config(good, bad), {"dynamical": FakeSource({RUN}), "openmeteo": Broken(set())}, ArchiveStore(memory_storage), now=lambda: NOW)
    statuses = {s.model: s for s in asyncio.run(archive.run())}
    assert statuses["test"].state == "ok" and statuses["bad"].state == "failed"
    assert "upstream down" in statuses["bad"].error
    assert archive.sources["openmeteo"].ended == 1


def test_loop_records_each_check(memory_storage) -> None:
    loop = ArchiveLoop(driver(memory_storage, {RUN}), now=lambda: NOW)
    status = asyncio.run(loop.run_check())
    assert status.state == "ok" and status.check == 1 and status.models[0].runs == ["20261108T00"]
    assert status.next_check_at == NOW + timedelta(minutes=15)
    stored = json.loads(memory_storage.objects[STATUS][0])
    assert stored["state"] == "ok" and stored["last_success_at"] is not None
    assert asyncio.run(loop.run_check()).check == 2


# --- dynamical.org source


def dynamical_dataset() -> xr.Dataset:
    """A dataset like GFS's: latitude north to south, two runs; the newer has data up to hour 3 only."""
    runs = [RUN - timedelta(hours=6), RUN]
    lats, lons = np.arange(52.0, 38.0, -0.5), np.arange(-132.0, -108.0, 0.5)
    leads = [0, 3, 6]
    shape = (len(runs), len(leads), len(lats), len(lons))
    temperature = np.broadcast_to(np.array([0.0, 10.0, 20.0], dtype=np.float32)[None, :, None, None], shape).copy()
    temperature[1, 2] = np.nan
    total = np.broadcast_to(np.array([0.0, 2.0, 5.0], dtype=np.float32)[None, :, None, None], shape).copy()
    dims = ("init_time", "lead_time", "latitude", "longitude")
    return xr.Dataset(
        {
            "temperature_2m": (dims, temperature, {"units": "degree_Celsius"}),
            "total_precipitation_run_total_surface": (dims, total, {"units": "kg m-2"}),
        },
        coords={
            "init_time": [np.datetime64(run.replace(tzinfo=None), "ns") for run in runs],
            "lead_time": [np.timedelta64(lead, "h") for lead in leads],
            "latitude": lats,
            "longitude": lons,
            "spatial_ref": ((), 0, {"crs_wkt": CRS.from_epsg(4326).to_wkt()}),
        },
    )


def test_dynamical_source_reads_runs_over_the_region() -> None:
    dataset = dynamical_dataset()
    opened = []
    source = DynamicalRunSource(open=lambda dataset_id: opened.append(dataset_id) or dataset)
    m = model()
    assert source.is_complete(m, RUN - timedelta(hours=6))
    assert not source.is_complete(m, RUN)  # hour 6 isn't published yet
    assert not source.is_complete(m, RUN + timedelta(hours=6))  # not started
    reader = source.open_run(m, RUN - timedelta(hours=6))
    temperature = reader.read("temperature_2m")
    assert temperature.values.shape[0] == 3 and temperature.units == "degree_Celsius"
    assert reader.grid.shape == temperature.values.shape[1:]
    assert reader.grid.transform.e < 0  # north up
    west, _, _, north = REGION
    assert reader.grid.transform.c <= west and reader.grid.transform.f >= north
    with pytest.raises(MissingField):
        reader.read("wind_u_10m")
    assert opened == ["test-dataset"]  # opened once per check
    source.end_check()
    source.is_complete(m, RUN)
    assert opened == ["test-dataset"] * 2


# --- Open-Meteo source


def write_static_om(path: Path, data: np.ndarray) -> None:
    from omfiles import OmFileWriter

    writer = OmFileWriter(str(path))
    writer.close(writer.write_array(data.astype(np.float32), chunks=[10, 10], scale_factor=10, name=""))


def write_om(path: Path, data: np.ndarray, crs: str, reference: datetime, leads: list[int], unit: str) -> None:
    from omfiles import OmFileWriter

    writer = OmFileWriter(str(path))
    children = [
        writer.write_scalar(crs, name="crs_wkt"),
        writer.write_scalar(unit, name="unit"),
        writer.write_scalar(np.int64(reference.timestamp()), name="forecast_reference_time"),
        writer.write_array(np.array([reference.timestamp() + 3600 * h for h in leads], dtype=np.int64), chunks=[len(leads)], name="time"),
    ]
    # like Open-Meteo, store with a scale factor: the default of 1 would round values to whole numbers
    root = writer.write_array(data.astype(np.float32), chunks=[1, 10, len(leads)], scale_factor=100, name="", children=children)
    writer.close(root)


# Open-Meteo's WKT, whose BBOX holds the first and last cells' latitude and longitude
OM_WKT = (
    'GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",ELLIPSOID["WGS 84",6378137,298.257223563]],CS[ellipsoidal,2],'
    'AXIS["latitude",north],AXIS["longitude",east],ANGLEUNIT["degree",0.0174532925199433],'
    'USAGE[SCOPE["grid"],BBOX[30.0,-140.0,60.0,-100.0]]]'
)


def openmeteo_transport(tmp_path: Path) -> httpx.MockTransport:
    """The bucket with one complete run of a 0.5° grid (rows south to north, like Open-Meteo's)."""
    lats, lons = np.arange(30.0, 60.01, 0.5), np.arange(-140.0, -99.99, 0.5)
    leads = [0, 3, 6]
    # temperature rises to the north so the orientation is checked: value = latitude + lead
    temperature = lats[:, None, None] + np.zeros((len(lats), len(lons), 1)) + np.array(leads)[None, None, :]
    write_om(tmp_path / "temperature_2m.om", temperature, OM_WKT, RUN, leads, "°C")
    files = {f"{run_url('test_domain', RUN)}/temperature_2m.om": (tmp_path / "temperature_2m.om").read_bytes()}
    files[f"{run_url('test_domain', RUN)}/meta.json"] = json.dumps({"variables": ["temperature_2m", "cloud_cover"]}).encode()
    # the domain's static surface height, on the same grid: 10 x latitude + longitude, with one sea cell
    heights = 10 * lats[:, None] + lons[None, :]
    heights[0, 0] = -999
    write_static_om(tmp_path / "HSURF.om", heights)
    files[f"{STATIC_URL.format(domain='test_domain')}/HSURF.om"] = (tmp_path / "HSURF.om").read_bytes()
    files[f"{STATIC_URL.format(domain='test_domain')}/meta.json"] = json.dumps({"crs_wkt": OM_WKT}).encode()

    def handle(request: httpx.Request) -> httpx.Response:
        data = files.get(str(request.url))
        return httpx.Response(200, content=data) if data is not None else httpx.Response(403)

    return httpx.MockTransport(handle)


def test_openmeteo_source_reads_a_run(tmp_path: Path) -> None:
    source = OpenMeteoRunSource(httpx.Client(transport=openmeteo_transport(tmp_path)))
    m = model(OpenMeteoRunsConfig("test_domain"))
    assert source.is_complete(m, RUN)
    assert not source.is_complete(m, RUN + timedelta(hours=6))
    reader = source.open_run(m, RUN)
    temperature = reader.read("temperature_2m")
    np.testing.assert_allclose(temperature.leads, [0, 3, 6])
    assert temperature.units == "°C"
    grid = reader.grid
    lon, lat = grid.lon_lat()
    # north up, with each cell's value matching its latitude
    assert lat[0, 0] > lat[-1, 0]
    np.testing.assert_allclose(temperature.values[1], lat + 3, atol=1e-4)
    assert lon.min() <= REGION[0] and lat.max() >= REGION[3]
    with pytest.raises(MissingField):
        reader.read("snow_depth")  # not in meta.json
    with pytest.raises(MissingField):
        reader.read("cloud_cover")  # listed, but no file
    folder = reader._folder
    reader.close()
    assert not folder.exists()


def test_openmeteo_run_archives_end_to_end(tmp_path: Path, memory_storage) -> None:
    source = OpenMeteoRunSource(httpx.Client(transport=openmeteo_transport(tmp_path)))
    m = model(OpenMeteoRunsConfig("test_domain"), variables={"temperature_2m": SourceField(("temperature_2m",))})
    archive = ArchiveDriver(config(m), {"openmeteo": source}, ArchiveStore(memory_storage), now=lambda: NOW)
    status = asyncio.run(archive.run())[0]
    assert status.state == "ok" and status.runs == ["20261108T00"]


def test_openmeteo_static_heights(tmp_path: Path) -> None:
    source = OpenMeteoRunSource(httpx.Client(transport=openmeteo_transport(tmp_path)))
    heights = source.surface_height("test_domain")
    assert heights.values.shape == (61, 81) and heights.ys[0] == 30.0 and heights.xs[0] == -140.0
    assert heights.values[0, 0] == 0  # the sea
    assert heights.values[1, 0] == pytest.approx(10 * 30.5 - 140)
    assert source.surface_height("test_domain") is heights  # read once per check
    source.end_check()
    assert source.surface_height("test_domain") is not heights
    with pytest.raises(MissingField):
        source.surface_height("other_domain")


def test_openmeteo_run_archives_with_static_terrain(tmp_path: Path, memory_storage) -> None:
    source = OpenMeteoRunSource(httpx.Client(transport=openmeteo_transport(tmp_path)))
    m = model(OpenMeteoRunsConfig("test_domain"), variables={"temperature_2m": SourceField(("temperature_2m",))},
              terrain=TerrainConfig(openmeteo="test_domain"))
    archive = ArchiveDriver(config(m), {"openmeteo": source}, ArchiveStore(memory_storage), heights=source,
                            now=lambda: NOW)
    status = asyncio.run(archive.run())[0]
    assert status.state == "ok" and status.warnings == []
    dataset = download_run(memory_storage, "test", RUN, tmp_path / "run")
    terrain = dataset["surface_height"]
    expected = 10 * dataset["y"].values[:, None] + dataset["x"].values[None, :]
    np.testing.assert_allclose(terrain.values, expected, atol=0.5)  # whole metres
    assert terrain.attrs["source"] == "Open-Meteo test_domain static HSURF"
