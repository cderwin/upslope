# upslope webapp

Vite + Vue 3 + TypeScript front end for upslope. The georegistration
page, which edits annotations through the server's admin API, is routed under
`/annotation`; the forecast map is under `/map`, and `/` redirects there.

```sh
npm install
npm run dev        # http://localhost:5173/map
npm run build      # type-check and bundle
npm run preview    # serve the production build
npm run typecheck
```

## Routes

| Path                              | Name               | Purpose                                      |
| --------------------------------- | ------------------ | -------------------------------------------- |
| `/`                               | `home`             | Redirects to `/map`                           |
| `/annotation`                     | `annotation`       | Georegistration tool, first image selected    |
| `/annotation/:maptype/:image`     | `annotation-image` | Same tool, deep-linked to one image           |
| `/map`                            | `map`              | Forecast map, first layer selected            |
| `/map/:model/:layer`              | `map-layer`        | Same map, deep-linked (`?hour=&opacity=&terrain=1&run=`) |
| `/about`                          | `about`            | Project description and credits               |
| anything else                     | `not-found`        | 404                                           |

The dropdowns drive the router, so the current selection is always reflected in
the URL and can be shared.

The **Annotation** nav link appears only while an admin token is stored; the
page itself still works by URL and asks for the token. The gear button in the
navbar opens **Settings** (`src/settings/`): log in or out (store or clear the
token) and set the playback speed. Settings are kept in `localStorage`.

## Where the data comes from

Annotations come from the server's admin API (`/admin/georegistration`; see
`python/README.md`). The dev server proxies `/admin` to `localhost:8000`, so run
the local stack (`just up`, admin token `upslope-local-admin`) alongside
`npm run dev`.

Forecasts come from the server's forecast API (`/fx/status` and `/fx/data/...`;
see `python/README.md`), which the dev server also proxies to `localhost:8000`.

## Forecast map

`/map` overlays `upslope export-web` output on a Mapbox GL map. It needs a
token in the repo-root `.env.local` (gitignored; see `.env.example`), which
`just` loads into the environment for `just web-dev` and `just web-build`:

```sh
VITE_MAPBOX_TOKEN=pk....
# VITE_FORECAST_BASE_URL=https://forecast-api.example  # defaults to /fx
```

The page fetches `<base>/status`, then each model's run manifest
(`<base>/data/<model>/<run>/manifest.json`), and shows every layer of every
model in one list, labeled with the model and layer names from the server's
config. By default each model's latest complete run is shown. `?run=2026-10-29T00Z`
shows that run instead (any run kept in storage, a week by default), with a
banner linking back to the latest; an unknown run lists the available ones.
`?run=in-progress` previews each model's newest run while it's still being
published: only the frames exported so far are shown (the controls say e.g.
"41 of 45 frames"), and the page reloads the manifests every 3 minutes to pick
up new ones.

The **Model runs** button (its dot is blue while a run is coming in, amber if a
layer has stalled, red on errors) opens `RunStatusDialog`: each model's latest
complete run, its newest run with a progress bar and state per layer (from
`/fx/status`, refreshed every minute while open), links to preview it, and every
kept run. The basemap is Mapbox's
`satellite-streets-v12`. Frames are drawn as a Mapbox `image` source from their
four corner coordinates, beneath the basemap's labels, with a Mercator
projection to match the EPSG:3857 registration. The **3D terrain** switch adds
Mapbox's terrain DEM (1.5× exaggeration), drapes the basemap and forecast over
it, and tilts the camera; ctrl-drag or right-drag to tilt and rotate. All of a layer's
frames are preloaded; playback advances one frame every 2 s by default
(0.25–4 s, set in Settings) and skips frames that are still loading. Space
plays/pauses, ←/→ step. The controls show each frame's valid time (the layer's
`initialized` time plus the frame's hour) in Pacific time, with the offset and
the run time (UTC) alongside.

## Annotation page

The page asks for the admin token (kept in `localStorage` until you log out in
Settings) and loads the
shared draft. `src/annotation/store.ts` holds the draft as the server last
returned it: every change is a request, and the response replaces the whole
document, so the browser never edits it locally. Destructive changes (deleting a
point or image, discarding, restoring) send the draft revision and reload if
someone else changed the draft first.

- Click the image to annotate a pixel; click a marker to edit or remove it.
- **Add image** promotes a forecast frame (layer and hour) from the model's latest run;
  so does **Annotate this frame** on `/map` when a token is set.
- The fit card shows the maptype's mean error against its threshold, which can
  be changed there; the table shows each point's error.
- **Publish** shows every maptype's fit, takes an optional note, and reports
  threshold warnings. The next export check (every 15 minutes) uses the published annotations.
- **History** lists published versions and restores one into the draft;
  **Discard changes** resets the draft to what is published.

## Layout

```
src/router/                    Route table
src/views/                     One component per route
src/forecast/
  types.ts                     Forecast API types (status, run manifests) and map layers
  api.ts                       Forecast API (VITE_FORECAST_BASE_URL), ?run= parsing
  usePlayback.ts               Frame selection, playback, preloading
  components/                  ForecastMap (Mapbox), MapControls, RunStatusDialog
src/admin/
  token.ts                     Admin token (localStorage) and prompt state
  api.ts                       Authenticated requests; 401/403/412 handling
  TokenDialog.vue              Token prompt
src/settings/
  settings.ts                  Viewer preferences (localStorage)
  SettingsDialog.vue           Login/logout and playback speed
src/annotation/
  types.ts                     Admin API document and dialog payload types
  api.ts                       /admin/georegistration client
  store.ts                     The shared draft, as last returned by the server
  useBootstrapModal.ts         Bootstrap modal bound to a reactive flag
  components/                  ImageStage, tables, annotate/promote/publish/history dialogs
```
