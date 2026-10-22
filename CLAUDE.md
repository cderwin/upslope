# CLAUDE.md

Guidance for AI coding agents working in this repo. Read `README.md` for the
overview; `python/README.md` is the reference for georegistration, storage
layout, the admin API and the manifest format.

## Priorities

1. **The Python backend (`python/upslope/`) is the core.** Most work lands there.
   The webapp (`webapp/`) is a thin client: keep business logic and validation on
   the server.
2. Keep changes small and focused. Don't refactor, rename or reformat code you
   weren't asked to touch.
3. Verify before reporting done: run the tests (below) and say what you ran.

## Commands

Run from the repo root with `just`, or from `python/` with `uv`. Never use bare
`pip` or `python`; the environment is managed by uv.

```sh
just py-test                  # all backend tests (~5s, offline)
just py-test -k annotation    # subset; args pass through to pytest
just test                     # backend tests + webapp typecheck
cd python && uv run upslope --help
cd python && uv add <pkg>     # add a dependency (updates pyproject.toml and uv.lock)
```

`just py-export` / `just py-serve` / `just deploy` talk to real storage or
production. Don't run them unless asked. `just up` runs everything locally
against MinIO and is safe.

## Backend map

| Path | Role |
| --- | --- |
| `cli/` | Click commands; `cli/main.py` registers them. `cli/__init__.py` holds repo paths (`PROJECT_BASE`, `DEFAULT_WRF_CONFIG`, `DEV_GEOREGISTRATION`) |
| `server/app.py` | `create_app()`: Litestar app, lifespan, hourly job wiring |
| `server/admin.py` | Admin API controller; maps `AnnotationError` subclasses to HTTP status codes |
| `server/forecast.py`, `server/frontend.py`, `server/jobs.py` | Forecast proxy, webapp serving, hourly export loop |
| `annotation_service.py` | All annotation edits/publishing (async, locked, conditional writes) |
| `clients/storage.py` | `StorageClient` over S3/Tigris; conditional writes via ETags |
| `clients/annotations.py` | `AnnotationStore`: draft/published/history/images keys in the bucket |
| `models/uw_wrf/` | WRF download (`wrf_client`), fit + reprojection (`georegistration`), document schema, export `driver` |
| `models/manifest.py` | Web export manifest schema |
| `logging.py` | `get_logger`, `configure_logging`, `log_duration` |

`models/openmeteo/` is an empty placeholder. `dash`, `plotly` and friends in
`pyproject.toml` are leftovers from an earlier UI; don't build on them.

## Python conventions

Match the surrounding code. Specifically:

- Python 3.12+, full type hints, `X | None` not `Optional`, `collections.abc` for
  abstract types, `Self` where it fits.
- Data types are `attrs` `@define` classes, serialized with `cattrs` converters
  (see `georegistration_document.converter`). Don't introduce pydantic or
  dataclasses.
- Google-style docstrings with `Args:` / `Returns:` / `Attributes:` on public
  functions and classes. Module docstrings say what the module is for.
- Comments explain *why* (constraints, non-obvious choices), not what.
- Logging: `logger = get_logger(__name__)` at module top; %-style args
  (`logger.info("exported %d layers", n)`), not f-strings. No `print` outside CLI
  output. Wrap slow steps in `log_duration`.
- Lines up to ~120 characters. No formatter is configured; don't mass-reformat.
- CLI errors raise `click.ClickException`. CLI modules import heavy dependencies
  inside functions so `upslope --help` stays fast.

## Backend invariants (don't break these)

- **Async server, blocking libraries.** boto3, rasterio, httpx-sync and PIL work
  is blocking: call it through `asyncio.to_thread` from async code, as
  `annotation_service.py` and `server/jobs.py` do.
- **Single machine.** Annotation edits are serialized by an in-process
  `asyncio.Lock`, and the hourly export runs inside the server. Fly runs exactly
  one machine (`--ha=false`). Don't add designs that assume multiple workers.
- **Conditional writes.** Draft saves use ETag `If-Match` / `If-None-Match` so a
  concurrent writer is never silently overwritten. Every draft change increments
  `revision`; the admin API exposes it as the `ETag` and honours `If-Match`.
- **Append-only history.** `georegistration/history/*` and
  `georegistration/images/*` are never changed or deleted.
- **Admin errors.** Raise `NotFound`, `Invalid`, `Conflict` or `UpstreamError`
  from the service; `server/admin.py` turns them into 404/422/412/502. These are
  deliberately not logged as server errors.
- **Production never reads `dev/georegistration/`.** It's for tests and the
  local stack only. Commands read storage unless given `--snapshot`.
- **Config at startup.** `create_app`'s lifespan builds storage eagerly so
  missing credentials fail at boot, not on first request.

## Testing

- Tests live in `python/tests/`, one file per module/area. Add or update tests
  with every behavior change; a bug fix gets a test that fails without it.
- Use the `memory_storage` fixture (`tests/conftest.py`), an in-memory S3 with
  real conditional-write semantics, instead of mocking boto3.
- Server tests: `create_app(storage=memory_storage, webapp_dist=tmp_path,
  run_jobs=False, admin_token="secret")` with `litestar.testing.TestClient`.
- Tests must not touch the network or real credentials. Load fixtures from
  `DEV_GEOREGISTRATION`.

## Docs and commits

- Changing a CLI command, an admin route, the storage layout or the manifest
  format? Update `python/README.md` in the same change. Webapp routes and
  behavior go in `webapp/README.md`.
- Commit messages: imperative, sentence case, describing the change
  (e.g. "Add conditional writes and key listing to StorageClient"). One logical
  change per commit. Only commit or push when asked.

## Secrets and safety

- Secrets live in the repo-root `.env.local` (gitignored). Never read it into
  output, commit it, or copy values into code, tests or docs. New settings go in
  `.env.example` with a comment and an empty value.
- Credentials in `compose.yaml` are local-only MinIO values; keep it that way.
- Don't write to the production bucket or deploy without explicit instruction.

## Webapp (secondary)

Vue 3 + TypeScript + Vite. Typecheck with `just web-typecheck`. The annotation
store treats the server's response as the source of truth: every change is a
request and the returned document replaces local state. Keep it that way rather
than adding client-side edits.
