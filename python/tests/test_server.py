import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from litestar.testing import TestClient

from upslope.clients.forecasts import EXPORT_STATUS, INDEX, ForecastStore
from upslope.clients.storage import StoredObject
from upslope.models.config import HourRange, LayerConfig, ModelConfig, ModelsConfig
from upslope.models.manifest import (
    ExportStatus, ForecastIndex, InProgress, LayerProgress, ModelCheckStatus, ModelIndex, RunManifest, RunPointer,
)
from upslope.server.app import create_app
from upslope.server.forecast import STALL_AFTER, RunRef, StatusCache, in_progress_status

INITIALIZED = datetime(2026, 10, 21, 12, tzinfo=UTC)
LAYER = LayerConfig("surface-temp", "Surface temperature", "https://x/{init:%Y%m%d%H}/{hour:02d}.gif", HourRange(last=3), "washington")
CONFIG = ModelsConfig([
    ModelConfig("uwwrf_4km", "UW WRF 4km", [0, 12], [LAYER]),
    ModelConfig("uwwrf_12km", "UW WRF 12km", [0, 12], [LAYER]),
])
RUN_KEY = "forecasts/uwwrf_4km/2026/10/21/1200Z"
# PUBLIC_DATA_URL, as conftest sets it
DATA_URL = "https://data.example/forecasts"


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
    with TestClient(create_app(storage=storage, webapp_dist=dist, config=CONFIG)) as client:
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
                "in_progress": None,
                "runs": [
                    {"initialized": "2026-10-21T12:00:00+00:00", "path": "2026/10/21/1200Z"},
                    {"initialized": "2026-10-21T00:00:00+00:00", "path": "2026/10/21/0000Z"},
                ],
            },
            {"id": "uwwrf_12km", "name": "UW WRF 12km", "latest": None, "in_progress": None, "runs": []},
        ],
        "data_url": DATA_URL,
        "export": None,
        "export_overdue": False,
    }


def test_status_reports_the_exporters_health(memory_storage) -> None:
    """The exporter (another process) records each check; the status shows failures and when it's overdue."""
    store = ForecastStore(memory_storage)
    finished = INITIALIZED + timedelta(minutes=3)
    store.write_export_status(
        ExportStatus(
            "failed", 7, INITIALIZED, overdue_after=INITIALIZED + timedelta(minutes=25), finished_at=finished,
            next_check_at=INITIALIZED + timedelta(minutes=15), consecutive_failures=2,
            models=[ModelCheckStatus("uwwrf_4km", "failed", error="HTTP 503"), ModelCheckStatus("uwwrf_12km", "ok", frames=3)],
        )
    )

    now = INITIALIZED + timedelta(minutes=10)
    cache = StatusCache(store, CONFIG, DATA_URL, now=lambda: now)
    on_time = asyncio.run(cache.get())
    assert not on_time.export_overdue
    assert (on_time.export.state, on_time.export.check, on_time.export.consecutive_failures) == ("failed", 7, 2)
    assert on_time.export.models[0].error == "HTTP 503"
    # worked out per request, without waiting for a refresh
    now = INITIALIZED + timedelta(minutes=30)
    assert asyncio.run(cache.get()).export_overdue


def test_status_cached_across_requests(client: TestClient, storage) -> None:
    client.get("/fx/status")
    export_run(storage, model="uwwrf_12km")
    assert client.get("/fx/status").json()["models"][1]["latest"] is None


def counting_gets(storage) -> list[tuple[str, bool]]:
    """Record each get_object call on `storage` as (key, not_modified)."""
    calls: list[tuple[str, bool]] = []
    original = storage.get_object

    def get_object(key: str, if_none_match: str | None = None):
        obj = original(key, if_none_match)
        calls.append((key, obj is not None and obj.not_modified))
        return obj

    storage.get_object = get_object
    return calls


def test_status_comes_from_the_index(memory_storage) -> None:
    """With an index, the server reads that one object instead of every model's files."""
    store = ForecastStore(memory_storage)
    pointer = RunPointer("uwwrf_12km", INITIALIZED, "2026/10/21/1200Z", INITIALIZED)
    store.write_index(ForecastIndex([
        ModelIndex("uwwrf_12km", pointer, None, [INITIALIZED]),
        ModelIndex("retired", pointer, None, [INITIALIZED]),  # no longer configured: left out
    ]))
    export_run(memory_storage)  # in storage, but not yet in the index
    calls = counting_gets(memory_storage)
    memory_storage.list_keys = None  # listing would fail

    status = asyncio.run(StatusCache(store, CONFIG, DATA_URL).get())
    assert [(m.id, m.latest, m.runs) for m in status.models] == [
        ("uwwrf_4km", None, []),
        ("uwwrf_12km", RunRef(INITIALIZED, "2026/10/21/1200Z"), [RunRef(INITIALIZED, "2026/10/21/1200Z")]),
    ]
    assert sorted(key for key, _ in calls) == [EXPORT_STATUS, INDEX]


