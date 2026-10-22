"""Admin API for the georegistration annotations: `/admin/georegistration/...`.

Every route needs `Authorization: Bearer <ADMIN_TOKEN>`: a missing token is 401, a wrong one 403,
and with no ADMIN_TOKEN configured the API answers 503. Changes accept an optional
`If-Match: "<revision>"` and fail with 412 if the draft has moved on.
"""

import hmac
from typing import Any

from litestar import Controller, Request, Response, delete, get, post, put
from litestar.connection import ASGIConnection
from litestar.datastructures import State
from litestar.exceptions import (
    ClientException,
    NotAuthorizedException,
    PermissionDeniedException,
    ServiceUnavailableException,
)
from litestar.handlers import BaseRouteHandler
from litestar.params import FromPath, JSONBody
from litestar.response import Stream

from upslope.annotation_service import AnnotationError, AnnotationService, Conflict, DraftState, Invalid, NotFound
from upslope.models.uw_wrf.georegistration_document import converter

# Images never change at a given URL, but sit behind auth, so only the browser may cache them.
IMAGE_CACHE_CONTROL = "private, max-age=31536000, immutable"


def admin_guard(connection: ASGIConnection, _: BaseRouteHandler) -> None:
    """Allow only requests carrying the admin bearer token."""
    expected = connection.app.state.get("admin_token")
    if not expected:
        raise ServiceUnavailableException("the admin API is disabled: ADMIN_TOKEN is not set")
    scheme, _, token = connection.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise NotAuthorizedException("an admin bearer token is required", headers={"WWW-Authenticate": "Bearer"})
    if not hmac.compare_digest(token.strip().encode(), expected.encode()):
        raise PermissionDeniedException("the admin token is not valid")


def annotation_error_handler(_: Request, error: AnnotationError) -> Response:
    """Report service errors as JSON with a matching status code."""
    content: dict[str, Any] = {"detail": str(error)}
    if isinstance(error, NotFound):
        status = 404
    elif isinstance(error, Conflict):
        status = 412
        content["current_revision"] = error.current_revision
    elif isinstance(error, Invalid):
        status = 422
        content["problems"] = error.problems
    else:
        status = 400
    return Response(content=content, status_code=status)


def expected_revision(request: Request) -> int | None:
    """The revision in an `If-Match` header, if any (quoted or bare, e.g. `"42"`)."""
    header = request.headers.get("if-match")
    if header is None:
        return None
    try:
        return int(header.strip().removeprefix("W/").strip('"'))
    except ValueError:
        raise ClientException(f"If-Match must be a draft revision, got {header!r}") from None


def _field(data: Any, name: str, kind: type | tuple[type, ...], required: bool = True) -> Any:
    if not isinstance(data, dict):
        raise Invalid("the request body must be a JSON object")
    value = data.get(name)
    if value is None and not required:
        return None
    # bool is an int subclass; never accept it as a number
    if value is None or isinstance(value, bool) or not isinstance(value, kind):
        raise Invalid(f"`{name}` is required and must be {'a number' if kind is not str else 'a string'}")
    return value


def _number(data: Any, name: str, required: bool = True) -> float | None:
    value = _field(data, name, (int, float), required)
    return None if value is None else float(value)


def _integer(data: Any, name: str) -> int:
    value = _field(data, name, (int, float))
    if value != int(value):
        raise Invalid(f"`{name}` must be a whole number")
    return int(value)


def draft_response(state: DraftState, status_code: int = 200, **extra: Any) -> Response:
    content = {**converter.unstructure(state), **extra}
    return Response(content=content, status_code=status_code, headers={"ETag": f'"{state.document.revision}"'})


def _service(state: State) -> AnnotationService:
    return state.annotations


