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
| `/map/:model/:layer`              | `map-layer`        | Same map, deep-linked (`?hour=&opacity=&run=`) |
| `/about`                          | `about`            | Project description and credits               |
| anything else                     | `not-found`        | 404                                           |

The dropdowns drive the router, so the current selection is always reflected in
the URL and can be shared.

The **Annotation** nav link appears only while an admin token is stored; the
page itself still works by URL and asks for the token. The gear button in the
navbar opens **Settings** (`src/settings/`): log in or out (store or clear the
token), set the playback speed, turn the map's 3D terrain on or off and pick a
default model for the map. Settings are kept in `localStorage`.

The dot beside the gear (blue while a run is coming in, amber if a layer has
stalled, red on errors, from `/fx/status` refreshed every minute,
`src/forecast/status.ts`) opens `RunStatusDialog` from any page: a collapsible
section per model, all collapsed when it opens, each with a summary badge in
its header; expanded, it shows the latest complete run, the newest run with a
progress bar and state per layer, links to preview it, and every kept run.

## Where the data comes from

Annotations come from the server's admin API (`/admin/georegistration`; see
`python/README.md`). The dev server proxies `/admin` to `localhost:8000`, so run
the local stack (`just up`, admin token `upslope-local-admin`) alongside
`npm run dev`.

The forecast status comes from the server (`/fx/status`; see `python/README.md`),
which the dev server also proxies to `localhost:8000`. Manifests and frames load
straight from the public bucket, at the status's `data_url`.

## Forecast map

`/map` overlays `upslope export-web` output on a Mapbox GL map. It needs a
token in the repo-root `.env.local` (gitignored; see `.env.example`), which
`just` loads into the environment for `just web-dev` and `just web-build`:

```sh
VITE_MAPBOX_TOKEN=pk....
# VITE_FORECAST_BASE_URL=https://forecast-api.example  # defaults to /fx
```

The page fetches `<base>/status`, which lists the variables (layer ids shared
across models, from the server's config) and each model's layers, then the
selected model's run manifest (`<data_url>/<model>/<run>/manifest.json`), and
the other models' in the background so switching is instant. The controls pick
a **Variable** (under its group heading) and then a **Model** among those that
have it, each labeled with its run; ones with no frames in the run shown are
disabled. Switching to a variable the current model lacks switches to the
viewer's default model (set in Settings), else the config's `default_model`,
else the first model that has it; `/map` with no model opens on the same
default. Switching models keeps the valid time, not the forecast hour, since
models' runs start at different times. `/map/<model>/<variable>` URLs are
unchanged. By default each model's latest complete run is shown. `?run=2026-10-29T00Z`
shows that run instead (any run kept in storage, a week by default), with a
banner linking back to the latest; an unknown run lists the available ones.
`?run=in-progress` previews each model's newest run while it's still being
published: only the frames exported so far are shown (the controls say e.g.
"41 of 45 frames"), and the page reloads the manifests every 3 minutes to pick
up new ones.

The basemap is Mapbox's
`satellite-streets-v12`. Frames are drawn as a Mapbox `image` source from their
four corner coordinates, beneath the basemap's labels, with a Mercator
projection to match the EPSG:3857 registration, with a burnt-orange outline
around the frames' data (the manifest's `extent`: the source grid's edges in
its native projection; the image's corners for layers exported before it). The **3D terrain** setting adds
Mapbox's terrain DEM (1.5× exaggeration), drapes the basemap and forecast over
it, and tilts the camera; ctrl-drag or right-drag to tilt and rotate. All of a layer's
frames are preloaded; playback advances one frame every 2 s by default
(0.25–4 s, set in Settings) and skips frames that are still loading. Space
plays/pauses, ←/→ step. The controls show each frame's valid time (the layer's
`initialized` time plus the frame's hour) in Pacific time, with the offset and
the run time (UTC) alongside.

Layers with a `legend` in their manifest (colors chosen by the export, not an
upstream image) show it bottom left (top left on phones), with the model's
`attribution`. Layers with value files (`values`; see `python/README.md`) show
the value under the cursor in a tooltip, or under a tap on touch screens
(`src/forecast/frameValues.ts`). geotiff.js decodes the value files; it loads in
the background once the page has loaded and the browser is idle
(`src/forecast/valueDecoder.ts`), so it doesn't slow the page but is ready for
the first hover. A layer's lookup file is fetched as the layer is shown, and the
current frame's value file the first time the cursor is over it; the recent 64
are kept for playback. The value is interpolated between the model's grid cells
around the cursor, as the image's colors are. **Annotate this frame** appears only
for layers with an annotation maptype (UW WRF).

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
  selection.ts                 Which variable and model to show; the pickers' options
  usePlayback.ts               Frame selection, playback, preloading
  components/                  ForecastMap (Mapbox), MapControls, RunStatusDialog
src/admin/
  token.ts                     Admin token (localStorage) and prompt state
  api.ts                       Authenticated requests; 401/403/412 handling
  TokenDialog.vue              Token prompt
src/settings/
  settings.ts                  Viewer preferences (localStorage)
  SettingsDialog.vue           Login/logout, playback speed and default model
src/annotation/
  types.ts                     Admin API document and dialog payload types
  api.ts                       /admin/georegistration client
  store.ts                     The shared draft, as last returned by the server
  useBootstrapModal.ts         Bootstrap modal bound to a reactive flag
  components/                  ImageStage, tables, annotate/promote/publish/history dialogs
```
