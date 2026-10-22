/** `[longitude, latitude]` */
export type LngLat = [number, number]

export interface ForecastFrame {
  hour: number
  /** Relative to the forecast base URL. */
  path: string
}

/** One model/variable combination, as written by `upslope export-web` (python `ManifestLayer`). */
export interface ForecastLayer {
  /** Model and domain, e.g. `uwwrf_12km`. */
  model: string
  variable: string
  /** Model run initialization time (ISO 8601, UTC). */
  initialized: string
  /** `[width, height]` in pixels. */
  dimensions: [number, number]
  /** Image corners: top-left, top-right, bottom-right, bottom-left (Mapbox order). */
  coordinates: [LngLat, LngLat, LngLat, LngLat]
  /** Sorted by hour. */
  frames: ForecastFrame[]
}

/** python `Manifest`. */
export interface ForecastManifest {
  /** When the export started and finished (ISO 8601). */
  started: string
  finished: string
  layers: ForecastLayer[]
}