def test_refresh_rereads_only_changed_files(memory_storage) -> None:
    store = ForecastStore(memory_storage)
    store.write_index(ForecastIndex([ModelIndex("uwwrf_4km")]))
    cache = StatusCache(store, CONFIG, DATA_URL)
    calls = counting_gets(memory_storage)

    async def scenario() -> None:
        await cache.refresh()
        await cache.refresh()
        assert set(calls[-2:]) == {(INDEX, True), (EXPORT_STATUS, False)}  # no export status: re-read until there is one
        store.write_index(ForecastIndex([ModelIndex("uwwrf_4km", runs=[INITIALIZED])]))
        await cache.refresh()
        assert set(calls[-2:]) == {(INDEX, False), (EXPORT_STATUS, False)}
        assert (await cache.get()).models[0].runs == [RunRef(INITIALIZED, "2026/10/21/1200Z")]

    asyncio.run(scenario())


def test_status_lists_runs_until_the_index_exists(memory_storage) -> None:
    """Before the exporter first writes the index, every model's files are read; then the index takes over."""
    export_run(memory_storage)
    store = ForecastStore(memory_storage)
    cache = StatusCache(store, CONFIG, DATA_URL)

    async def scenario() -> None:
        assert (await cache.get()).models[0].latest.initialized == INITIALIZED
        store.write_index(ForecastIndex([ModelIndex("uwwrf_4km")]))
        await cache.refresh()
        assert (await cache.get()).models[0].latest is None

    asyncio.run(scenario())


def test_requests_dont_wait_for_storage_once_loaded(memory_storage) -> None:
    """Concurrent first requests share one read; after that, requests are served from memory until a refresh."""
    cache = StatusCache(ForecastStore(memory_storage), CONFIG, DATA_URL)
    calls = counting_gets(memory_storage)

    async def scenario() -> None:
        await asyncio.gather(*(cache.get() for _ in range(10)))
        reads = len(calls)
        export_run(memory_storage)
        assert (await cache.get()).models[0].latest is None
        assert len(calls) == reads == 2  # the index (missing) and the export status

    asyncio.run(scenario())


def test_background_refresh_keeps_going_after_failures(memory_storage) -> None:
    store = ForecastStore(memory_storage)
    sleeps: list[float] = []
    original = memory_storage.get_object

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:  # storage fails during the first background refresh
            memory_storage.get_object = original
            store.write_index(ForecastIndex([ModelIndex("uwwrf_4km", runs=[INITIALIZED])]))
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    def broken(*args) -> None:
        raise RuntimeError("s3 down")

    cache = StatusCache(store, CONFIG, DATA_URL, sleep=sleep)

    async def scenario() -> None:
        await cache.refresh()
        memory_storage.get_object = broken
        with pytest.raises(asyncio.CancelledError):
            await cache.run_forever()
        assert (await cache.get()).models[0].runs == [RunRef(INITIALIZED, "2026/10/21/1200Z")]

    asyncio.run(scenario())
    assert sleeps == [60.0, 60.0, 60.0]


def test_status_includes_in_progress_run(client: TestClient, storage) -> None:
    run = INITIALIZED + timedelta(hours=12)
    checked = run + timedelta(hours=2)
    ForecastStore(storage).write_in_progress(
        InProgress("uwwrf_4km", run, "2026/10/22/0000Z", checked, [LayerProgress("surface-temp", 3, 0, checked)])
    )
    export_run(storage, initialized=run, latest=False)
    # a fresh app, since the fixture's has already cached the status
    with TestClient(create_app(storage=storage, webapp_dist=Path("/nonexistent"), config=CONFIG)) as fresh:
        status = fresh.get("/fx/status").json()["models"][0]
    assert status["latest"]["path"] == "2026/10/21/1200Z"
    assert status["runs"][0]["path"] == "2026/10/22/0000Z"
    progress = status["in_progress"]
    assert (progress["path"], progress["complete"]) == ("2026/10/22/0000Z", False)
    assert progress["layers"][0]["id"] == "surface-temp" and progress["layers"][0]["name"] == "Surface temperature"
    assert (progress["layers"][0]["available_hour"], progress["layers"][0]["last_hour"]) == (0, 3)


