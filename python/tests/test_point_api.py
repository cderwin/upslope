"""The archive cache (`upslope.archive.cache`) and the point forecast API (`GET /fx/point`), against in-memory
storage."""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from affine import Affine
from litestar.testing import TestClient
from upslope.archive.cache import PARTIAL, ArchiveCache
from upslope.archive.config import ChunkShape, ShardShape
from upslope.archive.dem import DemBuilder, DemGrid, Tile
from upslope.archive.store import ArchivedRun, ArchiveStore, DemIndex, run_key
from upslope.archive.writer import RunWriter
from upslope.models.config import HourRange
from upslope.server.app import create_app

from tests.conftest import MemoryStorage
from tests.test_point import VARIABLES, config, grid, model
from tests.test_server import CONFIG

RUN = datetime(2026, 11, 8, 0, tzinfo=UTC)
LAT, LON = 46.6, -121.6  # inside the test grid and DEM
FINE = model("fine", [HourRange(last=6, step=3)], ["temperature_2m", "precipitation"])
OTHER = model("other", [HourRange(last=6, step=3)], ["temperature_2m"])


class CountingStorage(MemoryStorage):
    """Counts the listings a download starts with."""

    def __init__(self) -> None:
        super().__init__()
        self.listed: list[str] = []

    def list_keys(self, prefix: str) -> list[str]:
        self.listed.append(prefix)
        return super().list_keys(prefix)


def publish_run(storage, tmp_path: Path, initialized: datetime, temperature: float = 10.0) -> None:
    """Archive a run of FINE, as the archiver does: upload the store, then list it in the index."""
    folder = tmp_path / "runs" / run_key(initialized)
    writer = RunWriter(
        folder, grid(), FINE.lead_hours, ChunkShape(2, 4, 4), ShardShape(8, 8), initialized, {"model": "fine"}
    )
    writer.write(VARIABLES[0], np.full((3, 10, 20), temperature, dtype=np.float32), "t")
    precipitation = np.broadcast_to(np.array([np.nan, 0.3, 0.6], dtype=np.float32)[:, None, None], (3, 10, 20)).copy()
    writer.write(VARIABLES[5], precipitation, "p")
    writer.write_terrain(np.full((10, 20), 1000.0, dtype=np.float32), "test")
    writer.finish()
    store = ArchiveStore(storage)
    store.upload_run("fine", initialized, folder)
    index = store.read_index("fine")
    index.runs = [ArchivedRun(initialized, "f", initialized, ["temperature_2m", "precipitation"]), *index.runs]
    store.write_index(index)


def publish_dem(storage, tmp_path: Path) -> None:
    """A 12-cells-per-degree DEM over -122..-121, 46..47, all at 2000 m."""
    n = 12
    tile = Tile(
        np.full((n + 12, n + 12), 2000.0, dtype=np.float32), Affine(1 / n, 0, -122 - 6 / n, 0, -1 / n, 47 + 6 / n)
    )
    DemBuilder(DemGrid((-122.0, 46.0, -121.0, 47.0), n), fetch=lambda url: tile if "USGS" in url else None).build(
        tmp_path / "dem.zarr"
    )
    ArchiveStore(storage).publish_dem(tmp_path / "dem.zarr", DemIndex("20261125T1200.zarr", RUN, [-122, 46, -121, 47]))


def cache(storage, tmp_path: Path, **kwargs) -> ArchiveCache:
    return ArchiveCache(storage, config(FINE, OTHER), tmp_path / "cache", **kwargs)


# --- the cache


