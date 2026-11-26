"""Point forecast API: `GET /fx/point`, every model's forecast at a point, for charts that compare models one
variable at a time (see `upslope.archive.point` for the response).

Answered from each model's newest archived run, kept on the server's disk by `ArchiveCache`. Each response has a
`Server-Timing` header (read, build and total, in milliseconds) so slow requests can be told apart from a client.
"""

import time
from typing import Annotated

from cattrs.preconf.json import make_converter
from litestar import Controller, Response, get
from litestar.datastructures import State
from litestar.exceptions import ServiceUnavailableException, ValidationException
from litestar.params import Parameter

from upslope.archive.cache import ArchiveCache, PointTimings

converter = make_converter()

# The archive changes a few times a day, but a response names the runs it used, so it's never cached stale
POINT_CACHE_CONTROL = "no-cache"


def server_timing(timings: PointTimings, total: float) -> str:
    """A `Server-Timing` header value: the read and build phases and the handler's total, in milliseconds."""
    phases = {"read": timings.read, "build": timings.build, "total": total}
    return ", ".join(f"{name};dur={seconds * 1000:.1f}" for name, seconds in phases.items())


def _ids(value: str | None) -> list[str] | None:
    return None if not value else [part.strip() for part in value.split(",") if part.strip()]


class PointController(Controller):
    """Serves point forecasts. Expects `state.archive_cache` (ArchiveCache, or None when disabled)."""

    path = "/fx"

    @get("/point")
    async def point(
        self,
        state: State,
        lat: Annotated[float, Parameter(ge=-90, le=90, description="Latitude in degrees.")],
        lon: Annotated[float, Parameter(ge=-180, le=180, description="Longitude in degrees.")],
        elevation: Annotated[
            float | None,
            Parameter(ge=-500, le=9000, description="The point's elevation in metres, instead of the DEM's."),
        ] = None,
        interpolate: Annotated[bool, Parameter(description="Fill the hours between each model's values.")] = False,
        models: Annotated[str | None, Parameter(description="Comma-separated model ids; default every model.")] = None,
        variables: Annotated[str | None, Parameter(description="Comma-separated variable ids; default all.")] = None,
    ) -> Response:
        started = time.perf_counter()
        cache: ArchiveCache | None = state.archive_cache
        if cache is None:
            raise ServiceUnavailableException("point forecasts aren't enabled on this server (ARCHIVE_CACHE_DIR)")
        model_ids, variable_ids = _ids(models), _ids(variables)
        if model_ids is not None:
            known = [m.id for m in cache.config.models]
            if unknown := [m for m in model_ids if m not in known]:
                raise ValidationException(f"unknown models {', '.join(unknown)}; known: {', '.join(known)}")
        timings = PointTimings()
        forecast = await cache.point(lat, lon, elevation, interpolate, model_ids, timings)
        if variable_ids is not None:
            forecast.variables = {k: v for k, v in forecast.variables.items() if k in variable_ids}
        body = converter.unstructure(forecast)
        # total stops before Litestar encodes the JSON and gzips it, which happen after the handler returns
        headers = {
            "Cache-Control": POINT_CACHE_CONTROL,
            "Server-Timing": server_timing(timings, time.perf_counter() - started),
        }
        return Response(body, headers=headers)
