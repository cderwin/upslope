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
| `/map/:model/:layer`              | `map-layer`        | Same map, deep-linked (`?hour=&opacity=&run=`); `?point=lat,lon` opens that point's forecast |
| `/soundings/:station?`            | `soundings`        | Latest sounding from a station (default: nearest Seattle) |
| `/about`                          | `about`            | Project description and credits               |
| anything else                     | `not-found`        | 404                                           |

The dropdowns drive the router, so the current selection is always reflected in
the URL and can be shared.

The **Annotation** nav link appears only while an admin token is stored; the
page itself still works by URL and asks for the token. The gear button in the
navbar opens **Settings** (`src/settings/`): log in or out (store or clear the
token), set the playback speed, turn the map's 3D terrain on or off and pick a
default model for the map. Settings are kept in `localStorage`, along with
whether the map's controls are collapsed.

On phones (narrower than Bootstrap's `sm`, 576px) there is no navbar: the map
fills the screen, and its ⋮ menu has **Model runs** (with the status dot) and
**Settings** instead. The other pages still work by URL.

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
straight from the public bucket, at the status's `data_url`. Point forecasts come
from `/fx/point`, which needs the server's archive cache (`ARCHIVE_CACHE_DIR`).

To develop against production's forecasts instead of the local stack, point the
proxy there (the server has no CORS, so the browser can't call it directly); it
only reads:

```nu
with-env {VITE_PROXY_TARGET: "https://upslope.fly.dev"} { just web-dev }
```

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
models' runs start at different times. Switching models or variables also keeps
the map where it is; it zooms to the new layer's domain only when that doesn't
cover the center of the view. `/map/<model>/<variable>` URLs are
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
it, but never tilts the camera: the map looks straight down until you ctrl-drag
or right-drag to tilt and rotate. All of a layer's
frames are preloaded; playback advances one frame every 2 s by default
(0.25–4 s, set in Settings) and skips frames that are still loading. Space
plays/pauses, ←/→ step, and [ / ] lower and raise the overlay's opacity in 10%
steps (also the −/+ buttons; it's not a slider, so it isn't mistaken for the
timeline). The controls show each frame's valid time (the layer's `initialized`
time plus the frame's hour) in Pacific time, with the offset alongside; the run
time (UTC) is in the model picker, and a run other than the latest gets a badge.
The button in the panel's top-left corner collapses it to the valid time and
playback buttons (remembered with the other settings), and the ⋮ beside the
variable picker holds extra actions, shown only when there are any (on phones,
always: it stands in for the navbar). On phones the panel docks at the bottom,
above the scale bar.

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
around the cursor, as the image's colors are. **Annotate this frame**, in the ⋮
menu, appears only to admins and for layers with an annotation maptype (UW WRF).

### Point forecasts

A click (or tap) on the map drops a pin and opens a small menu for that point:
its coordinates, the layer's value there, and **Point forecast**. The menu closes
with Esc, a click elsewhere, or by moving the map. **Point forecast** adds
`?point=lat,lon` to the URL (so the forecast can be shared, and Back closes it)
and opens a dialog with every archived model's forecast at the point, from
`/fx/point` (see `python/README.md`). Its header shows the point's elevation from
the server's elevation model; typing another refetches with `elevation=`, since
temperatures are adjusted to it.

Below it, one chart per variable compares every model (ECharts,
`src/pointForecast/charts.ts`): temperature, freezing level, precipitation and
snowfall totals, snow depth, wind speed, gusts and direction, cloud cover,
humidity, dew point, solar radiation and pressure (hourly amounts and wind
components can be turned on under **Charts**). The charts share their time axis
and hover: a crosshair at one time shows on all of them, with the hovered
chart's tooltip listing every model's value. Hovering a line, or a model's chip
in the controls, emphasizes that model. The controls pick the models, the time
window (24 h to all) and whether the hours between a model's steps are filled.
Nights are shaded (sunrise and sunset computed for the point, `sun.ts`), a line
marks now, and reference lines mark freezing and, on the freezing level, the
point's elevation. Each chart's expand button shows it on its own, much bigger,
with the controls still applying. Each model keeps its color everywhere
(`palette.ts`: a blue-leaning palette in a fixed, color-blind-checked order,
stepped for light and dark mode). GFS and RRFS report
a freezing level of 0 when it's below their terrain; the charts leave a gap
(issue #46).

A table under the charts lists each model's run, terrain height and adjustment,
and the models left out and why. The dialog, ECharts included, loads on first
use, so the map page doesn't download it. Heights and values show in the units chosen
in Settings (imperial by default; the server sends metric,
`src/pointForecast/units.ts`).

## Soundings

`/soundings` draws a skew-T log-P diagram of a station's most recent upper-air
sounding. Both come straight from the Iowa Environmental Mesonet, which allows
cross-origin requests: the station list from
`https://mesonet.agron.iastate.edu/geojson/network/RAOB.geojson` (stations whose
archive is still open) and the profile from `/json/raob.py?station=…&ts=…`, trying
the latest 00Z and 12Z launches and going back up to two days. `src/soundings/api.ts`
converts levels to the chart's units (wind from knots to m/s) and drops those
without a temperature and dewpoint.

The station picker searches by id, name and state and lists stations nearest
Seattle first, with distances in miles; the page opens on the nearest one.
Station elevation and heights on the chart are in feet; the chart draws
temperatures in °C and wind in knots.

The chart is [skewt-plus](https://www.npmjs.com/package/skewt-plus), which also draws
a parcel trace, LCL and cloud levels, moist adiabats and isohumes, and its own
controls. It's a plain script that sets `window.SkewT`; `src/soundings/skewt.ts`
loads it, types the parts used, switches its heights to feet and cleans up after
it. Press and drag on the chart to read a level's values.

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
  components/                  ForecastMap (Mapbox), MapControls, PlaybackButtons, RunStatusDialog
src/admin/
  token.ts                     Admin token (localStorage) and prompt state
  api.ts                       Authenticated requests; 401/403/412 handling
  TokenDialog.vue              Token prompt
src/soundings/
  api.ts                       IEM stations and soundings, unit conversion
  skewt.ts                     Typed wrapper over skewt-plus
  components/                  StationPicker (searchable combobox), SkewTChart
src/pointForecast/
  types.ts                     /fx/point response
  point.ts                     ?point= parsing
  units.ts                     Display units (imperial/metric) and formatting
  charts.ts                    Which charts, and each one's ECharts option
  palette.ts                   Model colors and the charts' theme (Bootstrap's colors)
  sun.ts                       Sunrise and sunset, for night shading
  components/                  PointMenu (the map click menu), PointForecastDialog, PointForecastControls,
                               ForecastChart
src/settings/
  settings.ts                  Viewer preferences (localStorage)
  SettingsDialog.vue           Login/logout, playback speed, units and default model
src/annotation/
  types.ts                     Admin API document and dialog payload types
  api.ts                       /admin/georegistration client
  store.ts                     The shared draft, as last returned by the server
  useBootstrapModal.ts         Bootstrap modal bound to a reactive flag
  components/                  ImageStage, tables, annotate/promote/publish/history dialogs
```
