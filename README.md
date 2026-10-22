# upslope

Georegistered mountain weather forecasts. upslope downloads the University of
Washington WRF forecast plots every hour, warps them onto a real map projection
using hand-placed control points, and serves them as an animated overlay on a
Mapbox map.

The core of the project is the **Python backend** in `python/`. The Vue webapp in
`webapp/` is a thin client over it.

## How it works

```
UW WRF plots (GIF)          annotations in S3 (control points)
        │                              │
        ▼                              ▼
  upslope export-web ── fit + warp to Web Mercator ── PNG frames + manifest.json ──► S3 / Tigris
                                                                                       │
  upslope serve (Litestar) ◄───────────────────────────────────────────────────────────┘
    ├── /api/forecast   latest manifest and frames
    ├── /admin/...      annotation editing API (bearer token)
    ├── /health
    └── /               built webapp (/map, /annotation)
```

- **Export pipeline** (`upslope/models/uw_wrf/`): downloads each configured layer,
  fits a pixel → Lambert-conformal transform per map type from the published
  control points, reprojects every frame to EPSG:3857, and uploads PNGs plus a
  `manifest.json`.
- **Server** (`upslope/server/`): one Litestar process that serves the API and
  webapp and also runs the export at the top of every hour.
- **Annotations** (`upslope/annotation_service.py`, `upslope/clients/annotations.py`):
  a versioned draft/publish workflow stored in S3, edited through the admin API
  and the webapp's `/annotation` page.

Details on georegistration, storage layout, the admin API and the manifest format
are in [`python/README.md`](python/README.md). The webapp is documented in
[`webapp/README.md`](webapp/README.md).

## Repository layout

| Path | Contents |
| --- | --- |
| `python/` | The `upslope` package: CLI, export pipeline, server, tests (uv project) |
| `data/wrf_codes.yaml` | Which WRF domains, map types and variables to export |
| `dev/georegistration/` | Annotation snapshot for tests and the local stack (never deployed) |
| `webapp/` | Vite + Vue 3 + TypeScript front end |
| `justfile` | Every common task; run `just` to list them |
| `Dockerfile`, `compose.yaml`, `fly.toml` | Image, local stack, and Fly.io deployment |

## Getting started

Requirements: [uv](https://docs.astral.sh/uv/), Python 3.12, [just](https://just.systems/),
Docker (for the local stack), and Node 24 (for the webapp).

```sh
cp .env.example .env.local   # fill in what you need; just loads it for every recipe
just py-sync                 # install the Python environment
just py-test                 # run the backend tests (no network or credentials needed)
```

### Run the full stack locally

```sh
just up        # MinIO + server; seeds annotations from dev/georegistration and exports at startup
               # map: http://localhost:8000/map   admin token: upslope-local-admin
just logs      # follow logs
just reset     # stop and wipe MinIO so the next `up` starts fresh
```

### Run pieces directly

```sh
cd python
uv run upslope --help
uv run upslope download                            # raw GIFs into forecast/raw/
uv run upslope reproject --snapshot ../dev/georegistration
uv run upslope registration-error --snapshot ../dev/georegistration
just py-export                                     # full export against the bucket in .env.local
just py-serve                                      # server against the bucket in .env.local
```

For webapp development, run `just web-dev` alongside the local stack.

## Configuration

All settings come from the environment (see [`.env.example`](.env.example)):

| Variable | Used for |
| --- | --- |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION`, `BUCKET_NAME` | S3/Tigris storage for exports and annotations |
| `ADMIN_TOKEN` | Bearer token for `/admin` and the annotation page; unset disables them |
| `VITE_MAPBOX_TOKEN` | Public Mapbox token, baked into the webapp at build time |
| `UPSLOPE_WEBAPP_DIST` | Where the server finds the built webapp (set in the Docker image) |

## Testing

```sh
just test            # backend tests + webapp typecheck
just py-test -k admin  # pass pytest arguments through
```

Backend tests use an in-memory S3 stand-in (`python/tests/conftest.py`) and the
`dev/georegistration` snapshot, so they run offline in a few seconds.

## Deployment

Production runs on Fly.io as a single always-on machine, which matters: the
server runs the hourly export itself and serializes annotation edits with an
in-process lock.

```sh
fly storage create                # once: provisions Tigris and sets the AWS_* secrets
fly secrets set ADMIN_TOKEN=...   # once
just deploy
```
