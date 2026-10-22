"""Shared test doubles."""

import hashlib
from collections.abc import Iterator

import pytest

from upslope.clients.storage import PreconditionFailed, StoredBytes, StoredObject


class MemoryStorage:
    """In-memory stand-in for StorageClient's object methods, with S3-style conditional writes."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
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
        self, key: str, data: bytes, content_type: str, if_match: str | None = None, if_none_match: bool = False
    ) -> str:
        current = self.objects.get(key)
        if if_none_match and current is not None:
            raise PreconditionFailed(key)
        if if_match is not None and (current is None or self._etag(current[0]) != if_match):
            raise PreconditionFailed(key)
        self.objects[key] = (data, content_type)
        self.puts.append(key)
        return self._etag(data)

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


@pytest.fixture
def memory_storage() -> Iterator[MemoryStorage]:
    yield MemoryStorage()
