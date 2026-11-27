/**
 * `GET /fx/point`: every archived model's forecast at a point, on one hourly axis, grouped by variable. Mirrors
 * `PointForecast` in `python/upslope/archive/point.py`; see the "Point forecasts" section of `python/README.md`.
 */

/** Where the point's elevation came from: the DEM's sources, or the request's `elevation`. */
export type ElevationSource = 'usgs_3dep' | 'copernicus_glo30' | 'request'

export interface PointInfo {
  lat: number
  lon: number
  /** The elevation values were adjusted to; null when unknown (no DEM there, and none in the request). */
  elevation_m: number | null
  elevation_source: ElevationSource | null
}

/** Up to `until_hour` (hours since the run started), the model's values are `step` hours apart. */
export interface ModelSteps {
  until_hour: number
  step: number
}

/** The model's grid cell the point falls in. */
export interface CellInfo {
  lat: number
  lon: number
  /** The model's terrain height there; null when the run has none. */
  elevation_m: number | null
}

/** Whether a model's values were moved from its terrain to the point's elevation, and how; or why not. */
export interface Adjustment {
  applied: boolean
  method: string | null
  lapse_rate_k_per_km: number | null
  /** The point's elevation less the model cell's. */
  delta_m: number | null
  variables: string[]
  /** Why not, when not applied: `no model terrain`, `point elevation unknown`, … */
  reason: string | null
}

export interface ModelInfo {
  name: string
  /** ISO 8601 */
  init_time: string
  steps: ModelSteps[]
  cell: CellInfo
  adjustment: Adjustment
  attribution: string | null
}

/** `instant`; `sum` / `mean` over the interval ending at each value; `total` since `accumulation_start`. */
export type VariableKind = 'instant' | 'sum' | 'mean' | 'total'

export interface VariableSeries {
  name: string
  /** Standard units, e.g. `degC`, `m`, `mm`, `m s-1`, `%`, `degree`, `W m-2`, `hPa`. */
  units: string
  kind: VariableKind
  /** Model id -> a value or null per hour of `times`. */
  series: Record<string, (number | null)[]>
}

export interface PointForecast {
  point: PointInfo
  /** The hourly axis, Unix seconds. */
  times: number[]
  interpolated: boolean
  /** Unix seconds: running totals (`kind: 'total'`) count from here. */
  accumulation_start: number | null
  models: Record<string, ModelInfo>
  variables: Record<string, VariableSeries>
  /** Model id -> why it's left out: `outside the model's region`, `no archived run`, `loading`, … */
  missing: Record<string, string>
}
