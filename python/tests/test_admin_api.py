"""The admin API over HTTP: auth, status codes, and the draft/publish flow."""

from pathlib import Path

import pytest
from litestar.testing import TestClient

from upslope.cli import DEV_GEOREGISTRATION
from upslope.clients.annotations import AnnotationStore, read_snapshot
from upslope.server.app import create_app

BASE = "/admin/georegistration"
AUTH = {"Authorization": "Bearer secret"}
ANNOTATION = f"{BASE}/draft/maptypes/washington/images/legacy-hour06/annotations"


@pytest.fixture
def store(memory_storage) -> AnnotationStore:
    store = AnnotationStore(memory_storage)
    document = read_snapshot(DEV_GEOREGISTRATION)
    store.publish(document)
    store.save_draft(document, create=True)
    store.put_image("washington", "legacy-hour06", b"GIF89a")
    return store


@pytest.fixture
def client(store: AnnotationStore, memory_storage, tmp_path: Path):
    app = create_app(storage=memory_storage, webapp_dist=tmp_path, run_jobs=False, admin_token="secret")
    with TestClient(app) as client:
        yield client


# --- auth


def test_missing_token_is_401(client: TestClient) -> None:
    response = client.get(f"{BASE}/draft")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert client.get(f"{BASE}/draft", headers={"Authorization": "Basic abc"}).status_code == 401


def test_wrong_token_is_403(client: TestClient) -> None:
    assert client.get(f"{BASE}/draft", headers={"Authorization": "Bearer nope"}).status_code == 403


def test_disabled_without_token(memory_storage, tmp_path: Path) -> None:
    with TestClient(create_app(storage=memory_storage, webapp_dist=tmp_path, run_jobs=False, admin_token="")) as client:
        assert client.get(f"{BASE}/draft", headers=AUTH).status_code == 503


def test_every_route_is_guarded(client: TestClient) -> None:
    for method, path in [
        ("GET", "/published"),
        ("GET", "/draft/fit"),
        ("GET", "/images/washington/legacy-hour06"),
        ("GET", "/history"),
        ("POST", "/publish"),
        ("POST", "/draft/points"),
        ("DELETE", "/draft/points/cape_flattery"),
        ("PUT", "/draft/maptypes/washington/images/legacy-hour06/annotations/cape_flattery"),
    ]:
        assert client.request(method, f"{BASE}{path}").status_code == 401, path


# --- draft


