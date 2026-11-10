"""Shared test doubles."""

import hashlib
import mimetypes
from collections.abc import Iterator
from pathlib import Path

import pytest

from upslope.clients.storage import PreconditionFailed, StoredBytes, StoredObject


class MemoryStorage:
    """In-memory stand-in for StorageClient's object methods, with S3-style conditional writes."""

    def __init__(self, bucket: str = "upslope-storage") -> None:
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.cache_control: dict[str, str | None] = {}
        self.puts: list[str] = []

    @staticmethod
    def _etag(data: bytes) -> str:
        return f'"{hashlib.md5(data).hexdigest()}"'

    def get_bytes(self, key: str) -> StoredBytes | None:
        if key not in self.objects:
            return None
        data, _ = self.objects[key]
        return StoredBytes(data=data, etag=self._etag(data))

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
        cache_control: str | None = None,
    ) -> str:
        current = self.objects.get(key)
        if if_none_match and current is not None:
            raise PreconditionFailed(key)
        if if_match is not None and (current is None or self._etag(current[0]) != if_match):
            raise PreconditionFailed(key)
        self.objects[key] = (data, content_type)
        self.cache_control[key] = cache_control
        self.puts.append(key)
        return self._etag(data)

    def upload_file(self, path: Path, key: str, content_type: str | None = None, cache_control: str | None = None) -> None:
        content_type = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
        self.put_bytes(key, path.read_bytes(), content_type, cache_control=cache_control)

    def copy_from(self, source_bucket: str, key: str, cache_control: str | None = None) -> None:
        source = BUCKETS[source_bucket]
        data, _ = source.objects[key]
        self.put_bytes(key, data, mimetypes.guess_type(key)[0] or "application/octet-stream", cache_control=cache_control)

    def delete_keys(self, keys: list[str]) -> None:
        for key in keys:
            self.objects.pop(key, None)

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))

    def get_object(self, key: str, if_none_match: str | None = None) -> StoredObject | None:
        if key not in self.objects:
            return None
        data, content_type = self.objects[key]
        etag = self._etag(data)
        if if_none_match == etag:
            return StoredObject(body=None, etag=etag, not_modified=True)
        return StoredObject(body=iter([data]), content_type=content_type, content_length=len(data), etag=etag)


# What tests' apps report as `data_url`, unless a test passes its own
DATA_URL = "https://data.example/forecasts"


@pytest.fixture(autouse=True)
def data_url(monkeypatch) -> str:
    """The server won't start without PUBLIC_DATA_URL."""
    monkeypatch.setenv("PUBLIC_DATA_URL", DATA_URL)
    return DATA_URL


# Buckets made by the fixtures, by name, so a copy can find its source bucket
BUCKETS: dict[str, MemoryStorage] = {}


@pytest.fixture
def memory_storage() -> Iterator[MemoryStorage]:
    BUCKETS["upslope-storage"] = storage = MemoryStorage()
    yield storage
    BUCKETS.clear()


@pytest.fixture
def public_storage() -> Iterator[MemoryStorage]:
    """A second bucket, standing in for the public forecasts bucket."""
    BUCKETS["upslope-data-public"] = storage = MemoryStorage("upslope-data-public")
    yield storage
    BUCKETS.clear()
