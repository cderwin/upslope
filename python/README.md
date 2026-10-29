# upslope

Command-line tooling for downloading and georegistering UW WRF forecast images.

```sh
uv run upslope download            # forecast/raw/<domain>/<variable>/hourNN.gif
uv run upslope reproject           # forecast/georegistered/<domain>/<variable>/hourNN.tiff
uv run upslope export-web          # full pipeline; uploads PNGs + manifest.json to storage
uv run upslope registration-error  # per-maptype fit error, in meters (--draft, --snapshot DIR)
uv run upslope serve               # API server + hourly export
uv run upslope georegistration upload DIR    # load a snapshot into storage (draft + published)
uv run upslope georegistration snapshot DIR  # save the published annotations to a directory
```

`reproject` and `registration-error` read the annotations published in storage;
pass `--snapshot ../dev/georegistration` to use the repo's snapshot instead.

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
history. The export records the published revision in each model run's marker
and re-exports a run when the revision has changed, so a publish takes effect
at the next hourly export.

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
| `GET /sources`, `POST /draft/images` | Promote a forecast frame `{model, variable, hour}` from UW's latest run |
| `GET /images/{m}/{i}` | Image bytes |
| `GET /draft/fit` | Fit error per maptype, image and point |
| `POST /publish` | `{revision, note?}` -> `{history, warnings}` |
| `GET /history`, `GET /history/{id}`, `POST /history/{id}/restore` | Published versions; restore copies into the draft |

### Snapshot

`dev/georegistration/` in the repo is a snapshot (`document.json` plus
`images/`) for tests and the local stack, which uploads it into MinIO on first
start. Production never reads it.

## Web export

`export-web` runs the whole UW WRF pipeline (`WrfDriver`) in a temporary
directory: it downloads every configured layer, georegisters each frame,
converts the GeoTIFFs to PNGs (nodata becomes transparent), and uploads the
frames and `manifest.json` to `web_exports/<started timestamp>/` in the S3/Tigris
bucket. It reads storage settings from `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `BUCKET_NAME` and, optionally,
`AWS_REGION`. Put them in the repo-root `.env.local` (gitignored; see
`.env.example`); `just py-export` and `just py-serve` load it.

The export downloads the newest model run UW has finished publishing, from the
run's own directory (`wrfrt/data/<YYYYMMDDHH>/`; UW's `timeindep/` copy is
overwritten in place mid-run and mixes two runs). A run counts as finished once
the last scheduled hour of every configured layer exists. Each exported run gets
a marker in `model_runs/uwwrf/<YYYYMMDDTHHMM>Z.json` recording its manifest, the
published annotation revision and a fingerprint of the layer config
(`WrfConfig.fingerprint()`). A run is exported again when either has changed, so
a publish, or a deploy that changes `data/wrf_codes.yaml`, takes effect at the
next export; production also exports at startup (`serve --export-on-startup`).
The server serves the newest run's export.

```json
{
  "started": "2026-10-21T10:58:34",
  "finished": "2026-10-21T11:02:10",
  "layers": [
    {
      "model": "uwwrf_4km",
      "variable": "surface-temp",
      "initialized": "2026-10-21T12:00:00+00:00",
      "dimensions": [900, 900],
      "coordinates": [[lon, lat], ...],
      "frames": [{ "hour": 6, "path": "uwwrf_4km/surface-temp/hour_06.png" }, ...]
    }
  ]
}
```

`dimensions` is `[width, height]` in pixels. `coordinates` are the image's
top-left, top-right, bottom-right and bottom-left corners. Frame paths are
relative to the manifest.
