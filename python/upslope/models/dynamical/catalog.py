"""Opening dynamical.org datasets: look each one up in the STAC catalog, then open its Icechunk repository.

Store locations are versioned and move, so they're looked up every time rather than hard-coded
(https://dynamical.org/migration-2026/). Virtual datasets keep only references to the upstream GRIB
files (NOAA's and ECMWF's open-data buckets), which Icechunk reads anonymously once it's allowed to.
"""

import logging

import httpx
import icechunk
import xarray as xr

from upslope.logging import get_logger, log_duration

logger = get_logger(__name__)

STAC_COLLECTION_URL = "https://stac.dynamical.org/{dataset}/collection.json"
# The collection asset holding the Icechunk repository, served over HTTPS
ICECHUNK_ASSET = "icechunk-https"

# Every request has a time limit, so a stalled upstream fails the frame (retried next check) instead of hanging
# the export. Repository metadata comes over HTTPS (object_store options); chunks come through Icechunk's storage.
HTTP_STORAGE_OPTIONS = {"timeout": "60s", "connect_timeout": "10s"}
STORAGE_SETTINGS = icechunk.StorageSettings(
    timeouts=icechunk.StorageTimeoutSettings(connect_timeout_ms=10_000, read_timeout_ms=60_000, operation_timeout_ms=180_000),
    retries=icechunk.StorageRetriesSettings(max_tries=4, initial_backoff_ms=500, max_backoff_ms=10_000),
)


def store_url(dataset: str, client: httpx.Client) -> str:
    """The Icechunk repository URL of a catalog dataset. Raises httpx.HTTPError or KeyError if it can't be found."""
    response = client.get(STAC_COLLECTION_URL.format(dataset=dataset))
    _ = response.raise_for_status()
    return response.json()["assets"][ICECHUNK_ASSET]["href"]


def anonymous_credentials(url_prefix: str) -> icechunk.AnyCredential:
    """Credentials to read a public bucket of virtual chunks anonymously."""
    if url_prefix.startswith("s3://"):
        return icechunk.s3_anonymous_credentials()
    if url_prefix.startswith("gs://"):
        return icechunk.gcs_credentials(anonymous=True)
    raise ValueError(f"no anonymous access for virtual chunks under {url_prefix!r}")


def pin_aws_endpoints(config: icechunk.RepositoryConfig) -> None:
    """Point each S3 bucket of virtual chunks at AWS explicitly.

    Without an endpoint, Icechunk's S3 client takes one from `AWS_ENDPOINT_URL_S3` / `AWS_ENDPOINT_URL`, which
    name our own bucket (Tigris in production, MinIO locally), so NOAA's and ECMWF's chunks would be fetched
    from there and fail.
    """
    for prefix, container in (config.virtual_chunk_containers or {}).items():
        store = container.store
        if isinstance(store, icechunk.ObjectStoreConfig.S3) and store[0].endpoint_url is None:
            region = store[0].region or "us-east-1"
            endpoint = f"https://s3.{region}.amazonaws.com"
            config.set_virtual_chunk_container(icechunk.VirtualChunkContainer(prefix, icechunk.s3_store(region=region, endpoint_url=endpoint)))


def open_dataset(dataset: str, client: httpx.Client) -> xr.Dataset:
    """Open the latest snapshot of a catalog dataset, lazily: data is read when indexed.

    Args:
        dataset: Catalog dataset id, e.g. `noaa-gfs-forecast-virtual`.
        client: HTTP client for the catalog.

    Returns:
        The dataset, as one consistent snapshot.
    """
    with log_duration(logger, f"opened {dataset}", logging.DEBUG):
        storage = icechunk.http_storage(store_url(dataset, client), opts=HTTP_STORAGE_OPTIONS)
        config = icechunk.Repository.fetch_config(storage) or icechunk.RepositoryConfig.default()
        config.storage = STORAGE_SETTINGS
        containers = config.virtual_chunk_containers or {}
        credentials = {prefix: anonymous_credentials(prefix) for prefix in containers}
        pin_aws_endpoints(config)
        repo = icechunk.Repository.open(
            storage, config=config, authorize_virtual_chunk_access=icechunk.containers_credentials(credentials) if credentials else None
        )
        return xr.open_zarr(repo.readonly_session("main").store, chunks=None, decode_timedelta=True)
