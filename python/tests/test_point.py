"""Point forecasts (`upslope.archive.point`): reading a point from runs and the DEM, adjusting to its elevation, and
putting every model on one hourly axis."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from affine import Affine
from pyproj import CRS

from upslope.archive.config import (
    ArchiveConfig,
    ArchiveDefaults,
    ArchiveModel,
    ArchiveVariable,
    ChunkShape,
    DynamicalRunsConfig,
    ShardShape,
    SourceField,
)
from upslope.archive.dem import DemBuilder, DemGrid, Tile
from upslope.archive.grid import RegionGrid
from upslope.archive.point import (
    Elevation,
    OpenRun,
    RunPoint,
    adjust_to_elevation,
    build_point_forecast,
    dew_point,
    hourly,
    read_elevation,
    read_run_point,
    relative_humidity,
)
from upslope.archive.writer import RunWriter
from upslope.models.config import HourRange, ValueQuantization

RUN = datetime(2026, 11, 8, 0, tzinfo=UTC)
REGION = (-126.0, 44.0, -116.0, 49.0)

VARIABLES = [
    ArchiveVariable("temperature_2m", "2 metre temperature", "degC", ValueQuantization(0.05, -80, 60)),
    ArchiveVariable("dew_point_2m", "2 metre dew point", "degC", ValueQuantization(0.05, -90, 50)),
    ArchiveVariable("relative_humidity_2m", "2 metre relative humidity", "%", ValueQuantization(0.1, 0, 100)),
    ArchiveVariable("wind_u_10m", "eastward wind", "m s-1", ValueQuantization(0.05, -150, 150)),
    ArchiveVariable("wind_v_10m", "northward wind", "m s-1", ValueQuantization(0.05, -150, 150)),
    ArchiveVariable("precipitation", "precipitation", "mm", ValueQuantization(0.01, 0, 300), kind="sum"),
    ArchiveVariable("shortwave_radiation", "shortwave", "W m-2", ValueQuantization(0.1, 0, 1500), kind="mean"),
]


def model(
    model_id: str = "fine", leads: list[HourRange] | None = None, variables: list[str] | None = None
) -> ArchiveModel:
    names = variables or [v.id for v in VARIABLES]
    return ArchiveModel(
        id=model_id,
        name=model_id.upper(),
        source=DynamicalRunsConfig("test"),
        region=REGION,
        start_times=[0, 6, 12, 18],
        leads=leads or [HourRange(last=6, step=3)],
        variables={name: SourceField((name,)) for name in names},
        attribution="test data",
    )


def config(*models: ArchiveModel) -> ArchiveConfig:
    return ArchiveConfig(VARIABLES, list(models or [model()]), ArchiveDefaults(ChunkShape(2, 4, 4), ShardShape(8, 8)))


def grid() -> RegionGrid:
    return RegionGrid(CRS.from_epsg(4326).to_wkt(), Affine(0.5, 0, -126.0, 0, -0.5, 49.0), (10, 20))


def point(m: ArchiveModel, init: datetime, values: dict[str, list[float]], terrain: float | None = 1000.0) -> RunPoint:
    leads = np.array(m.lead_hours)
    return RunPoint(
        m, init, leads, -121.75, 46.75, terrain, {k: np.array(v, dtype=np.float64) for k, v in values.items()}
    )


# --- grids


def test_grid_finds_a_points_cell() -> None:
    g = grid()
    assert g.cell(-125.9, 48.9) == (0, 0)
    assert g.cell(-121.6, 46.6) == (4, 8)
    assert g.cell_centre(4, 8) == pytest.approx((-121.75, 46.75))
    assert g.cell(-127.0, 48.0) is None and g.cell(-120.0, 43.9) is None
    lambert = RegionGrid(
        "+proj=lcc +lat_1=38.5 +lat_2=38.5 +lat_0=38.5 +lon_0=-97.5 +R=6371229 +units=m +no_defs",
        Affine(3000, 0, -2423020.0, 0, -3000, 1588193.0),
        (384, 434),
    )
    row, col = lambert.cell(-121.7604, 46.8529)
    lon, lat = lambert.cell_centre(row, col)
    assert abs(lon + 121.7604) < 0.03 and abs(lat - 46.8529) < 0.03  # within a 3 km cell


# --- reading


def test_reads_a_point_from_a_stored_run(tmp_path: Path) -> None:
    m = model(variables=["temperature_2m", "precipitation"])
    writer = RunWriter(
        tmp_path / "run.zarr", grid(), m.lead_hours, ChunkShape(2, 4, 4), ShardShape(8, 8), RUN, {"model": m.id}
    )
    rows = np.arange(10, dtype=np.float32)[None, :, None]
    writer.write(
        VARIABLES[0],
        np.broadcast_to(rows + np.array([0, 1, 2], dtype=np.float32)[:, None, None], (3, 10, 20)).copy(),
        "t",
    )
    precipitation = np.full((3, 10, 20), 0.5, dtype=np.float32)
    precipitation[0] = np.nan
    writer.write(VARIABLES[5], precipitation, "p")
    writer.write_terrain(np.full((10, 20), 1234.0, dtype=np.float32), "test")
    writer.finish()

    run = OpenRun.open(m, tmp_path / "run.zarr")
    assert run.init_time == RUN and list(run.leads) == [0, 3, 6]
    got = read_run_point(run, ["temperature_2m", "precipitation", "snow_depth"], 46.6, -121.6)
    assert got is not None and got.terrain == 1234
    np.testing.assert_allclose(got.values["temperature_2m"], [4, 5, 6], atol=0.03)  # row 4
    np.testing.assert_allclose(got.values["precipitation"], [np.nan, 0.5, 0.5])
    assert "snow_depth" not in got.values
    assert (got.cell_lon, got.cell_lat) == pytest.approx((-121.75, 46.75))
    assert read_run_point(run, ["temperature_2m"], 40.0, -121.6) is None


def test_reads_a_points_elevation_from_the_dem(tmp_path: Path) -> None:
    n = 12
    values = np.arange(n * n, dtype=np.float32).reshape(n, n) * 10
    tile = Tile(np.pad(values, 6), Affine(1 / n, 0, -122 - 6 / n, 0, -1 / n, 47 + 6 / n))
    DemBuilder(DemGrid((-122.0, 46.0, -121.0, 47.0), n), fetch=lambda url: tile if "USGS" in url else None).build(
        tmp_path / "dem.zarr"
    )
    import zarr

    dem = zarr.open_group(tmp_path / "dem.zarr", mode="r")
    assert read_elevation(dem, 46.99, -121.99) == Elevation(0, "usgs_3dep")
    assert read_elevation(dem, 46.99, -121.0 - 1 / 24) == Elevation(110, "usgs_3dep")  # row 0, last column
    assert read_elevation(dem, 45.5, -121.5) is None


# --- elevation


def test_adjusts_to_the_points_elevation_with_the_standard_lapse_rate() -> None:
    m = model()
    p = point(
        m, RUN, {"temperature_2m": [10, 10, 10], "dew_point_2m": [5, 9.5, 5], "relative_humidity_2m": [70, 97, 70]}
    )
    values, adjustment = adjust_to_elevation(p, Elevation(2000, "usgs_3dep"))  # 1000 m above the model's terrain
    assert adjustment.applied and adjustment.method == "standard_lapse_rate" and adjustment.delta_m == 1000
    assert adjustment.variables == ["temperature_2m", "dew_point_2m", "relative_humidity_2m"]
    np.testing.assert_allclose(values["temperature_2m"], 3.5)
    np.testing.assert_allclose(values["dew_point_2m"], [3.2, 3.5, 3.2])  # never above the temperature
    np.testing.assert_allclose(
        values["relative_humidity_2m"], relative_humidity(values["temperature_2m"], values["dew_point_2m"])
    )
    assert values["relative_humidity_2m"][1] == pytest.approx(100)
    np.testing.assert_allclose(p.values["temperature_2m"], 10)  # the read values are left alone

    # a model with humidity but no dew point still has its humidity adjusted, through the dew point
    humid = point(m, RUN, {"temperature_2m": [10, 10, 10], "relative_humidity_2m": [50, 50, 50]})
    values, adjustment = adjust_to_elevation(humid, Elevation(500, "request"))
    assert adjustment.variables == ["temperature_2m", "relative_humidity_2m"] and "dew_point_2m" not in values
    expected = relative_humidity(np.array(13.25), dew_point(np.array(10.0), np.array(50.0)) + 0.0018 * 500)
    np.testing.assert_allclose(values["relative_humidity_2m"], expected)

    for p, elevation, reason in [
        (point(m, RUN, {"temperature_2m": [1, 2, 3]}, terrain=None), Elevation(10, "request"), "no model terrain"),
        (point(m, RUN, {"temperature_2m": [1, 2, 3]}), None, "point elevation unknown"),
    ]:
        values, adjustment = adjust_to_elevation(p, elevation)
        assert not adjustment.applied and adjustment.reason == reason and values is p.values


def test_dew_point_and_humidity_round_trip() -> None:
    t, rh = np.array([-10.0, 0.0, 20.0]), np.array([40.0, 80.0, 100.0])
    np.testing.assert_allclose(relative_humidity(t, dew_point(t, rh)), rh, rtol=1e-6)


# --- the hourly axis


def test_hourly_places_or_interpolates_by_kind() -> None:
    leads = np.array([0, 3, 6])
    nan = np.nan
    np.testing.assert_allclose(
        hourly(leads, np.array([nan, 0.3, 0.6]), "sum", False), [nan, nan, nan, 0.3, nan, nan, 0.6]
    )
    np.testing.assert_allclose(
        hourly(leads, np.array([nan, 0.3, 0.6]), "sum", True), [nan, 0.1, 0.1, 0.1, 0.2, 0.2, 0.2]
    )
    np.testing.assert_allclose(hourly(leads, np.array([nan, 30, 60]), "mean", True), [nan, 30, 30, 30, 60, 60, 60])
    np.testing.assert_allclose(hourly(leads, np.array([0, 3, 9]), "instant", True), [0, 1, 2, 3, 5, 7, 9])
    np.testing.assert_allclose(hourly(leads, np.array([0, 3, 9]), "instant", False), [0, nan, nan, 3, nan, nan, 9])
    # a sum after a lead time without data covers both steps (e.g. NBM's 6-hourly precipitation on a 3-hourly axis)
    np.testing.assert_allclose(
        hourly(np.array([0, 3, 6, 9]), np.array([nan, 0.3, nan, 0.9]), "sum", True),
        [nan, 0.1, 0.1, 0.1, 0.15, 0.15, 0.15, 0.15, 0.15, 0.15],
    )
    assert np.isnan(hourly(leads, np.full(3, nan), "instant", True)).all()


def test_point_forecast_puts_every_model_on_one_hourly_axis() -> None:
    fine = model("fine", [HourRange(last=6, step=1)])
    coarse = model(
        "coarse", [HourRange(last=12, step=6)], ["temperature_2m", "wind_u_10m", "wind_v_10m", "precipitation"]
    )
    nan = np.nan
    fine_point = point(
        fine,
        RUN,
        {
            "temperature_2m": [0, 1, 2, 3, 4, 5, 6],
            "precipitation": [nan, 1, 1, 1, 1, 1, 1],
            "wind_u_10m": [0] * 7,
            "wind_v_10m": [-2] * 7,  # from the north
        },
        terrain=None,
    )
    # six hours older, coarser, and 500 m below the point
    coarse_point = point(
        coarse,
        RUN - timedelta(hours=6),
        {
            "temperature_2m": [10, 16, 22],
            "precipitation": [nan, 6, 12],
            "wind_u_10m": [3, 3, 3],
            "wind_v_10m": [0, 0, 0],
        },
        terrain=1000,
    )
    forecast = build_point_forecast(
        config(fine, coarse),
        46.75,
        -121.75,
        [fine_point, coarse_point],
        Elevation(1500, "usgs_3dep"),
        missing={"other": "point outside region"},
    )

    start = int((RUN - timedelta(hours=6)).timestamp())
    assert forecast.times == [start + 3600 * h for h in range(13)]
    assert forecast.accumulation_start == int(RUN.timestamp()) and not forecast.interpolated
    assert forecast.missing == {"other": "point outside region"}
    assert list(forecast.models) == ["fine", "coarse"]
    assert forecast.models["fine"].adjustment.reason == "no model terrain"
    assert forecast.models["coarse"].adjustment.delta_m == 500 and forecast.models["coarse"].steps[0].step == 6
    assert forecast.point.elevation_m == 1500 and forecast.point.elevation_source == "usgs_3dep"

    temperature = forecast.variables["temperature_2m"]
    assert temperature.units == "degC" and temperature.kind == "instant"
    assert temperature.series["fine"] == [None] * 6 + [0, 1, 2, 3, 4, 5, 6]
    assert temperature.series["coarse"] == [6.75, *[None] * 5, 12.75, *[None] * 5, 18.75]  # 3.25 K colder
    assert forecast.variables["precipitation"].series["coarse"] == [None] * 6 + [6, *[None] * 5, 12]
    assert "relative_humidity_2m" not in forecast.variables  # no model has it

    assert forecast.variables["wind_speed_10m"].series["coarse"][0] == 3
    assert forecast.variables["wind_direction_10m"].series["coarse"][0] == 270  # from the west
    assert forecast.variables["wind_direction_10m"].series["fine"][6] == 0  # from the north
    # running totals from the newest run's start: the coarse model's 6-hour sums are split evenly to count
    totals = forecast.variables["precipitation_accumulated"]
    assert totals.kind == "total"
    assert totals.series["fine"] == [None] * 7 + [1, 2, 3, 4, 5, 6]
    assert totals.series["coarse"] == [None] * 6 + [0, *[None] * 5, 12]  # the sum ending at the start isn't counted

    interpolated = build_point_forecast(
        config(fine, coarse), 46.75, -121.75, [fine_point, coarse_point], None, interpolate=True
    )
    assert interpolated.interpolated and interpolated.point.elevation_m is None
    assert interpolated.variables["temperature_2m"].series["coarse"][:7] == [10, 11, 12, 13, 14, 15, 16]
    assert interpolated.variables["precipitation"].series["coarse"][1:7] == [1] * 6
    assert interpolated.variables["precipitation_accumulated"].series["coarse"][6:] == [0, 2, 4, 6, 8, 10, 12]


def test_point_forecast_reports_models_without_data() -> None:
    m = model()
    empty = point(m, RUN, {"temperature_2m": [np.nan] * 3})
    forecast = build_point_forecast(config(m), 46.75, -121.75, [empty], None)
    assert forecast.times == [] and forecast.models == {} and forecast.missing == {"fine": "no data at this point"}
    assert forecast.accumulation_start is None