def test_get_draft(client: TestClient) -> None:
    response = client.get(f"{BASE}/draft", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["etag"] == '"1"'
    body = response.json()
    assert body["published_revision"] == 1
    assert body["document"]["geographic_points"]["cape_flattery"]["name"] == "Cape Flattery, WA"


def test_point_lifecycle(client: TestClient) -> None:
    response = client.post(f"{BASE}/draft/points", json={"name": "Mount Rainier", "latitude": 46.853, "longitude": -121.76}, headers=AUTH)
    assert response.status_code == 201
    assert response.json()["key"] == "mount_rainier"
    assert response.headers["etag"] == '"2"'

    response = client.put(f"{BASE}/draft/points/mount_rainier", json={"name": "Rainier", "latitude": 46.85, "longitude": -121.76}, headers=AUTH)
    assert response.json()["document"]["geographic_points"]["mount_rainier"]["name"] == "Rainier"

    response = client.delete(f"{BASE}/draft/points/cape_flattery", headers=AUTH)
    assert response.status_code == 200 and response.json()["removed_annotations"] > 0
    assert client.delete(f"{BASE}/draft/points/cape_flattery", headers=AUTH).status_code == 404


@pytest.mark.parametrize("body", [{"name": "x"}, {"name": "x", "latitude": True, "longitude": 1}, {"name": 5, "latitude": 1, "longitude": 1}, [1]])
def test_bad_point_body_is_422(client: TestClient, body) -> None:
    assert client.post(f"{BASE}/draft/points", json=body, headers=AUTH).status_code == 422


def test_annotation_put_and_delete(client: TestClient) -> None:
    response = client.put(f"{ANNOTATION}/cape_flattery", json={"pixel_x": 5, "pixel_y": 6}, headers=AUTH)
    assert response.status_code == 200
    image = response.json()["document"]["maptypes"]["washington"]["images"]["legacy-hour06"]
    assert image["annotations"]["cape_flattery"] == {"pixel_x": 5, "pixel_y": 6}

    assert client.put(f"{ANNOTATION}/cape_flattery", json={"pixel_x": 5.5, "pixel_y": 6}, headers=AUTH).status_code == 422
    assert client.put(f"{ANNOTATION}/cape_flattery", json={"pixel_x": 99999, "pixel_y": 6}, headers=AUTH).status_code == 422
    assert client.delete(f"{ANNOTATION}/cape_flattery", headers=AUTH).status_code == 200
    assert client.delete(f"{ANNOTATION}/cape_flattery", headers=AUTH).status_code == 404


def test_stale_if_match_is_412(client: TestClient) -> None:
    body = {"pixel_x": 5, "pixel_y": 6}
    assert client.put(f"{ANNOTATION}/cape_flattery", json=body, headers={**AUTH, "If-Match": '"1"'}).status_code == 200
    response = client.put(f"{ANNOTATION}/cape_flattery", json=body, headers={**AUTH, "If-Match": '"1"'})
    assert response.status_code == 412 and response.json()["current_revision"] == 2
    assert client.put(f"{ANNOTATION}/cape_flattery", json=body, headers={**AUTH, "If-Match": "abc"}).status_code == 400


def test_fit_threshold_and_fit(client: TestClient) -> None:
    response = client.put(f"{BASE}/draft/maptypes/washington/fit-threshold", json={"meters": 100}, headers=AUTH)
    assert response.json()["document"]["maptypes"]["washington"]["fit_threshold_m"] == 100
    fit = client.get(f"{BASE}/draft/fit", headers=AUTH).json()
    assert fit["washington"]["over_threshold"] is True
    assert fit["western-washington"]["over_threshold"] is False
    response = client.put(f"{BASE}/draft/maptypes/washington/fit-threshold", json={"meters": None}, headers=AUTH)
    assert response.json()["document"]["maptypes"]["washington"]["fit_threshold_m"] is None


def test_image_served_with_cache_headers(client: TestClient) -> None:
    response = client.get(f"{BASE}/images/washington/legacy-hour06", headers=AUTH)
    assert response.status_code == 200 and response.content == b"GIF89a"
    assert "immutable" in response.headers["cache-control"]
    again = client.get(f"{BASE}/images/washington/legacy-hour06", headers={**AUTH, "If-None-Match": response.headers["etag"]})
    assert again.status_code == 304
    assert client.get(f"{BASE}/images/washington/missing", headers=AUTH).status_code == 404


def test_publish_flow(client: TestClient) -> None:
    assert client.post(f"{BASE}/publish", json={"revision": 1}, headers=AUTH).status_code == 422  # nothing new

    client.put(f"{BASE}/draft/maptypes/washington/fit-threshold", json={"meters": 100}, headers=AUTH)
    assert client.post(f"{BASE}/publish", json={"revision": 1}, headers=AUTH).status_code == 412
    response = client.post(f"{BASE}/publish", json={"revision": 2, "note": "tighter"}, headers=AUTH)
    assert response.status_code == 200
    result = response.json()
    assert result["history"]["revision"] == 2 and len(result["warnings"]) == 1

    assert client.get(f"{BASE}/published", headers=AUTH).json()["revision"] == 2
    history = client.get(f"{BASE}/history", headers=AUTH).json()
    assert [entry["revision"] for entry in history] == [2, 1]
    assert client.get(f"{BASE}/history/{history[0]['id']}", headers=AUTH).json()["note"] == "tighter"

    response = client.post(f"{BASE}/history/{history[1]['id']}/restore", headers=AUTH)
    assert response.json()["document"]["maptypes"]["washington"]["fit_threshold_m"] == 1170
    assert client.post(f"{BASE}/history/nope/restore", headers=AUTH).status_code == 404


def test_publish_blocked_lists_problems(client: TestClient) -> None:
    for image in ("legacy-hour06", "legacy-hour36"):
        client.delete(f"{BASE}/draft/maptypes/washington/images/{image}", headers=AUTH)
    response = client.post(f"{BASE}/publish", json={"revision": 3}, headers=AUTH)
    assert response.status_code == 422
    assert any(problem.startswith("washington:") for problem in response.json()["problems"])


def test_discard(client: TestClient) -> None:
    client.delete(f"{BASE}/draft/points/cape_flattery", headers=AUTH)
    response = client.post(f"{BASE}/draft/discard", headers=AUTH)
    assert "cape_flattery" in response.json()["document"]["geographic_points"]


def test_admin_paths_not_served_as_frontend(store, memory_storage, tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html>app</html>")
    with TestClient(create_app(storage=memory_storage, webapp_dist=tmp_path, run_jobs=False, admin_token="secret")) as client:
        assert client.get("/admin/nope", headers=AUTH).status_code == 404
        assert client.get("/annotation").text == "<html>app</html>"


def test_sources_and_promote(client: TestClient, memory_storage) -> None:
    sources = client.get(f"{BASE}/sources", headers=AUTH).json()
    assert {"model", "variable", "maptype", "hours"} <= sources[0].keys()

    from unittest.mock import patch

    from upslope.annotation_service import UpstreamError

    source = next(s for s in sources if s["maptype"] == "washington")
    body = {"model": source["model"], "variable": source["variable"], "hour": source["hours"][0]}
    with patch("upslope.annotation_service.AnnotationService.promote", side_effect=UpstreamError("UW down")):
        assert client.post(f"{BASE}/draft/images", json=body, headers=AUTH).status_code == 502
    assert client.post(f"{BASE}/draft/images", json={**body, "hour": "6"}, headers=AUTH).status_code == 422


def test_handled_errors_are_not_logged_as_server_errors(client: TestClient, caplog) -> None:
    import logging

    with caplog.at_level(logging.ERROR, logger="upslope.server.app"):
        assert client.delete(f"{BASE}/draft/points/nope", headers=AUTH).status_code == 404
    assert not caplog.records
