# upslope

The exporter, server and command-line tooling: UW WRF images georegistered with
hand-placed annotations, and dynamical.org models rendered from gridded data.

```sh
uv run upslope check-layers        # check every configured layer's images exist upstream (--model, --run)
uv run upslope export-web          # one export check of every model (or -m MODEL; --no-frame-limit); uploads PNGs + manifest.json to storage
uv run upslope export-loop         # the exporter: a check every 15 min, forever (--interval, --run-immediately)
uv run upslope registration-error  # per-maptype fit error, in meters (--draft, --snapshot DIR)
uv run upslope serve               # API server and webapp (doesn't export)
uv run upslope georegistration upload DIR    # load a snapshot into storage (draft + published)
uv run upslope georegistration snapshot DIR  # save the published annotations to a directory
uv run upslope georegistration promote-examples  # draft an image for each configured maptype with none (--hour, --dry-run)
uv run upslope render-frame -m gfs_0.25 -l surface-temp --hour 24 out.png  # preview a dynamical.org frame
```

`registration-error` reads the annotations published in storage;
pass `--snapshot ../dev/georegistration` to use the repo's snapshot instead.

## Models config

`config/models.yaml` lists the variables layers can show, the forecast models
and, for each, the image layers to export (`upslope/models/config.py`):

```yaml
default_model: uwwrf_4km     # the map's default model (see below)

variables:                   # in the order the map lists them
  - {id: 3-hour-precip, name: 3-hour precipitation, group: Precipitation}

models:
  - id: uwwrf_4km            # in map URLs and storage keys; don't rename
    name: UW WRF 4km         # display only
    source: {kind: uw_wrf}   # where frames come from (the default)
    # attribution: ...       # optional credit shown with the model's frames
    start_times: [0, 12]     # UTC initialization hours
    retention_days: 7
    layers:
      - id: 3-hour-precip    # a variable; in map URLs (/map/uwwrf_4km/3-hour-precip); don't rename
        url: https://a.atmos.washington.edu/wrfrt/data/{init:%Y%m%d%H}/images_d3/wa_pcp3.{hour:02d}.0000.gif
        hours: {first: 6, last: 120}   # step defaults to 3, first to 0
        maptype: washington  # annotations to georegister with
```

A layer's `id` is a variable's, and it takes the variable's `name` (layers
don't set one). Layers with the same id, from any models, are one variable on
the map: the viewer picks a variable, under its `group` heading, and then a
model among those that have it. `default_model` is the model the map opens on,
and switches to when the viewer's model lacks the variable they pick, unless
they've chosen a default model of their own in the map's settings. Layers that
show different things (e.g. 3- and 6-hour precipitation) need different
variables. Renaming a variable changes no frames, so nothing is re-exported.

`url` takes exactly two fields: `{init}`, the run's initialization time (a UTC
datetime, so give it a strftime format), and `{hour}`, the forecast hour. The
file is validated when it loads (ids, templates, hour ranges, start times), so a
mistake fails at startup. After editing it, run `upslope check-layers` to request
every layer's every hour from the newest complete run.

### dynamical.org models

