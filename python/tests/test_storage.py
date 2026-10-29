import io
from datetime import UTC, datetime
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from dateutil.tz import tzutc

from upslope.clients.storage import ModelRunMarker, PreconditionFailed, StorageClient, StoredBytes

MARKER_JSON = b'{"model": "uwwrf", "initialized": "2026-10-21T12:00:00+00:00", "manifest": "web_exports/20261021T130000/manifest.json"}'


def body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


@pytest.fixture
def stubbed():
    s3 = boto3.client(
        "s3", region_name="auto", aws_access_key_id="x", aws_secret_access_key="x", endpoint_url="https://example.invalid"
    )
    with Stubber(s3) as stubber:
        yield StorageClient(s3=s3, bucket="b"), stubber
        stubber.assert_no_pending_responses()


def test_latest_model_run_picks_newest_key(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "model_runs/uwwrf/20261021T1200Z.json"}, {"Key": "model_runs/uwwrf/20261020T0000Z.json"}]},
        {"Bucket": "b", "Prefix": "model_runs/uwwrf/"},
    )
    stubber.add_response(
        "get_object", {"Body": body(MARKER_JSON)}, {"Bucket": "b", "Key": "model_runs/uwwrf/20261021T1200Z.json"}
    )
    marker = storage.latest_model_run("uwwrf")
    assert marker == ModelRunMarker("uwwrf", datetime(2026, 10, 21, 12, tzinfo=UTC), "web_exports/20261021T130000/manifest.json")
    assert marker.export_prefix == "web_exports/20261021T130000"


def test_latest_model_run_none_when_empty(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_response("list_objects_v2", {}, {"Bucket": "b", "Prefix": "model_runs/uwwrf/"})
    assert storage.latest_model_run("uwwrf") is None


def test_marker_round_trips(stubbed) -> None:
    storage, stubber = stubbed
    marker = ModelRunMarker("uwwrf", datetime(2026, 10, 21, 12, tzinfo=UTC), "web_exports/20261021T130000/manifest.json")
    stubber.add_response("put_object", {}, None)
    storage.mark_model_run_exported(marker.model, marker.initialized, marker.manifest)
    stubber.add_response("list_objects_v2", {"Contents": [{"Key": "model_runs/uwwrf/20261021T1200Z.json"}]}, None)
    stubber.add_response("get_object", {"Body": body(MARKER_JSON)}, None)
    assert storage.latest_model_run("uwwrf") == marker


def test_model_run_marker_reads_revision(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_response(
        "get_object",
        {"Body": body(MARKER_JSON[:-1] + b', "registration_revision": 4}')},
        {"Bucket": "b", "Key": "model_runs/uwwrf/20261021T1200Z.json"},
    )
    marker = storage.model_run_marker("uwwrf", datetime(2026, 10, 21, 12, tzinfo=UTC))
    assert marker.registration_revision == 4
    assert marker.config_fingerprint is None
    stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    assert storage.model_run_marker("uwwrf", datetime(2026, 10, 21, 0, tzinfo=UTC)) is None


def test_get_object_streams_body(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_response(
        "get_object",
        {
            "Body": body(b"png"),
            "ContentType": "image/png",
            "ContentLength": 3,
            "ETag": '"abc"',
            "LastModified": datetime(2026, 10, 21, 13, 5, tzinfo=tzutc()),
        },
        {"Bucket": "b", "Key": "k"},
    )
    obj = storage.get_object("k")
    assert obj is not None and not obj.not_modified
    assert b"".join(obj.body) == b"png"
    assert (obj.content_type, obj.content_length, obj.etag) == ("image/png", 3, '"abc"')
    assert obj.last_modified.tzinfo is UTC  # standard library UTC, not boto's tzutc


def test_get_object_missing(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    assert storage.get_object("k") is None


def test_get_object_not_modified(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_client_error(
        "get_object", service_error_code="304", http_status_code=304, expected_params={"Bucket": "b", "Key": "k", "IfNoneMatch": '"abc"'}
    )
    obj = storage.get_object("k", if_none_match='"abc"')
    assert obj is not None and obj.not_modified and obj.body is None


def test_get_object_other_errors_raise(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
    with pytest.raises(Exception, match="AccessDenied"):
        storage.get_object("k")


def test_get_bytes_reads_content_and_etag(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_response("get_object", {"Body": body(b"{}"), "ETag": '"e1"'}, {"Bucket": "b", "Key": "k"})
    assert storage.get_bytes("k") == StoredBytes(data=b"{}", etag='"e1"')


def test_get_bytes_missing(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    assert storage.get_bytes("k") is None


def test_put_bytes_passes_conditions(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_response(
        "put_object",
        {"ETag": '"e2"'},
        {"Bucket": "b", "Key": "k", "Body": b"x", "ContentType": "application/json", "IfMatch": '"e1"'},
    )
    assert storage.put_bytes("k", b"x", "application/json", if_match='"e1"') == '"e2"'
    stubber.add_response(
        "put_object",
        {"ETag": '"e3"'},
        {"Bucket": "b", "Key": "k", "Body": b"x", "ContentType": "application/json", "IfNoneMatch": "*"},
    )
    assert storage.put_bytes("k", b"x", "application/json", if_none_match=True) == '"e3"'


@pytest.mark.parametrize(("code", "status"), [("PreconditionFailed", 412), ("ConditionalRequestConflict", 409)])
def test_put_bytes_failed_condition_raises(stubbed, code: str, status: int) -> None:
    storage, stubber = stubbed
    stubber.add_client_error("put_object", service_error_code=code, http_status_code=status)
    with pytest.raises(PreconditionFailed):
        storage.put_bytes("k", b"x", "application/json", if_match='"e1"')


def test_list_keys_pages_and_sorts(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "p/b"}, {"Key": "p/a"}], "IsTruncated": True, "NextContinuationToken": "t"},
        {"Bucket": "b", "Prefix": "p/"},
    )
    stubber.add_response(
        "list_objects_v2", {"Contents": [{"Key": "p/c"}]}, {"Bucket": "b", "Prefix": "p/", "ContinuationToken": "t"}
    )
    assert storage.list_keys("p/") == ["p/a", "p/b", "p/c"]


def test_marker_records_config_fingerprint() -> None:
    s3 = MagicMock()
    storage = StorageClient(s3=s3, bucket="b")
    initialized = datetime(2026, 10, 21, 12, tzinfo=UTC)
    storage.mark_model_run_exported("uwwrf", initialized, "web_exports/x/manifest.json", 4, "abc123")
    written = s3.put_object.call_args.kwargs["Body"]

    s3.get_object.return_value = {"Body": body(written.encode())}
    marker = storage.model_run_marker("uwwrf", initialized)
    assert marker == ModelRunMarker("uwwrf", initialized, "web_exports/x/manifest.json", 4, "abc123")