NOW = datetime(2026, 10, 22, 6, tzinfo=UTC)


@pytest.mark.parametrize(
    ("layer", "state"),
    [
        (LayerProgress("surface-temp", 3, 3, NOW - timedelta(hours=5)), "complete"),
        (LayerProgress("surface-temp", 3, 0, NOW - timedelta(minutes=20)), "waiting"),
        (LayerProgress("surface-temp", 3, 0, NOW - STALL_AFTER - timedelta(minutes=1)), "stalled"),
        (LayerProgress("surface-temp", 3, 0, NOW, "hour 3: HTTP 503", NOW), "error"),
        # no frames yet: measured from when the run's first frame arrived (the other layer's)
        (LayerProgress("surface-temp", 3, None, None), "stalled"),
    ],
)
def test_layer_states(layer: LayerProgress, state: str) -> None:
    other = LayerProgress("snow", 6, 3, NOW - timedelta(hours=3))
    model = ModelConfig("m", "M", [0], [LAYER, LayerConfig("snow", "Snow", LAYER.url, HourRange(first=3, last=6), "washington")])
    status = in_progress_status(model, InProgress("m", NOW, "p", NOW, [layer, other]), NOW)
    assert status.layers[0].state == state
    assert status.layers[1].state == "stalled"
    assert not status.complete


def test_layer_states_for_new_and_finished_runs() -> None:
    model = ModelConfig("m", "M", [0], [LAYER])
    # a layer added to the config since the last check is waiting
    assert in_progress_status(model, InProgress("m", NOW, "p", NOW, []), NOW).layers[0].state == "waiting"
    done = in_progress_status(model, InProgress("m", NOW, "p", NOW, [LayerProgress("surface-temp", 3, 3, NOW)]), NOW)
    assert done.complete and done.layers[0].name == "Surface temperature"


# --- other paths


def test_unknown_fx_path_is_404_not_html(client: TestClient) -> None:
    for path in ("/fx", "/fx/web/manifest.json", "/fx/data/uwwrf_4km/latest.json", "/fx/other/thing"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "<html>" not in response.text


def test_server_errors_are_logged(dist: Path, memory_storage) -> None:
    from unittest.mock import patch

    class BrokenStorage(type(memory_storage)):
        def get_object(self, key: str, if_none_match: str | None = None) -> StoredObject | None:
            raise RuntimeError("storage exploded")

    with patch("upslope.server.app.logger") as logger:
        with TestClient(create_app(storage=BrokenStorage(), webapp_dist=dist, config=CONFIG)) as client:
            # the startup refresh failed, so each request tries storage again
            assert client.get("/fx/status").status_code == 500
            assert client.get("/fx/status").status_code == 500
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
    with TestClient(create_app(storage=storage, webapp_dist=tmp_path / "nope", config=CONFIG)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/fx/status").status_code == 200
        assert client.get("/").status_code == 404


# --- the public bucket


def test_forecasts_come_from_the_public_bucket(memory_storage, public_storage, dist: Path) -> None:
    export_run(public_storage)
    app = create_app(
        storage=memory_storage, public_storage=public_storage, webapp_dist=dist, config=CONFIG,
        data_url="https://upslope-data-public.example/forecasts",
    )
    with TestClient(app) as client:
        status = client.get("/fx/status").json()
        assert status["data_url"] == "https://upslope-data-public.example/forecasts"
        assert status["models"][0]["latest"]["path"] == "2026/10/21/1200Z"
    assert not any(key.startswith("forecasts/") for key in memory_storage.objects)


def test_data_url_from_the_environment(memory_storage, dist: Path, monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_DATA_URL", "http://localhost:9000/upslope-data-public/forecasts")
    with TestClient(create_app(storage=memory_storage, webapp_dist=dist, config=CONFIG)) as client:
        assert client.get("/fx/status").json()["data_url"] == "http://localhost:9000/upslope-data-public/forecasts"


def test_startup_needs_a_data_url(memory_storage, dist: Path, monkeypatch) -> None:
    monkeypatch.delenv("PUBLIC_DATA_URL")
    with pytest.raises(RuntimeError, match="PUBLIC_DATA_URL"):
        create_app(storage=memory_storage, webapp_dist=dist, config=CONFIG)
