"""Serves the built webapp (`webapp/dist`) as a single-page app."""

import mimetypes
from collections.abc import Collection
from pathlib import Path

from litestar import Router, get
from litestar.exceptions import NotFoundException
from litestar.params import FromPath
from litestar.response import File

# Vite fingerprints everything under assets/, so those files never change at a given URL.
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
FILE_CACHE_CONTROL = "public, max-age=300"
# index.html names the current asset hashes, so browsers must always revalidate it.
INDEX_CACHE_CONTROL = "no-cache"


def _file(path: Path, cache_control: str) -> File:
    # set the type explicitly: browsers refuse module scripts served as application/octet-stream
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return File(path, media_type=media_type, content_disposition_type="inline", headers={"Cache-Control": cache_control})


def create_frontend_router(dist: Path, reserved_prefixes: Collection[str] = ()) -> Router:
    """Build a router serving `dist` at `/`.

    Existing files are served as-is. Any other path gets `index.html` so the app's client-side
    routes (e.g. `/map/...`) load, except paths under `reserved_prefixes`, which 404 so API
    typos don't come back as HTML.

    Args:
        dist: Built webapp directory containing `index.html`.
        reserved_prefixes: First path segments owned by other routes, e.g. `("fx", "health")`.
    """
    dist = dist.resolve()
    index = dist / "index.html"
    reserved = frozenset(reserved_prefixes)

    def index_response() -> File:
        return _file(index, INDEX_CACHE_CONTROL)

    @get("/", include_in_schema=False)
    async def serve_index() -> File:
        return index_response()

    @get("/{path:path}", include_in_schema=False)
    async def serve_path(path: FromPath[str]) -> File:
        relative = path.lstrip("/")
        if relative.split("/", 1)[0] in reserved:
            raise NotFoundException()

        candidate = (dist / relative).resolve()
        if candidate.is_relative_to(dist) and candidate.is_file():
            cache_control = ASSET_CACHE_CONTROL if relative.startswith("assets/") else FILE_CACHE_CONTROL
            return _file(candidate, cache_control)
        return index_response()

    return Router(path="/", route_handlers=[serve_index, serve_path])
