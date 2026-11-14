/** `[longitude, latitude]` */
export type LngLat = [number, number]

// --- server responses

/** A model run and its folder: `<base>/data/<model>/<path>/manifest.json` (python `RunRef`). */
export interface RunRef {
  /** Initialization time (ISO 8601, UTC). */
  initialized: string
  /** e.g. `2026/10/29/0000Z` */
  path: string
}

export type LayerState = 'complete' | 'waiting' | 'stalled' | 'error'

/** How far a layer of the in-progress run has got (python `LayerStatus`). */
export interface LayerStatus {
  id: string
  name: string
  /** `waiting` for the next hour; `stalled` if none has arrived for a while; `error` if the last try failed. */
  state: LayerState
  /** Newest hour exported, or null before the first. */
  available_hour: number | null
  last_hour: number
  progressed_at: string | null
  error: string | null
}

/** The newest run that has started publishing (python `InProgressStatus`). */
export interface InProgressStatus {
  initialized: string
  path: string
  /** When the server last checked it for new hours. */
  checked_at: string
  complete: boolean
  layers: LayerStatus[]
}

/** python `ModelStatus`. */
export interface ModelStatus {
  id: string
  name: string
  /** Ids of the variables the model is configured with. */
  layers: string[]
  /** Newest complete run, if any has been exported. */
  latest: RunRef | null
  /** Newest run that has started, and how far it has got. */
  in_progress: InProgressStatus | null
  /** Retained runs with a manifest, newest first. */
  runs: RunRef[]
}

/** Something layers show, whichever model they come from (python `VariableConfig`). */
export interface Variable {
  /** The id of every layer showing it, e.g. `freezing-level`. */
  id: string
  name: string
  /** Heading it's listed under, e.g. `Snow`. */
  group: string | null
}

/** `GET <base>/status` (python `ForecastStatus`). */
export interface ForecastStatus {
  models: ModelStatus[]
  /** In display order. */
  variables: Variable[]
  /** Model to show when the viewer hasn't picked one, or theirs lacks a variable. */
  default_model: string | null
  /** Base URL of the forecast files (manifests and frames) in the public bucket. */
  data_url: string
}

/** python `Frame`. */
export interface RunFrame {
  hour: number
  /** Valid time (ISO 8601, UTC). */
  valid: string
  /** Relative to the run's folder. */
  path: string
  /** The frame's value file, relative to the run's folder (see `ValueEncoding`); absent or null without one. */
  values?: string | null
}

/** One color of a layer's scale (python `LegendStop`). */
export interface LegendStop {
  /** Value the color starts at, in the legend's units. */
  value: number
  /** CSS hex color. */
  color: string
}

/** How to read a layer's colors (python `Legend`); absent for images that carry their own (UW WRF). */
export interface Legend {
  /** e.g. `°F` */
  units: string
  /** Increasing; each color applies from its value up to the next one's. */
  stops: LegendStop[]
  decimals: number
}

/**
 * How a layer's value files store numbers (python `ValueEncoding`): int16 GeoTIFFs on the model's grid, where
 * code `n` is no data when -32768, else the value `n * scale + offset`. The layer's lookup file maps display
 * pixels to positions on that grid, sampled every `lookup_step` pixels.
 */
export interface ValueEncoding {
  scale: number
  offset: number
  /** The lookup file's path, relative to the run's folder. */
  lookup: string
  lookup_step: number
}

/** python `RunLayer`. */
export interface RunLayer {
  id: string
  name: string
  /** Annotation maptype (UW WRF only); frames without one can't be promoted for annotation. */
  maptype: string | null
  fingerprint: string
  /** Every hour the layer is published for, exported or not. */
  hours: number[]
  /** `[width, height]` in pixels; null until a frame exists. */
  dimensions: [number, number] | null
  /** Image corners: top-left, top-right, bottom-right, bottom-left (Mapbox order). */
  coordinates: [LngLat, LngLat, LngLat, LngLat] | null
  frames: RunFrame[]
  legend?: Legend | null
  values?: ValueEncoding | null
  /** Outline of the frames' data (the source grid's edges), clockwise from the top-left; absent in older manifests. */
  extent?: LngLat[] | null
}

/** A run's `manifest.json` (python `RunManifest`). */
export interface RunManifest {
  model: string
  name: string
  initialized: string
  registration_revision: number | null
  updated_at: string
  layers: RunLayer[]
  /** Credit for the data, shown on the map. */
  attribution?: string | null
}

// --- what the map shows

export interface ForecastFrame {
  hour: number
  /** Absolute or root-relative URL of the image. */
  url: string
  /** URL of the frame's value file, if the layer has numbers to show under the cursor. */
  valuesUrl: string | null
}

/** One layer of one model run, ready for the map. */
export interface ForecastLayer {
  /** Model id, e.g. `uwwrf_4km`. */
  model: string
  modelName: string
  /** Layer id, e.g. `surface-temp`. */
  id: string
  name: string
  /** Model run initialization time (ISO 8601, UTC). */
  initialized: string
  /** Whether the run is the model's latest complete run. */
  latest: boolean
  /** Whether the run is the model's in-progress run (which may also be complete). */
  inProgress: boolean
  /** `[width, height]` in pixels. */
  dimensions: [number, number]
  /** Image corners: top-left, top-right, bottom-right, bottom-left (Mapbox order). */
  coordinates: [LngLat, LngLat, LngLat, LngLat]
  /** Outline of the frames' data, without the nodata around them; null if the manifest predates it. */
  extent: LngLat[] | null
  /** Sorted by hour. */
  frames: ForecastFrame[]
  /** Hours the layer is published for; more than `frames` while a run is incomplete. */
  hours: number[]
  /** Annotation maptype (UW WRF only). */
  maptype: string | null
  legend: Legend | null
  /** Encoding of the frames' value files, if they have them. */
  values: ValueEncoding | null
  /** URL of the layer's lookup file (see `ValueEncoding`), if its frames have values. */
  lookupUrl: string | null
  attribution: string | null
}
