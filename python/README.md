# upslope

Command-line tooling for downloading and georegistering forecast map images
(currently UW WRF).

```sh
uv run upslope check-layers        # check every configured layer's images exist upstream (--model, --run)
uv run upslope download            # forecast/raw/<model>/<layer>/hourNN.gif (--model, --run)
uv run upslope reproject           # forecast/georegistered/<model>/<layer>/hourNN.tiff
uv run upslope export-web          # full pipeline; uploads PNGs + manifest.json to storage
uv run upslope registration-error  # per-maptype fit error, in meters (--draft, --snapshot DIR)
uv run upslope serve               # API server + export every 15 min (--export-interval)
uv run upslope georegistration upload DIR    # load a snapshot into storage (draft + published)
uv run upslope georegistration snapshot DIR  # save the published annotations to a directory
```

`reproject` and `registration-error` read the annotations published in storage;
pass `--snapshot ../dev/georegistration` to use the repo's snapshot instead.

## Models config

`config/models.yaml` lists the forecast models and, for each, the image layers
to export (`upslope/models/config.py`):

```yaml
models:
  - id: uwwrf_4km            # in map URLs and storage keys; don't rename
    name: UW WRF 4km         # display only
    start_times: [0, 12]     # UTC initialization hours
    retention_days: 7
    layers:
      - id: 3-hour-precip    # in map URLs (/map/uwwrf_4km/3-hour-precip); don't rename
        name: 3-hour precipitation
        url: https://a.atmos.washington.edu/wrfrt/data/{init:%Y%m%d%H}/images_d3/wa_pcp3.{hour:02d}.0000.gif
        hours: {first: 6, last: 120}   # step defaults to 3, first to 0
        maptype: washington  # annotations to georegister with
```

`url` takes exactly two fields: `{init}`, the run's initialization time (a UTC
datetime, so give it a strftime format), and `{hour}`, the forecast hour. The
file is validated when it loads (ids, templates, hour ranges, start times), so a
mistake fails at startup. After editing it, run `upslope check-layers` to request
every layer's every hour from the newest complete run.

## Georegistration

The UW WRF plots are drawn on the model's native grid, a Lambert conformal conic
projection on a 6370 km sphere (WPS namelist: `truelat1=30`, `truelat2=60`,
`stand_lon=-121`; see `WRF_LAMBERT_CRS` in `georegistration.py`). Because each
plot is axis-aligned with that grid, `reproject` fits only a scale and offset per
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

### Admin API

`/admin/georegistration/...` on the server, behind `Authorization: Bearer
$ADMIN_TOKEN` (401 without a token, 403 with a wrong one, 503 if `ADMIN_TOKEN`
is unset). Changes return the new draft with its revision as the `ETag`, and
accept `If-Match: "<revision>"` to fail with 412 if the draft has moved on.

| Method and path | Purpose |
| --- | --- |
| `GET /draft`, `GET /published` | Documents (the draft with `published_revision`) |
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
model: it downloads each layer's images, georegisters each frame, converts it to
a PNG (nodata becomes transparent) and uploads it to the S3/Tigris bucket. It
reads storage settings from `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_ENDPOINT_URL_S3`, `BUCKET_NAME` and, optionally, `AWS_REGION`. Put them in
the repo-root `.env.local` (gitignored; see `.env.example`); `just py-export` and
`just py-serve` load it.

The server runs the export every 15 minutes, on the clock (`serve
--export-interval`), and at startup with `--export-on-startup`. Each check
follows every run of each model from its latest complete run onward, oldest
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

Runs live under `forecasts/`, laid out like open-meteo's
[`data_spatial/`](https://github.com/open-meteo/open-data#data-organization):

| Key | Contents |
| --- | --- |
| `forecasts/<model>/latest.json` | The newest complete run: `{model, initialized, path, updated_at}` |
| `forecasts/<model>/in-progress.json` | The newest run that has started, and each layer's progress (below) |
| `forecasts/<model>/<YYYY>/<MM>/<DD>/<HHMM>Z/manifest.json` | The run's layers and frames (below) |
| `forecasts/<model>/<YYYY>/<MM>/<DD>/<HHMM>Z/<layer>/<YYYY-MM-DDTHHMM>.png` | Frames, named by valid time (UTC) |

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

A run is exported again, in place, when its layers no longer match what the
config and annotations would produce. Each manifest layer records its
`fingerprint` (`LayerConfig.fingerprint()`: its URL template, hours and maptype,
not its name) and the manifest records the annotation revision. A publish
re-exports every layer; a config change re-exports only the layers it changed;
a renamed layer only rewrites the manifest. This applies to the latest run and
any newer ones being followed. Production also exports at startup
(`serve --export-on-startup`), so a deploy that changes `config/models.yaml`
takes effect right away.

After each check, runs initialized more than the model's `retention_days`
(7 by default) ago are deleted, except the ones `latest.json` and
`in-progress.json` point at.
`georegistration/` is never touched. `upslope delete-legacy-exports` deletes the
layout used before `forecasts/` (`web_exports/` and `model_runs/`).

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
      "frames": [{ "hour": 6, "valid": "2026-10-29T06:00:00+00:00", "path": "surface-temp/2026-10-29T0600.png" }, ...]
    }
  ]
}
```

`hours` lists every hour the layer is published for. `dimensions` is
`[width, height]` in pixels. `coordinates` are the image's top-left, top-right,
bottom-right and bottom-left corners. Frame paths are relative to the run's
folder.

### Forecast API

| Method and path | Purpose |
| --- | --- |
| `GET /fx/status` | Each configured model's `latest` run, `in_progress` run and retained `runs` (`{initialized, path}`); refreshed after every check and at most a minute old |
| `GET /fx/data/{path}` | `forecasts/{path}` from storage: pointers and manifests (`no-cache`) and frames (`max-age=300`), with ETags |

`in_progress` adds `checked_at`, `complete` and, per configured layer, `name`,
`available_hour`, `last_hour`, `progressed_at`, `error` and a `state` the server
derives: `complete`; `waiting` for the next hour; `stalled` when an incomplete
layer has gained no frame for 2 hours (`STALL_AFTER`; a layer with no frames is
measured from the run's first frame); or `error` when the last attempt failed
(it's retried at the next check).
