import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from litestar.testing import TestClient

from upslope.clients.storage import ModelRunMarker, StoredObject
from upslope.server.app import create_app
from upslope.server.forecast import LatestRun

RUN = ModelRunMarker(
    model="uwwrf",
    initialized=datetime(2026, 10, 21, 12, tzinfo=UTC),
    manifest="web_exports/20261021T130000/manifest.json",
)


class FakeStorage:
    """Stands in for StorageClient: objects live in a dict, and lookups are counted."""

    def __init__(self, marker: ModelRunMarker | None = RUN, objects: dict[str, bytes] | None = None) -> None:
        self.marker = marker
        self.objects = objects or {}
        self.latest_calls = 0
        self.requested: list[str] = []

    def latest_model_run(self, model: str) -> ModelRunMarker | None:
        self.latest_calls += 1
        return self.marker

    def get_object(self, key: str, if_none_match: str | None = None) -> StoredObject | None:
        self.requested.append(key)
        if key not in self.objects:
            return None
        etag = f'"{hash(self.objects[key])}"'
        if if_none_match == etag:
            return StoredObject(body=None, etag=etag, not_modified=True)
        content_type = "application/json" if key.endswith(".json") else "image/png"
        data = self.objects[key]
        return StoredObject(
            body=iter([data]),
            content_type=content_type,
            content_length=len(data),
            etag=etag,
            last_modified=datetime(2026, 10, 21, 13, 5, tzinfo=UTC),
        )


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets" / "app-abc123.js").write_text("console.log(1)")
    (tmp_path / "favicon.ico").write_bytes(b"icon")
    return tmp_path


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage(
        objects={
            "web_exports/20261021T130000/manifest.json": b'{"layers": []}',
            "web_exports/20261021T130000/uwwrf_12km/surface-temp/hour_06.png": b"png-bytes",
        }
    )


@pytest.fixture
def client(storage: FakeStorage, dist: Path):
    with TestClient(create_app(storage=storage, webapp_dist=dist, run_jobs=False)) as client:
        yield client


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


# --- forecast proxy


def test_manifest_proxied_from_latest_run(client: TestClient) -> None:
    response = client.get("/fx/web/manifest.json")
    assert response.status_code == 200
    assert response.json() == {"layers": []}
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"]


def test_frame_proxied_with_headers(client: TestClient, storage: FakeStorage) -> None:
    response = client.get("/fx/web/uwwrf_12km/surface-temp/hour_06.png")
    assert response.status_code == 200
    assert response.content == b"png-bytes"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-length"] == "9"
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["last-modified"] == "Wed, 21 Oct 2026 13:05:00 GMT"
    assert storage.requested[-1] == "web_exports/20261021T130000/uwwrf_12km/surface-temp/hour_06.png"


def test_matching_etag_returns_304(client: TestClient) -> None:
    etag = client.get("/fx/web/uwwrf_12km/surface-temp/hour_06.png").headers["etag"]
    response = client.get("/fx/web/uwwrf_12km/surface-temp/hour_06.png", headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""


def test_missing_frame_404(client: TestClient) -> None:
    assert client.get("/fx/web/uwwrf_12km/surface-temp/hour_99.png").status_code == 404


def test_no_exported_run_404(dist: Path) -> None:
    with TestClient(create_app(storage=FakeStorage(marker=None), webapp_dist=dist, run_jobs=False)) as client:
        assert client.get("/fx/web/manifest.json").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/fx/web/uwwrf_12km/surface-temp/../../other/secret.json",
        "/fx/web/uwwrf_12km/surface-temp/%2E%2E/x.png",
        "/fx/web/uwwrf_12km/surface-temp/a//b.png",
        "/fx/web/uwwrf_12km/surface-temp/a\\b.png",
    ],
)
def test_path_traversal_rejected(client: TestClient, storage: FakeStorage, path: str) -> None:
    storage.requested.clear()
    assert client.get(path).status_code == 404
    assert all(key.startswith("web_exports/20261021T130000/uwwrf_12km/") and ".." not in key for key in storage.requested)


def test_unknown_fx_path_is_404_not_html(client: TestClient) -> None:
    for path in ("/fx", "/fx/web", "/fx/web/only-one-segment", "/fx/other/thing"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "<html>" not in response.text


def test_server_errors_are_logged(dist: Path) -> None:
    from unittest.mock import patch

    class BrokenStorage(FakeStorage):
        def get_object(self, key: str, if_none_match: str | None = None) -> StoredObject | None:
            raise RuntimeError("storage exploded")

    with patch("upslope.server.app.logger") as logger:
        with TestClient(create_app(storage=BrokenStorage(), webapp_dist=dist, run_jobs=False)) as client:
            assert client.get("/fx/web/manifest.json").status_code == 500
            assert client.get("/fx/web/a/b/missing.png").status_code == 500
            assert logger.error.call_count == 2
            assert all(isinstance(call.kwargs["exc_info"], RuntimeError) for call in logger.error.call_args_list)
            logger.error.reset_mock()
            assert client.get("/fx/nope").status_code == 404  # client errors aren't logged
            logger.error.assert_not_called()


# --- latest run cache


def test_latest_run_cached_across_requests(client: TestClient, storage: FakeStorage) -> None:
    calls = storage.latest_calls  # startup lookup
    for _ in range(5):
        client.get("/fx/web/manifest.json")
    assert storage.latest_calls == calls


def test_latest_run_refreshes_when_stale() -> None:
    storage = FakeStorage()
    latest = LatestRun(storage, "uwwrf", max_age=0)

    async def scenario() -> None:
        assert await latest.get() == RUN
        newer = ModelRunMarker("uwwrf", datetime(2026, 10, 22, 0, tzinfo=UTC), "web_exports/20261022T010000/manifest.json")
        storage.marker = newer
        assert await latest.get() == newer

    asyncio.run(scenario())
    assert storage.latest_calls == 2


def test_latest_run_concurrent_stale_requests_share_one_lookup() -> None:
    storage = FakeStorage()
    latest = LatestRun(storage, "uwwrf", max_age=60)

    async def scenario() -> None:
        results = await asyncio.gather(*(latest.get() for _ in range(10)))
        assert all(result == RUN for result in results)

    asyncio.run(scenario())
    assert storage.latest_calls == 1


def test_latest_run_serves_cached_marker_when_refresh_fails() -> None:
    storage = FakeStorage()
    latest = LatestRun(storage, "uwwrf", max_age=0)

    async def scenario() -> None:
        await latest.refresh()

        def fail(model: str) -> None:
            raise RuntimeError("s3 down")

        storage.latest_model_run = fail
        assert await latest.get() == RUN

    asyncio.run(scenario())


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


def test_missing_dist_still_serves_api(tmp_path: Path, storage: FakeStorage) -> None:
    with TestClient(create_app(storage=storage, webapp_dist=tmp_path / "nope", run_jobs=False)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/fx/web/manifest.json").status_code == 200
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
