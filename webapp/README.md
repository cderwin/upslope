# upslope webapp

Vite + Vue 3 + TypeScript front end for upslope. The georegistration
page (a port of `georegister.html`, which in turn mirrors
`upslope/annotation.py`) is routed under `/annotation`; the forecast map is
under `/map`.

```sh
npm install
npm run dev        # http://localhost:5173/map
npm run build      # type-check, bundle, and copy the images into dist/
npm run preview    # serve the production build
npm run typecheck
```

## Routes

| Path                              | Name               | Purpose                                      |
| --------------------------------- | ------------------ | -------------------------------------------- |
| `/`                               | `home`             | Landing page                                  |
| `/annotation`                     | `annotation`       | Georegistration tool, first image selected    |
| `/annotation/:domain/:image`      | `annotation-image` | Same tool, deep-linked to one image           |
| `/map`                            | `map`              | Forecast map, first layer selected            |
| `/map/:domain/:variable`          | `map-layer`        | Same map, deep-linked (`?hour=&opacity=&terrain=1`) |
| anything else                     | `not-found`        | 404                                           |

The dropdowns drive the router, so the current selection is always reflected in
the URL and can be shared.

## Where the data comes from

The data lives outside this package, in the repository's `data/` and
`forecast/` trees. `plugins/dataMounts.ts` is a small Vite plugin that mounts
directories under URL prefixes, configured in `vite.config.ts`:

| Prefix                          | Directory                         | Notes                                     |
| ------------------------------- | --------------------------------- | ----------------------------------------- |
| `/data/georegistration/images`  | `data/georegistration/images`     | Copied into `dist/`; listed as `virtual:georegistration-manifest` |
| `/api/forecast`                 | `forecast/web_export`             | Served from disk only, never bundled      |

Every mount is served by both `vite dev` and `vite preview`, with a
path-traversal guard. A mount with `manifestModule` exposes its directory
listing as a virtual module, so the annotation dropdowns are generated from
what is actually on disk; the dev server reloads when files are added or
removed.

## Forecast map

`/map` overlays `upslope export-web` output on a Mapbox GL map. It needs a
token in `webapp/.env.local` (gitignored):

```sh
VITE_MAPBOX_TOKEN=pk....
# VITE_FORECAST_BASE_URL=https://forecast-api.example  # defaults to /api/forecast
```

The page fetches `<base>/manifest.json` at runtime and loads frames from the
paths it lists, so pointing `VITE_FORECAST_BASE_URL` at the forecast API is the
only change needed once it exists. The basemap is Mapbox's
`satellite-streets-v12`. Frames are drawn as a Mapbox `image` source from their
four corner coordinates, beneath the basemap's labels, with a Mercator
projection to match the EPSG:3857 registration. The **3D terrain** switch adds
Mapbox's terrain DEM (1.5× exaggeration), drapes the basemap and forecast over
it, and tilts the camera; ctrl-drag or right-drag to tilt and rotate. All of a layer's
frames are preloaded; playback advances one frame every 2 s and skips frames
that are still loading. Space plays/pauses, ←/→ step.

## State and the YAML format

`src/annotation/store.ts` holds the working document, seeded on first load from
the repo's `annotations.yaml` (inlined at build time) and persisted to
`localStorage` after that. Nothing is written back to disk — use **Export
annotations.yaml** to download the file, or **Import YAML** to load one.

`src/annotation/yaml.ts` serialises byte-for-byte like PyYAML's
`yaml.dump(doc, default_flow_style=False, sort_keys=False)`, so an exported file
can replace `annotations.yaml` directly. Its reader is a minimal
indentation-based parser covering only the subset that file uses, not general
YAML; if the schema grows, reach for `js-yaml` instead.

## Layout

```
plugins/dataMounts.ts          Vite plugin: data directory serving + manifests
src/router/                    Route table
src/views/                     One component per route
src/forecast/
  types.ts                     Forecast manifest types
  api.ts                       Manifest/frame URLs (VITE_FORECAST_BASE_URL)
  usePlayback.ts               Frame selection, playback, preloading
  components/                  ForecastMap (Mapbox), MapControls
src/annotation/
  types.ts                     Document and dialog payload types
  yaml.ts                      serialize / parse / normalize
  store.ts                     Reactive document + localStorage
  manifest.ts                  Domain/image lists and URLs
  useBootstrapModal.ts         Bootstrap modal bound to a reactive flag
  components/                  ImageStage, dialogs, tables
```