Models with `source: {kind: dynamical, dataset: <id>}` are rendered from a
gridded dataset in [dynamical.org's catalog](https://dynamical.org/catalog/)
instead of downloaded as images (`upslope/models/dynamical/`):

```yaml
  - id: gfs_0.25
    name: GFS 0.25°
    source: {kind: dynamical, dataset: noaa-gfs-forecast-virtual}
    region: [-127, 41.5, -113, 50]   # west, south, east, north: the area rendered
    attribution: NOAA NWS NCEP GFS data processed by dynamical.org ... (CC BY 4.0).
    start_times: [0, 6, 12, 18]
    layers:
      - id: 3-hour-precip
        field: {derive: change, variables: [total_precipitation_run_total_surface], hours: 3}
        scale: precip_in            # colors and display units (models/dynamical/scales.py)
        hours: {first: 3, last: 240}
```

`field` is a dataset variable, or a derivation: `wind_speed` from u and v
components, `change` (a run total's growth over `hours`, e.g. precipitation),
`rate_sum` (a rate averaged since the previous step, summed over `hours`) and
`freezing_level` (missing, zero or negative heights, at or below sea level, as
0). Values are converted from the variable's `units` to the scale's.

Each check looks the dataset's Icechunk repository up in the STAC catalog
(`https://stac.dynamical.org/<id>/collection.json`; store URLs are versioned, so
they're never hard-coded) and opens it once, as one snapshot. Virtual datasets
point at the upstream GRIB files in NOAA's and ECMWF's open-data buckets, which
are read anonymously and decoded with `gribberish`. A frame is the region's
window of the native grid (lat/lon or projected) at one forecast hour, plus a
two-cell margin, warped to Web Mercator (bilinear, upsampled to about 600 px
across for coarse grids) and colored with the scale; its values are exported as
the frame's value image. An hour counts as published once `temperature_2m` has
data over the region (unwritten chunks read as NaN), so frames of a run appear as
the model publishes them.

`upslope check-layers -m <model>` checks each layer's variables, hours and units
against the live dataset and lists hours not published yet;
`upslope render-frame` writes one frame as a PNG, without uploading it.

## Georegistration

The UW WRF plots are drawn on the model's native grid, a Lambert conformal conic
projection on a 6370 km sphere (WPS namelist: `truelat1=30`, `truelat2=60`,
`stand_lon=-121`; see `WRF_LAMBERT_CRS` in `georegistration.py`). Because each
plot is axis-aligned with that grid, the export fits only a scale and offset per
axis for each map type from the published annotations (pixel -> Lambert meters;
see below), then warps
every frame to Web Mercator (EPSG:3857) with nearest-neighbour resampling, so the
palette survives. Pixels outside the warped frame get an unused palette index as
nodata. Mercator is what Mapbox renders image overlays in, so a frame's four
corners place it exactly.

Each map type needs at least two control points; with three or more,
`registration-error` reports genuine residuals (geodesic meters at the control
points).

## Annotations

The control points live in the storage bucket under `georegistration/`, and are
edited through the admin API (the webapp's `/annotation` page):

| Key | Contents |
| --- | --- |
| `draft.json` | The shared working copy every edit goes to |
| `published.json` | What the export georegisters with |
| `history/<time>Z-r<rev>.json` | Every published version, with its note; never deleted |
| `images/<maptype>/<image>.gif` | Annotated images; never changed or deleted |

A document (`upslope/models/uw_wrf/georegistration_document.py`) holds landmarks
(`geographic_points`: key -> name, latitude, longitude) and, per maptype, its
images, each with at most one pixel annotation per landmark, plus a
`fit_threshold_m`. Every change to the draft increases its `revision`.
Publishing copies the draft to `published.json` (keeping its revision) and to
history. Each run manifest records the revision it was georegistered with, and
the export redoes the runs it follows when the revision changes, so a publish
takes effect at the next check (every 15 minutes).

`upslope/annotation_service.py` applies changes: they are serialized by a lock
(the server runs on one machine) and saved with S3 conditional writes, so a
write from another process is never lost. Publishing is refused if a maptype
can't be fitted (fewer than two control points), a configured layer's maptype
has no annotations, or an annotation names an unknown landmark. A maptype whose
mean fit error exceeds its threshold only produces a warning.

A new maptype in `config/models.yaml` therefore blocks publishing until it is
annotated. `upslope georegistration promote-examples` adds a frame (hour 6 by
default) from the first layer using each configured maptype that has no images
in the draft; annotate it on the `/annotation` page, then publish. Maptypes that
already have an image, annotated or not, are skipped.

### Admin API

`/admin/georegistration/...` on the server, behind `Authorization: Bearer
$ADMIN_TOKEN` (401 without a token, 403 with a wrong one, 503 if `ADMIN_TOKEN`
is unset). Changes return the new draft with its revision as the `ETag`, and
accept `If-Match: "<revision>"` to fail with 412 if the draft has moved on.

| Method and path | Purpose |
| --- | --- |
| `GET /draft` | The draft, with `published_revision` |
| `POST /draft/discard` | Reset the draft to the published document |
| `POST /draft/points`, `PUT`/`DELETE /draft/points/{key}` | Landmarks (delete also removes their annotations) |
| `PUT`/`DELETE /draft/maptypes/{m}/images/{i}/annotations/{point}` | A landmark's pixel on an image (`{pixel_x, pixel_y, replaces?}`) |
| `DELETE /draft/maptypes/{m}/images/{i}` | Remove an image from the draft |
| `PUT /draft/maptypes/{m}/fit-threshold` | `{meters}` (or `null`) |
| `GET /sources`, `POST /draft/images` | Promote a forecast frame `{model, variable, hour}` (model and layer ids) from the model's latest complete run |
| `GET /images/{m}/{i}` | Image bytes |
| `GET /draft/fit` | Fit error per maptype, image and point |
| `POST /publish` | `{revision, note?}` -> `{history, warnings}` |
| `GET /history`, `GET /history/{id}`, `POST /history/{id}/restore` | Published versions; restore copies into the draft |

### Snapshot

`dev/georegistration/` in the repo is a snapshot (`document.json` plus
`images/`) for tests and the local stack, which uploads it into MinIO on first
start. Production never reads it.

## Web export

`export-web` runs the whole pipeline (`ForecastDriver`) for each configured
model: the model's source (`upslope/models/source.py`; `models/uw_wrf/source.py`
and `models/dynamical/source.py`) renders each frame as a Web Mercator GeoTIFF
(for UW, by downloading the image and georegistering it), and the driver
converts it to a PNG (nodata becomes transparent) and uploads it to the
S3/Tigris bucket. A model exports at most 400 frames per check
(`upslope/export_loop.py`), so a backlog takes a few checks without holding up the others;
`export-web --no-frame-limit` exports all of it at once. It
reads storage settings from `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_ENDPOINT_URL_S3`, `BUCKET_NAME` (the private bucket, which annotations are
read from), `PUBLIC_BUCKET_NAME` (the public bucket forecasts are written to) and, optionally, `AWS_REGION`. Put them in
the repo-root `.env.local` (gitignored; see `.env.example`); `just py-export`,
`just py-export-loop` and `just py-serve` load it.

### The exporter

`export-loop` (`upslope/export_loop.py`) runs a check every 15 minutes, on the
clock (`--interval`), and also at startup with `--run-immediately`. It's its
own process, apart from the server: in production the `exporter` Fly process
group, on its own machine, so exporting never slows down requests (`fly.toml`).
Exactly one exporter may run, since two would race on manifests and retention;
deploys pass `--ha=false`. The two processes share only storage.

The pipeline is async. Every model is checked concurrently, each in its own
task, while a model's runs, layers and hours go in order. Blocking work
(reading datasets, georegistering, PNG encoding, storage) runs in worker
threads (16 of them), with at most 3 dynamical.org frames rendered (or latest
runs looked up) at once. Each dynamical.org dataset is opened under its own
lock, so a slow upstream holds up only the models that read it. Nothing stops
the loop:

- a model that fails is reported failed and the others carry on;
- a model that takes over 40 minutes is stopped, and a check over 45
  (`--check-timeout`). What they exported is kept, and the next check resumes;
- any other error is logged and recorded in the status.

Every request has a timeout:

- UW: 30 s, 10 s to connect;
- dynamical.org: 60 s per read and 180 s per operation, with Icechunk's own
  retries;
- storage (boto3): 60 s, with standard retries.

Requests to UW, a single modest server, go through a shared throttle: at most
2 at once, started at least 0.2 s apart. Throttle and retries are in
`models/uw_wrf/image_client.py`.

- 429, 5xx responses and connection errors are retried up to 4 tries, with
  exponential backoff (2 s, 4 s, 8 s, with jitter, capped at 30 s) and honoring
  `Retry-After`.
- A 429 or 503 also holds back every other UW request for the backoff.
- A 404 is never retried: it means the hour isn't published yet.

Log lines of a check are tagged `[check N]`, and a model's lines also `[<model
id>]`, so one check or model can be followed through the interleaved logs. A
check ends with a summary line: frames exported, failed and skipped models,
and frames to retry.

As each check starts and finishes, the exporter writes
`forecasts/export-status.json` (`ExportStatus` in `models/manifest.py`). The
server reports it at `/fx/status`:

```json
{
  "state": "failed",
  "check": 12,
  "started_at": "2026-11-08T14:00:00+00:00",
  "overdue_after": "2026-11-08T14:25:00+00:00",
  "finished_at": "2026-11-08T14:03:41+00:00",
  "next_check_at": "2026-11-08T14:15:00+00:00",
  "last_success_at": "2026-11-08T13:48:12+00:00",
  "consecutive_failures": 1,
  "error": null,
  "models": [
    { "id": "gfs_0.25", "state": "ok", "frames": 9, "error": null, "layer_errors": [], "seconds": 41.2 },
    { "id": "uwwrf_4km", "state": "failed", "frames": 0, "error": "HTTP 503", "layer_errors": [], "seconds": 102.5 }
  ]
}
```

- `state` is `running`, `ok`, or `failed` (the check as a whole, or some
  model).
- A model's `state` is `ok`, `skipped` (e.g. no annotations are published) or
  `failed`. Its `layer_errors` are frames that failed and are retried at the
  next check; they don't fail the model.
- `overdue_after` is when to suspect the exporter is stuck or down: the
  running check's time limit, or 10 minutes after the next check is due.

Each check follows every run of each model from its latest complete run onward, oldest
first, downloading from the run's own directory (for UW,
`wrfrt/data/<YYYYMMDDHH>/`; UW's `timeindep/` copy is overwritten in place
mid-run and mixes two runs). Each layer resumes after its newest exported hour
and downloads hours until one returns 404, which means it isn't published yet.
Any other failure (a timeout, a 5xx, an image that won't decode) is recorded on
the layer and retried at the next check. So frames of a run in progress appear
as UW publishes them, and a run becomes the latest once every layer has its
last hour. A model that fails outright (e.g. UW unreachable while looking for
runs) doesn't stop the others. Before any run is complete (a new deploy, or a
config whose hours UW doesn't publish), every run in the model's 48-hour lookback
is followed.

### Storage layout

Runs live under `forecasts/` in the public bucket (`PUBLIC_BUCKET_NAME`), laid
out like open-meteo's
[`data_spatial/`](https://github.com/open-meteo/open-data#data-organization).
Browsers read it directly, at `PUBLIC_DATA_URL`, so nothing else ever goes in it:
annotations stay in the private bucket (`BUCKET_NAME`), which only the server and
exporter read.

| Key | Contents |
| --- | --- |
| `forecasts/export-status.json` | The exporter's health: its last check's outcome, per model (above) |
| `forecasts/index.json` | Every model's `latest.json`, `in-progress.json` and retained runs in one object, for `/fx/status` (below) |
| `forecasts/<model>/latest.json` | The newest complete run: `{model, initialized, path, updated_at}` |
| `forecasts/<model>/in-progress.json` | The newest run that has started, and each layer's progress (below) |
| `forecasts/<model>/<YYYY>/<MM>/<DD>/<HHMM>Z/manifest.json` | The run's layers and frames (below) |
| `forecasts/<model>/<YYYY>/<MM>/<DD>/<HHMM>Z/<layer>/<YYYY-MM-DDTHHMM>-<digest>.png` | Frames, named by valid time (UTC) and content (and `...-<digest>.values.png` value images) |

Frames are named by the first 16 hex digits of their content's SHA-256, so a
re-exported frame gets a new name and a cached one is never stale. They're
uploaded with `Cache-Control: public, max-age=31536000, immutable`. The JSON
files are rewritten in place and uploaded with `Cache-Control: no-cache`, so
browsers revalidate them with their ETags every time. Frames a re-export
replaced stay until their run is deleted. (Frames exported before the names had
digests keep their old names, which nothing writes again.)

`index.json` (`ForecastIndex` in `models/manifest.py`) exists so the server
reads one object instead of listing every frame of every model to find its
runs. As each model's check finishes, the exporter re-reads that model's
pointers and runs from storage and rewrites the index if its entry changed, so
the index can't drift from the files; the per-model files stay the source of
truth.

A run's manifest is rewritten as its frames arrive, after the frames are
uploaded, and `latest.json` points at a run only once it's complete, so a reader
following the pointers never finds a missing file. `latest.json` never moves to
an older run.

`in-progress.json` is rewritten at every check, so it also shows when the
exporter last looked (`checked_at`), and keeps pointing at the newest run once
it's complete:

```json
{
  "model": "uwwrf_4km",
  "initialized": "2026-10-29T12:00:00+00:00",
  "path": "2026/10/29/1200Z",
  "checked_at": "2026-10-29T15:45:03+00:00",
  "layers": [
    { "id": "surface-temp", "last_hour": 120, "available_hour": 42,
      "progressed_at": "2026-10-29T15:30:11+00:00", "error": null, "error_at": null }
  ]
}
```

A run is exported again when its layers no longer match what the
config and annotations would produce. Each manifest layer records its
`fingerprint` (`LayerConfig.fingerprint()`: its URL template, hours and maptype,
not its name) and the manifest records the annotation revision. A publish
re-exports every layer; a config change re-exports only the layers it changed;
a renamed layer only rewrites the manifest. This applies to the latest run and
any newer ones being followed. Production also checks at startup
(`export-loop --run-immediately`), so a deploy that changes `config/models.yaml`
takes effect right away.

After each check, runs initialized more than the model's `retention_days`
(7 by default) ago are deleted, except the ones `latest.json` and
`in-progress.json` point at.
`georegistration/` is never touched.

#### The public bucket

The public bucket needs public read, and a CORS rule allowing `GET` and `HEAD`
from the app's origin (and `http://localhost:5173` for the dev server), exposing
`ETag`: the map fetches manifests and frames cross-origin. Locally, `just up`
creates an anonymously readable `upslope-data-public` bucket in MinIO.

### Run manifest

```json
{
  "model": "uwwrf_4km",
  "name": "UW WRF 4km",
  "initialized": "2026-10-29T00:00:00+00:00",
  "registration_revision": 12,
  "updated_at": "2026-10-29T06:02:10+00:00",
  "layers": [
    {
      "id": "surface-temp",
      "name": "Surface temperature",
      "maptype": "washington",
      "fingerprint": "3f9c0d6e1a2b4c5d",
      "hours": [0, 3, 6, ...],
      "dimensions": [958, 919],
      "coordinates": [[lon, lat], ...],
      "frames": [{ "hour": 6, "valid": "2026-10-29T06:00:00+00:00", "path": "surface-temp/2026-10-29T0600.png", "values": null }, ...],
      "legend": null,
      "values": null,
      "extent": [[lon, lat], ...]
    }
  ],
  "attribution": null
}
```

`hours` lists every hour the layer is published for. `dimensions` is
`[width, height]` in pixels. `coordinates` are the image's top-left, top-right,
bottom-right and bottom-left corners. `extent` outlines where the frames have
data: the source grid's edges (the UW image, or a dynamical.org dataset's
window) in their native projection, 16 points to an edge, clockwise from the
top-left, without the nodata the warp leaves around them; the map draws it. It's
missing from layers exported before it was recorded until their next frame.
Frame paths are relative to the run's
folder. `registration_revision` is null for models whose source has no
annotations.

Layers whose colors the export chooses (rather than an upstream image with its
own legend, like UW's) also have:

- `legend`: `{"units": "°F", "stops": [{"value": 32, "color": "#ffffff"}, ...], "decimals": 0}`;
  each color applies from its value up to the next stop's.
- `values`: `{"scale": 0.01, "offset": -100000}`, and each frame's `values`
  names a value image beside it (`surface-temp/2026-10-29T0600.values.png`): a
  lossless RGB PNG on the frame's pixel grid where `n = R*65536 + G*256 + B` is
  no data when 0, else the value `(n - 1) * scale + offset` in the legend's
  units. The map reads it to show the value under the cursor.

`attribution` is the model's `attribution` from the config (credit a license
requires), shown on the map.

### Forecast API

| Method and path | Purpose |
| --- | --- |
| `GET /fx/status` | Each configured model's `layers` (variable ids), `latest` run, `in_progress` run and retained `runs` (`{initialized, path}`); the `variables` (`{id, name, group}`) and `default_model` from the config; and the exporter's health (`export`, `export_overdue`), from memory; refreshed in the background every minute |

`data_url` is where the webapp loads manifests
(`<data_url>/<model>/<run path>/manifest.json`) and frames (their paths are
relative to the run's folder) from: `PUBLIC_DATA_URL`, the public bucket's
`forecasts/`. The server won't start without it.

`in_progress` adds `checked_at`, `complete` and, per configured layer, `name`,
`available_hour`, `last_hour`, `progressed_at`, `error` and a `state` the server
derives: `complete`; `waiting` for the next hour; `stalled` when an incomplete
layer has gained no frame for 2 hours (`STALL_AFTER`; a layer with no frames is
measured from the run's first frame); or `error` when the last attempt failed
(it's retried at the next check).

The server builds the status from `index.json` and `export-status.json`, which
a background task re-reads every 60 seconds with their ETags, so a request
never waits on storage and an unchanged file costs one small request. Until
the exporter first writes the index (in a new bucket), no runs are listed. With
the index written once per
model check, a running check's progress shows up when that model finishes.

`export` is `export-status.json` (see [The exporter](#the-exporter)), or null if
the exporter has never run. `export_overdue` is true once `export.overdue_after`
has passed without a newer status: the exporter is stuck or down.