class AdminGeoregistrationController(Controller):
    """Edit, check and publish the georegistration annotations.

    Expects `state.annotations` (AnnotationService) and `state.admin_token`.
    """

    path = "/admin/georegistration"
    guards = [admin_guard]
    exception_handlers = {AnnotationError: annotation_error_handler}

    # --- documents

    @get("/draft")
    async def get_draft(self, state: State) -> Response:
        return draft_response(await _service(state).draft())

    @get("/published")
    async def get_published(self, state: State) -> Response:
        document = await _service(state).published()
        return Response(content=converter.unstructure(document), headers={"ETag": f'"{document.revision}"'})

    @post("/draft/discard", status_code=200)
    async def discard(self, request: Request, state: State) -> Response:
        return draft_response(await _service(state).discard_draft(expected_revision(request)))

    @get("/draft/fit")
    async def fit(self, state: State) -> dict[str, Any]:
        return converter.unstructure(await _service(state).fit())

    # --- points

    @post("/draft/points")
    async def create_point(self, request: Request, state: State, data: JSONBody[Any]) -> Response:
        draft, key = await _service(state).create_point(
            _field(data, "name", str), _number(data, "latitude"), _number(data, "longitude"), expected_revision(request)
        )
        return draft_response(draft, status_code=201, key=key)

    @put("/draft/points/{key:str}")
    async def update_point(self, request: Request, state: State, key: FromPath[str], data: JSONBody[Any]) -> Response:
        draft = await _service(state).update_point(
            key, _field(data, "name", str), _number(data, "latitude"), _number(data, "longitude"), expected_revision(request)
        )
        return draft_response(draft)

    @delete("/draft/points/{key:str}", status_code=200)
    async def delete_point(self, request: Request, state: State, key: FromPath[str]) -> Response:
        draft, removed = await _service(state).delete_point(key, expected_revision(request))
        return draft_response(draft, removed_annotations=removed)

    # --- annotations, images, thresholds

    @put("/draft/maptypes/{maptype:str}/images/{image:str}/annotations/{point:str}")
    async def put_annotation(
        self, request: Request, state: State, maptype: FromPath[str], image: FromPath[str], point: FromPath[str], data: JSONBody[Any]
    ) -> Response:
        draft = await _service(state).put_annotation(
            maptype,
            image,
            point,
            _integer(data, "pixel_x"),
            _integer(data, "pixel_y"),
            replaces=_field(data, "replaces", str, required=False),
            expected_revision=expected_revision(request),
        )
        return draft_response(draft)

    @delete("/draft/maptypes/{maptype:str}/images/{image:str}/annotations/{point:str}", status_code=200)
    async def delete_annotation(
        self, request: Request, state: State, maptype: FromPath[str], image: FromPath[str], point: FromPath[str]
    ) -> Response:
        return draft_response(await _service(state).delete_annotation(maptype, image, point, expected_revision(request)))

    @delete("/draft/maptypes/{maptype:str}/images/{image:str}", status_code=200)
    async def delete_image(self, request: Request, state: State, maptype: FromPath[str], image: FromPath[str]) -> Response:
        return draft_response(await _service(state).delete_image(maptype, image, expected_revision(request)))

    @put("/draft/maptypes/{maptype:str}/fit-threshold")
    async def set_fit_threshold(self, request: Request, state: State, maptype: FromPath[str], data: JSONBody[Any]) -> Response:
        meters = _number(data, "meters", required=False)
        return draft_response(await _service(state).set_fit_threshold(maptype, meters, expected_revision(request)))

    @get("/images/{maptype:str}/{image:str}")
    async def image(self, request: Request, state: State, maptype: FromPath[str], image: FromPath[str]) -> Response:
        stored = await _service(state).image(maptype, image, request.headers.get("if-none-match"))
        headers = {"Cache-Control": IMAGE_CACHE_CONTROL}
        if stored.etag:
            headers["ETag"] = stored.etag
        if stored.not_modified:
            return Response(content=None, status_code=304, headers=headers)
        if stored.content_length is not None:
            headers["Content-Length"] = str(stored.content_length)
        return Stream(stored.body, media_type=stored.content_type or "image/gif", headers=headers)

    # --- publishing and history

    @post("/publish", status_code=200)
    async def publish(self, state: State, data: JSONBody[Any]) -> dict[str, Any]:
        result = await _service(state).publish(_integer(data, "revision"), _field(data, "note", str, required=False))
        return converter.unstructure(result)

    @get("/history")
    async def history(self, state: State) -> list[dict[str, Any]]:
        return converter.unstructure(await _service(state).history())

    @get("/history/{history_id:str}")
    async def history_entry(self, state: State, history_id: FromPath[str]) -> dict[str, Any]:
        return converter.unstructure(await _service(state).history_entry(history_id))

    @post("/history/{history_id:str}/restore", status_code=200)
    async def restore(self, request: Request, state: State, history_id: FromPath[str]) -> Response:
        return draft_response(await _service(state).restore(history_id, expected_revision(request)))
