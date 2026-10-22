# upslope

Command-line tooling for downloading and georegistering UW WRF forecast images.

```sh
uv run upslope download            # forecast/raw/<domain>/<variable>/hourNN.gif
uv run upslope reproject           # forecast/georegistered/<domain>/<variable>/hourNN.tiff
uv run upslope export-web          # full pipeline; uploads PNGs + manifest.json to storage
uv run upslope registration-error  # per-domain control-point error, in meters
```

## Georegistration

The UW WRF plots are drawn on the model's native grid, a Lambert conformal conic
projection on a 6370 km sphere (WPS namelist: `truelat1=30`, `truelat2=60`,
`stand_lon=-121`; see `WRF_LAMBERT_CRS` in `georegistration.py`). Because each
plot is axis-aligned with that grid, `reproject` fits only a scale and offset per
axis for each map type from the control points in
`data/georegistration/annotations.yaml` (pixel -> Lambert meters), then warps
every frame to Web Mercator (EPSG:3857) with nearest-neighbour resampling, so the
palette survives. Pixels outside the warped frame get an unused palette index as
nodata. Mercator is what Mapbox renders image overlays in, so a frame's four
corners place it exactly.

Each map type needs at least two control points; with three or more,
`registration-error` reports genuine residuals (geodesic meters at the control
points).

## Web export

`export-web` runs the whole UW WRF pipeline (`WrfDriver`) in a temporary
directory: it downloads every configured layer, georegisters each frame,
converts the GeoTIFFs to PNGs (nodata becomes transparent), and uploads the
frames and `manifest.json` to `web_exports/<started timestamp>/` in the S3/Tigris
bucket. It reads storage settings from `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `BUCKET_NAME` and, optionally,
`AWS_REGION`.

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
