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
  /** Newest complete run, if any has been exported. */
  latest: RunRef | null
  /** Newest run that has started, and how far it has got. */
  in_progress: InProgressStatus | null
  /** Retained runs with a manifest, newest first. */
  runs: RunRef[]
}

/** `GET <base>/status` (python `ForecastStatus`). */
export interface ForecastStatus {
  models: ModelStatus[]
}

/** python `Frame`. */
export interface RunFrame {
  hour: number
  /** Valid time (ISO 8601, UTC). */
  valid: string
  /** Relative to the run's folder. */
  path: string
}

/** python `RunLayer`. */
export interface RunLayer {
  id: string
  name: string
  maptype: string
  fingerprint: string
  /** Every hour the layer is published for, exported or not. */
  hours: number[]
  /** `[width, height]` in pixels; null until a frame exists. */
  dimensions: [number, number] | null
  /** Image corners: top-left, top-right, bottom-right, bottom-left (Mapbox order). */
  coordinates: [LngLat, LngLat, LngLat, LngLat] | null
  frames: RunFrame[]
}

/** A run's `manifest.json` (python `RunManifest`). */
export interface RunManifest {
  model: string
  name: string
  initialized: string
  registration_revision: number
  updated_at: string
  layers: RunLayer[]
}

// --- what the map shows

export interface ForecastFrame {
  hour: number
  /** Absolute or root-relative URL of the image. */
  url: string
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
  /** Sorted by hour. */
  frames: ForecastFrame[]
  /** Hours the layer is published for; more than `frames` while a run is incomplete. */
  hours: number[]
}