def test_cache_downloads_the_newest_run_and_answers_points(tmp_path: Path) -> None:
    storage = CountingStorage()
    publish_run(storage, tmp_path, RUN)
    publish_dem(storage, tmp_path)
    archive = cache(storage, tmp_path)

    forecast = asyncio.run(_refresh_then_point(archive))
    assert archive.ready
    assert (tmp_path / "cache" / "fine" / run_key(RUN) / "zarr.json").is_file()
    assert (tmp_path / "cache" / "dem" / "20261125T1200.zarr" / "elevation" / "zarr.json").is_file()
    assert forecast.missing == {"other": "no archived run"}
    assert forecast.point.elevation_m == 2000 and forecast.point.elevation_source == "usgs_3dep"
    fine = forecast.models["fine"]
    assert fine.init_time == RUN and fine.adjustment.applied and fine.adjustment.delta_m == 1000
    assert forecast.variables["temperature_2m"].series["fine"] == [
        3.5,
        None,
        None,
        3.5,
        None,
        None,
        3.5,
    ]  # 6.5 K colder
    assert forecast.variables["precipitation"].series["fine"] == [None, None, None, 0.3, None, None, 0.6]

    # nothing new: no downloads
    listed = len(storage.listed)
    asyncio.run(archive.refresh())
    assert len(storage.listed) == listed


async def _refresh_then_point(archive: ArchiveCache, **kwargs):
    await archive.refresh()
    return await archive.point(LAT, LON, **kwargs)


def test_cache_swaps_in_new_runs_and_keeps_the_one_before(tmp_path: Path) -> None:
    storage = MemoryStorage()
    archive = cache(storage, tmp_path)
    runs = [RUN + timedelta(hours=6 * i) for i in range(3)]

    async def scenario() -> list[float | None]:
        temperatures = []
        for i, initialized in enumerate(runs):
            publish_run(storage, tmp_path, initialized, temperature=float(i))
            await archive.refresh()
            forecast = await archive.point(LAT, LON, elevation=1000)  # the model's own height: no change
            temperatures.append(forecast.variables["temperature_2m"].series["fine"][0])
        return temperatures

    assert asyncio.run(scenario()) == [0, 1, 2]
    cached = sorted(p.name for p in (tmp_path / "cache" / "fine").iterdir())
    assert cached == [run_key(runs[1]), run_key(runs[2])]  # the oldest is gone; the one before is kept


def test_cache_reuses_complete_downloads_and_drops_partial_ones(tmp_path: Path) -> None:
    storage = CountingStorage()
    publish_run(storage, tmp_path, RUN)
    asyncio.run(cache(storage, tmp_path).refresh())
    stale = tmp_path / "cache" / "fine" / (run_key(RUN - timedelta(hours=6)) + PARTIAL)
    stale.mkdir()

    storage.listed.clear()
    restarted = cache(storage, tmp_path)  # a server restart with the disk intact
    forecast = asyncio.run(_refresh_then_point(restarted, elevation=1000))
    assert storage.listed == []  # the run on disk is complete, so it's used as it is
    assert not stale.exists()
    assert forecast.variables["temperature_2m"].series["fine"][0] == 10


def test_cache_reports_why_models_are_missing(tmp_path: Path) -> None:
    storage = MemoryStorage()
    publish_run(storage, tmp_path, RUN)
    loading = cache(storage, tmp_path, ready_timeout=0.0)
    forecast = asyncio.run(loading.point(LAT, LON))  # nothing read yet, and it doesn't wait
    assert forecast.missing == {"fine": "loading", "other": "loading"} and forecast.times == []

    ready = cache(storage, tmp_path)
    forecast = asyncio.run(_refresh_then_point(ready))
    assert forecast.point.elevation_m is None  # no DEM published
    assert forecast.models["fine"].adjustment.reason == "point elevation unknown"
    outside = asyncio.run(ready.point(30.0, -100.0))
    assert outside.missing == {"fine": "outside the model's region", "other": "no archived run"}
    only = asyncio.run(ready.point(LAT, LON, models=["other"]))
    assert list(only.missing) == ["other"] and only.models == {}


def test_cache_survives_a_failing_download(tmp_path: Path) -> None:
    storage = MemoryStorage()
    publish_run(storage, tmp_path, RUN)
    for key in storage.list_keys(ArchiveStore.run_prefix("fine", RUN)):
        storage.objects.pop(key)  # listed in the index, but its objects are gone
    archive = cache(storage, tmp_path)
    forecast = asyncio.run(_refresh_then_point(archive))
    assert archive.ready and forecast.missing["fine"] == "no archived run"
    assert not any((tmp_path / "cache" / "fine").glob(f"*{PARTIAL}"))
    publish_run(storage, tmp_path, RUN + timedelta(hours=6))  # the next run arrives intact
    forecast = asyncio.run(_refresh_then_point(archive))
    assert "fine" in forecast.models


