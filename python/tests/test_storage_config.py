"""Which buckets the storage settings in the environment pick."""

import pytest

from upslope.clients.storage import StorageClient

ENV = {"AWS_ACCESS_KEY_ID": "id", "AWS_SECRET_ACCESS_KEY": "secret", "AWS_ENDPOINT_URL_S3": "http://localhost:1"}


@pytest.fixture
def env(monkeypatch) -> pytest.MonkeyPatch:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BUCKET_NAME", "upslope-storage")
    monkeypatch.delenv("PUBLIC_BUCKET_NAME", raising=False)
    return monkeypatch


def test_public_bucket_from_the_environment(env) -> None:
    env.setenv("PUBLIC_BUCKET_NAME", "upslope-data-public")
    assert StorageClient.build().bucket == "upslope-storage"
    assert StorageClient.build_public().bucket == "upslope-data-public"


def test_public_bucket_is_required(env) -> None:
    env.setenv("PUBLIC_BUCKET_NAME", "")
    with pytest.raises(KeyError, match="PUBLIC_BUCKET_NAME"):
        StorageClient.build_public()
