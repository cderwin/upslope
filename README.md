# upslope

Georegistered mountain weather forecasts. upslope downloads the University of
Washington WRF forecast plots as they're published, warps them onto a real map projection
using hand-placed control points, and serves them as an animated overlay on a
Mapbox map.

The core of the project is the **Python backend** in `python/`. The Vue webapp in
`webapp/` is a thin client over it.

## How it works

```
UW WRF plots (GIF)          annotations in S3 (control points)
        │                              │
        ▼                              ▼
  upslope export-loop ─ fit + warp to Web Mercator ── PNG frames + manifest.json ──► public bucket
                                                                                       │      │
  upslope serve (Litestar) ◄── index.json, export-status.json ─────────────────────────┘      │
    ├── /fx/status      each model's latest and retained runs, the exporter's health and      │
    │                   data_url, where the browser loads manifests and frames from ◄─────────┘
    ├── /admin/...      annotation editing API (bearer token)
    ├── /health
    └── /               built webapp (/map, /annotation)
```

- **Export pipeline** (`upslope/models/`): downloads each configured layer,
  fits a pixel → Lambert-conformal transform per map type from the published
  control points, reprojects every frame to EPSG:3857, and uploads PNGs plus a
  `manifest.json`.
- **Server** (`upslope/server/`): a Litestar process that serves the API and webapp.
- **Exporter** (`upslope/export_loop.py`, `upslope export-loop`): a separate
  process, on its own Fly machine, that checks every model for new frames every
  15 minutes, following runs as they're published. It shares only the bucket
  with the server, and records its health in `forecasts/export-status.json`,
  which `/fx/status` reports.
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
| `python/config/models.yaml` | Forecast models and the image layers to export from each |
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
uv run upslope registration-error --snapshot ../dev/georegistration
just py-export                                     # full export against the bucket in .env.local
just py-serve                                      # server against the bucket in .env.local
```

For webapp development, run `just web-dev` alongside the local stack.

## Configuration

All settings come from the environment (see [`.env.example`](.env.example)):

| Variable | Used for |
| --- | --- |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION`, `BUCKET_NAME` | S3/Tigris storage; `BUCKET_NAME` is the private bucket, for annotations |
| `PUBLIC_BUCKET_NAME` | The publicly readable bucket the exporter writes forecasts to (required) |
| `PUBLIC_DATA_URL` | Where browsers load forecast files from: the public bucket's `forecasts/` (required) |
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

Production runs on Fly.io: one always-on web machine (it serializes annotation
edits with an in-process lock) and one exporter machine (`fly.toml`).

```sh
fly storage create                # once: provisions Tigris and sets the AWS_* secrets
fly secrets set ADMIN_TOKEN=...   # once
# once: the public forecasts bucket (public read, CORS for GET/HEAD from the app's origin; see python/README.md)
fly secrets set PUBLIC_BUCKET_NAME=upslope-data-public PUBLIC_DATA_URL=https://upslope-data-public.t3.tigrisfiles.io/forecasts
```

**Merging to `main` deploys.** GitHub Actions (`.github/workflows/`) runs:

| Workflow | When | What |
| --- | --- | --- |
| `ci.yml` | Every pull request | Backend tests and webapp typecheck (same as `just test`) |
| `deploy.yml` | Push to `main`, or run by hand from the Actions tab | `ci.yml`, then `flyctl deploy --remote-only --ha=false` |

Deploys run one at a time and are never cancelled midway. They need two
repository secrets (Settings → Secrets and variables → Actions):

- `FLY_API_TOKEN`: a deploy token scoped to the app, from `fly tokens create deploy -a upslope`
- `VITE_MAPBOX_TOKEN`: the public Mapbox token, baked into the webapp at build time

To deploy by hand from your machine (for example if Actions is down), run
`just deploy`, which uses `VITE_MAPBOX_TOKEN` from `.env.local`.
