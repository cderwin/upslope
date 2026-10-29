import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from litestar.testing import TestClient

from upslope.clients.forecasts import ForecastStore
from upslope.clients.storage import StoredObject
from upslope.models.config import HourRange, LayerConfig, ModelConfig, ModelsConfig
from upslope.models.manifest import RunManifest, RunPointer
from upslope.server.app import create_app
from upslope.server.forecast import StatusCache

INITIALIZED = datetime(2026, 10, 21, 12, tzinfo=UTC)
LAYER = LayerConfig("surface-temp", "Surface temperature", "https://x/{init:%Y%m%d%H}/{hour:02d}.gif", HourRange(last=3), "washington")
CONFIG = ModelsConfig([
    ModelConfig("uwwrf_4km", "UW WRF 4km", [0, 12], [LAYER]),
    ModelConfig("uwwrf_12km", "UW WRF 12km", [0, 12], [LAYER]),
])
RUN_KEY = "forecasts/uwwrf_4km/2026/10/21/1200Z"


def export_run(storage, model: str = "uwwrf_4km", initialized: datetime = INITIALIZED, latest: bool = True) -> None:
    store = ForecastStore(storage)
    store.storage.put_bytes(f"{store.run_prefix(model, initialized)}/surface-temp/2026-10-21T1500.png", b"png-bytes", "image/png")
    manifest = RunManifest(model, "UW WRF 4km", initialized, 1, initialized, [])
    store.write_manifest(manifest)
    if latest:
        store.write_latest(RunPointer.to(manifest, initialized))


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets" / "app-abc123.js").write_text("console.log(1)")
    (tmp_path / "favicon.ico").write_bytes(b"icon")
    return tmp_path


@pytest.fixture
def storage(memory_storage):
    export_run(memory_storage, initialized=INITIALIZED - timedelta(hours=12))
    export_run(memory_storage)
    return memory_storage


@pytest.fixture
def client(storage, dist: Path):
    with TestClient(create_app(storage=storage, webapp_dist=dist, run_jobs=False, config=CONFIG)) as client:
        yield client


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


# --- forecast status


def test_status_lists_latest_and_retained_runs(client: TestClient) -> None:
    response = client.get("/fx/status")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.json() == {
        "models": [
            {
                "id": "uwwrf_4km",
                "name": "UW WRF 4km",
                "latest": {"initialized": "2026-10-21T12:00:00+00:00", "path": "2026/10/21/1200Z"},
                "runs": [
                    {"initialized": "2026-10-21T12:00:00+00:00", "path": "2026/10/21/1200Z"},
                    {"initialized": "2026-10-21T00:00:00+00:00", "path": "2026/10/21/0000Z"},
                ],
            },
            {"id": "uwwrf_12km", "name": "UW WRF 12km", "latest": None, "runs": []},
        ]
    }


def test_status_cached_across_requests(client: TestClient, storage) -> None:
    client.get("/fx/status")
    export_run(storage, model="uwwrf_12km")
    assert client.get("/fx/status").json()["models"][1]["latest"] is None


def test_status_cache_refreshes_when_stale(memory_storage) -> None:
    cache = StatusCache(ForecastStore(memory_storage), CONFIG, max_age=0)

    async def scenario() -> None:
        assert (await cache.get()).models[0].latest is None
        export_run(memory_storage)
        assert (await cache.get()).models[0].latest.initialized == INITIALIZED

    asyncio.run(scenario())


def test_status_cache_concurrent_stale_requests_share_one_lookup(memory_storage) -> None:
    store = ForecastStore(memory_storage)
    cache = StatusCache(store, CONFIG, max_age=60)
    reads = 0
    original = store.read_latest

    def counting_read_latest(model: str):
        nonlocal reads
        reads += 1
        return original(model)

    cache._store = type("Counting", (), {"read_latest": staticmethod(counting_read_latest), "list_runs": store.list_runs})()

    async def scenario() -> None:
        await asyncio.gather(*(cache.get() for _ in range(10)))

    asyncio.run(scenario())
    assert reads == len(CONFIG.models)


def test_status_cache_serves_cached_status_when_refresh_fails(memory_storage) -> None:
    export_run(memory_storage)
    cache = StatusCache(ForecastStore(memory_storage), CONFIG, max_age=0)

    async def scenario() -> None:
        await cache.refresh()

        def fail(model: str) -> None:
            raise RuntimeError("s3 down")

        cache._store = type("Broken", (), {"read_latest": staticmethod(fail), "list_runs": staticmethod(fail)})()
        assert (await cache.get()).models[0].latest.initialized == INITIALIZED

    asyncio.run(scenario())


# --- forecast data


def test_manifest_and_pointer_served_uncached(client: TestClient) -> None:
    response = client.get("/fx/data/uwwrf_4km/2026/10/21/1200Z/manifest.json")
    assert response.status_code == 200
    assert response.json()["initialized"] == "2026-10-21T12:00:00+00:00"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"]
    latest = client.get("/fx/data/uwwrf_4km/latest.json")
    assert latest.json()["path"] == "2026/10/21/1200Z"
    assert latest.headers["cache-control"] == "no-cache"


