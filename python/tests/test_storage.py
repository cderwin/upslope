import io
from datetime import UTC, datetime
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from dateutil.tz import tzutc

from upslope.clients.storage import PreconditionFailed, StorageClient, StoredBytes


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


def test_delete_keys_batches_by_1000(stubbed) -> None:
    storage, stubber = stubbed
    keys = [f"k{i:04d}" for i in range(1500)]
    for batch in (keys[:1000], keys[1000:]):
        stubber.add_response(
            "delete_objects", {}, {"Bucket": "b", "Delete": {"Objects": [{"Key": k} for k in batch], "Quiet": True}}
        )
    storage.delete_keys(keys)


def test_delete_keys_raises_on_errors(stubbed) -> None:
    storage, stubber = stubbed
    stubber.add_response("delete_objects", {"Errors": [{"Key": "k", "Code": "AccessDenied", "Message": "no"}]})
    with pytest.raises(RuntimeError, match="could not delete 1 objects"):
        storage.delete_keys(["k"])


def test_upload_file_guesses_content_type(tmp_path) -> None:
    s3 = MagicMock()
    path = tmp_path / "2026-10-29T0600.png"
    path.write_bytes(b"png")
    StorageClient(s3=s3, bucket="b").upload_file(path, "forecasts/m/x.png")
    s3.upload_file.assert_called_once_with(str(path), "b", "forecasts/m/x.png", ExtraArgs={"ContentType": "image/png"})