# --- the API


@pytest.fixture
def archived(memory_storage, tmp_path: Path):
    publish_run(memory_storage, tmp_path, RUN)
    publish_dem(memory_storage, tmp_path)
    return memory_storage


@pytest.fixture
def client(archived, tmp_path: Path):
    app = create_app(
        storage=archived,
        webapp_dist=tmp_path / "no-webapp",
        config=CONFIG,
        archive_cache_dir=tmp_path / "cache",
        archive_config=config(FINE, OTHER),
    )
    with TestClient(app) as client:
        yield client


def test_point_api_serves_every_model(client: TestClient) -> None:
    response = client.get("/fx/point", params={"lat": LAT, "lon": LON})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-cache"
    body = response.json()
    assert body["point"] == {"lat": LAT, "lon": LON, "elevation_m": 2000, "elevation_source": "usgs_3dep"}
    assert body["times"][0] == int(RUN.timestamp()) and len(body["times"]) == 7
    assert body["models"]["fine"]["init_time"] == "2026-11-08T00:00:00+00:00"
    assert body["models"]["fine"]["adjustment"]["applied"] is True
    assert body["missing"] == {"other": "no archived run"}
    assert body["variables"]["temperature_2m"]["units"] == "degC"
    assert body["variables"]["temperature_2m"]["series"]["fine"][0] == 3.5
    assert set(body["variables"]) == {"temperature_2m", "precipitation", "precipitation_accumulated"}


def test_point_api_options(client: TestClient) -> None:
    body = client.get(
        "/fx/point",
        params={"lat": LAT, "lon": LON, "interpolate": "true", "elevation": 1000, "variables": "precipitation"},
    ).json()
    assert body["interpolated"] is True and body["point"]["elevation_source"] == "request"
    assert list(body["variables"]) == ["precipitation"]
    assert body["variables"]["precipitation"]["series"]["fine"][1:] == [0.1, 0.1, 0.1, 0.2, 0.2, 0.2]
    body = client.get("/fx/point", params={"lat": LAT, "lon": LON, "models": "other"}).json()
    assert body["models"] == {} and list(body["missing"]) == ["other"]


def test_point_api_validates_its_parameters(client: TestClient) -> None:
    for params in ({"lat": 91, "lon": 0}, {"lat": 0}, {"lat": "x", "lon": 0}, {"lat": 0, "lon": 0, "elevation": 10000}):
        assert client.get("/fx/point", params=params).status_code == 400, params
    response = client.get("/fx/point", params={"lat": LAT, "lon": LON, "models": "fine,nope"})
    assert response.status_code == 400 and "nope" in response.text


def test_point_api_is_gzipped(client: TestClient) -> None:
    response = client.get("/fx/point", params={"lat": LAT, "lon": LON}, headers={"Accept-Encoding": "gzip"})
    assert response.headers.get("content-encoding") == "gzip"
    assert response.json()["models"]["fine"]  # decoded by the client
    assert SERVER_TIMING.fullmatch(response.headers["server-timing"])


SERVER_TIMING = re.compile(r"read;dur=(\d+\.\d), build;dur=(\d+\.\d), total;dur=(\d+\.\d)")


def test_point_api_reports_server_timing(client: TestClient) -> None:
    response = client.get("/fx/point", params={"lat": LAT, "lon": LON})
    match = SERVER_TIMING.fullmatch(response.headers["server-timing"])
    assert match, response.headers["server-timing"]
    read, build, total = (float(d) for d in match.groups())
    assert read + build <= total + 0.2  # each rounded to 0.1 ms
    assert "dur=" not in response.text  # not in the body


def test_point_api_is_disabled_without_a_cache_folder(memory_storage, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("ARCHIVE_CACHE_DIR", raising=False)
    with TestClient(create_app(storage=memory_storage, webapp_dist=tmp_path, config=CONFIG)) as client:
        assert client.get("/fx/point", params={"lat": LAT, "lon": LON}).status_code == 503
