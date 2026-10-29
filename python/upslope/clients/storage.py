import mimetypes
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

import boto3
from attrs import define
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from upslope.logging import get_logger

logger = get_logger(__name__)

_NOT_FOUND_CODES = ("404", "NoSuchKey", "NotFound")


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


@define
class StoredObject:
    """An object fetched from storage.

    Attributes:
        body: Content in chunks; closes the underlying connection when exhausted or closed.
            None when `not_modified` is set.
        content_type: Media type, if stored.
        content_length: Size in bytes, if known.
        etag: Entity tag (quoted, as S3 returns it), if known.
        last_modified: Last modification time, if known.
        not_modified: True if the object matched the caller's `if_none_match` ETag.
    """

    body: Iterator[bytes] | None
    content_type: str | None = None
    content_length: int | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    not_modified: bool = False


class PreconditionFailed(Exception):
    """A conditional write lost: the object changed (If-Match) or already exists (If-None-Match)."""


# S3 answers a failed condition with 412, or 409 when a concurrent conditional write wins the race
_PRECONDITION_CODES = ("412", "PreconditionFailed", "409", "ConditionalRequestConflict")


@define
class StoredBytes:
    """A small object read fully into memory.

    Attributes:
        data: The object's content.
        etag: Entity tag (quoted, as S3 returns it), for conditional writes.
    """

    data: bytes
    etag: str | None


def _iter_body(body, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    try:
        yield from body.iter_chunks(chunk_size)
    finally:
        body.close()


@define
class StorageConfig:
    access_key_id: str
    secret_access_key: str
    endpoint_url: str
    bucket: str
    region: str = "auto"

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"],
            bucket=os.environ.get("BUCKET_NAME", "upslope-storage"),
            region=os.environ.get("AWS_REGION", "auto"),
        )


@define
class StorageClient:
    """Interface to S3/Tigris storage that hides the S3 API from the rest of the app.

    Attributes:
        s3: boto3 S3 client.
        bucket: Bucket to write to.
    """

    s3: BaseClient
    bucket: str

    def get_object(self, key: str, if_none_match: str | None = None) -> StoredObject | None:
        """Fetch an object for streaming.

        Args:
            key: Bucket key.
            if_none_match: ETag the caller already has; if it still matches, no body is fetched.

        Returns:
            The object (with `not_modified` set if the ETag matched), or None if it does not exist.
        """
        kwargs = {"Bucket": self.bucket, "Key": key}
        if if_none_match:
            kwargs["IfNoneMatch"] = if_none_match
        try:
            response = self.s3.get_object(**kwargs)
        except ClientError as e:
            if _error_code(e) in _NOT_FOUND_CODES:
                return None
            if _error_code(e) in ("304", "NotModified"):
                return StoredObject(body=None, etag=if_none_match, not_modified=True)
            raise
        return StoredObject(
            body=_iter_body(response["Body"]),
            content_type=response.get("ContentType"),
            content_length=response.get("ContentLength"),
            etag=response.get("ETag"),
            # boto uses dateutil's tzutc; normalize so callers get the standard library's UTC
            last_modified=response["LastModified"].astimezone(UTC) if response.get("LastModified") else None,
        )

    def get_bytes(self, key: str) -> StoredBytes | None:
        """Read a small object fully into memory.

        Args:
            key: Bucket key.

        Returns:
            The content and its ETag, or None if the object does not exist.
        """
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if _error_code(e) in _NOT_FOUND_CODES:
                return None
            raise
        return StoredBytes(data=response["Body"].read(), etag=response.get("ETag"))

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> str | None:
        """Write an object, optionally only if it is unchanged or does not exist yet.

        Args:
            key: Bucket key.
            data: Content to write.
            content_type: Media type to store with the object.
            if_match: Only write if the stored object still has this ETag.
            if_none_match: Only write if no object exists at `key`.

        Returns:
            The new object's ETag. Raises PreconditionFailed if a condition does not hold.
        """
        kwargs = {"Bucket": self.bucket, "Key": key, "Body": data, "ContentType": content_type}
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        if if_none_match:
            kwargs["IfNoneMatch"] = "*"
        try:
            response = self.s3.put_object(**kwargs)
        except ClientError as e:
            if _error_code(e) in _PRECONDITION_CODES:
                raise PreconditionFailed(key) from e
            raise
        return response.get("ETag")

    def list_keys(self, prefix: str) -> list[str]:
        """List every key under a prefix, in lexicographic order.

        Args:
            prefix: Key prefix, e.g. `georegistration/history/`.

        Returns:
            The matching keys.
        """
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(item["Key"] for item in page.get("Contents", []))
        return sorted(keys)

    def upload_file(self, path: Path, key: str, content_type: str | None = None) -> None:
        """Upload a local file.

        Args:
            path: File to upload.
            key: Bucket key to write.
            content_type: Media type; guessed from the file name if omitted.
        """
        content_type = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.s3.upload_file(str(path), self.bucket, key, ExtraArgs={"ContentType": content_type})

    def delete_keys(self, keys: list[str]) -> None:
        """Delete objects. Keys that don't exist are ignored.

        Args:
            keys: Bucket keys to delete.
        """
        # DeleteObjects takes at most 1000 keys per request
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            response = self.s3.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True}
            )
            if errors := response.get("Errors"):
                raise RuntimeError(f"could not delete {len(errors)} objects, e.g. {errors[0].get('Key')}: {errors[0].get('Message')}")

    @classmethod
    def build(cls) -> Self:
        """Create a StorageClient from S3 settings in environment variables.

        Returns:
            The configured StorageClient.
        """
        config = StorageConfig.from_env()
        s3 = boto3.client(
            "s3",
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            endpoint_url=config.endpoint_url,
            region_name=config.region,
            # tigris requires virtual-hosted-style addressing
            config=Config(s3={"addressing_style": "virtual"}),
        )
        return cls(s3=s3, bucket=config.bucket)
