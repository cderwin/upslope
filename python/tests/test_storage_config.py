"""Which buckets the storage settings in the environment pick."""

import pytest
from click.testing import CliRunner

from upslope.cli.main import main
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


def test_public_bucket_falls_back_to_the_private_one(env) -> None:
    """Before the public bucket is set up, forecasts stay where they were."""
    env.setenv("PUBLIC_BUCKET_NAME", "")
    assert StorageClient.build_public().bucket == "upslope-storage"


def test_old_forecasts_kept_while_still_in_use(env) -> None:
    """forecasts/ in the private bucket can't be deleted while no other bucket serves forecasts."""
    result = CliRunner().invoke(main, ["delete-legacy-exports", "--old-forecasts", "--yes"])
    assert result.exit_code != 0 and "still in use" in result.output
    env.setenv("PUBLIC_BUCKET_NAME", "upslope-storage")
    result = CliRunner().invoke(main, ["delete-legacy-exports", "--old-forecasts", "--yes"])
    assert result.exit_code != 0 and "still in use" in result.output


def test_migrate_needs_a_destination(env) -> None:
    result = CliRunner().invoke(main, ["migrate-forecasts", "--dry-run"])
    assert result.exit_code != 0 and "PUBLIC_BUCKET_NAME" in result.output