def test_frame_served_with_headers(client: TestClient) -> None:
    response = client.get("/fx/data/uwwrf_4km/2026/10/21/1200Z/surface-temp/2026-10-21T1500.png")
    assert response.status_code == 200
    assert response.content == b"png-bytes"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == "9"
    assert response.headers["cache-control"] == "public, max-age=300"


def test_matching_etag_returns_304(client: TestClient) -> None:
    path = "/fx/data/uwwrf_4km/2026/10/21/1200Z/surface-temp/2026-10-21T1500.png"
    etag = client.get(path).headers["etag"]
    response = client.get(path, headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""


def test_missing_object_404(client: TestClient) -> None:
    assert client.get("/fx/data/uwwrf_4km/2026/10/21/1200Z/surface-temp/2026-10-21T2100.png").status_code == 404
    assert client.get("/fx/data/uwwrf_12km/latest.json").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/fx/data/uwwrf_4km/../georegistration/draft.json",
        "/fx/data/uwwrf_4km/%2E%2E/%2E%2E/georegistration/draft.json",
        "/fx/data/uwwrf_4km/a\\b.png",
    ],
)
def test_path_traversal_rejected(client: TestClient, storage, path: str) -> None:
    storage.put_bytes("georegistration/draft.json", b"secret", "application/json")
    response = client.get(path)
    assert response.status_code == 404
    assert b"secret" not in response.content


def test_unknown_fx_path_is_404_not_html(client: TestClient) -> None:
    for path in ("/fx", "/fx/web/manifest.json", "/fx/data", "/fx/other/thing"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "<html>" not in response.text


def test_server_errors_are_logged(dist: Path, memory_storage) -> None:
    from unittest.mock import patch

    class BrokenStorage(type(memory_storage)):
        def get_object(self, key: str, if_none_match: str | None = None) -> StoredObject | None:
            raise RuntimeError("storage exploded")

    with patch("upslope.server.app.logger") as logger:
        with TestClient(create_app(storage=BrokenStorage(), webapp_dist=dist, run_jobs=False, config=CONFIG)) as client:
            assert client.get("/fx/data/uwwrf_4km/latest.json").status_code == 500
            assert client.get("/fx/data/a/b/missing.png").status_code == 500
            assert logger.error.call_count == 2
            assert all(isinstance(call.kwargs["exc_info"], RuntimeError) for call in logger.error.call_args_list)
            logger.error.reset_mock()
            assert client.get("/fx/nope").status_code == 404  # client errors aren't logged
            logger.error.assert_not_called()


# --- frontend


def test_index_served_at_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.text == "<html>app</html>"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["content-type"].startswith("text/html")


def test_client_routes_fall_back_to_index(client: TestClient) -> None:
    for path in ("/map", "/map/uwwrf_12km/surface-temp", "/annotation/12km/hour06.gif", "/missing.js"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.text == "<html>app</html>"


def test_static_files_served_with_cache_headers(client: TestClient) -> None:
    asset = client.get("/assets/app-abc123.js")
    assert asset.text == "console.log(1)"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert asset.headers["content-type"].split(";")[0] == "text/javascript"
    assert "attachment" not in asset.headers.get("content-disposition", "")

    favicon = client.get("/favicon.ico")
    assert favicon.content == b"icon"
    assert favicon.headers["cache-control"] == "public, max-age=300"


def test_frontend_cannot_escape_dist(client: TestClient, dist: Path) -> None:
    (dist.parent / "secret.txt").write_text("secret")
    response = client.get("/%2E%2E/secret.txt")
    assert "secret" not in response.text


def test_missing_dist_still_serves_api(tmp_path: Path, storage) -> None:
    with TestClient(create_app(storage=storage, webapp_dist=tmp_path / "nope", run_jobs=False, config=CONFIG)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/fx/data/uwwrf_4km/latest.json").status_code == 200
        assert client.get("/").status_code == 404


# --- hourly job


def test_seconds_until_next_hour() -> None:
    from upslope.server.jobs import seconds_until_next_hour

    assert seconds_until_next_hour(datetime(2026, 10, 21, 13, 59, 30)) == 30
    assert seconds_until_next_hour(datetime(2026, 10, 21, 14, 0, 0)) == 3600


def test_run_hourly_runs_immediately_when_asked() -> None:
    from unittest.mock import MagicMock, patch

    from upslope.server import jobs

    driver = MagicMock()
    driver.run.return_value = None  # already exported

    async def stop_on_sleep(seconds: float) -> None:
        raise asyncio.CancelledError

    with patch.object(jobs.asyncio, "sleep", stop_on_sleep), pytest.raises(asyncio.CancelledError):
        asyncio.run(jobs.run_hourly(driver, run_immediately=True))
    assert driver.run.call_count == 1

    driver.reset_mock()
    with patch.object(jobs.asyncio, "sleep", stop_on_sleep), pytest.raises(asyncio.CancelledError):
        asyncio.run(jobs.run_hourly(driver))
    assert driver.run.call_count == 0
