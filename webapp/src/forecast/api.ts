import type { ForecastLayer, ForecastStatus, ModelStatus, RunManifest, RunRef } from './types'

/**
 * The upslope server's forecast API, fetched at runtime and never bundled. In development, vite proxies
 * it to a running server (`just up`).
 */
export const FORECAST_BASE_URL = (import.meta.env.VITE_FORECAST_BASE_URL ?? '/fx').replace(/\/+$/, '')

function dataUrl(path: string): string {
  return `${FORECAST_BASE_URL}/data/${path.split('/').map(encodeURIComponent).join('/')}`
}

async function getJson<T>(url: string, what: string, signal: AbortSignal | null): Promise<T> {
  const response = await fetch(url, { signal, cache: 'no-cache' })
  if (!response.ok) throw new Error(`Could not load ${what} (${response.status} ${response.statusText})`)
  return (await response.json()) as T
}

export function fetchStatus(signal: AbortSignal | null = null): Promise<ForecastStatus> {
  return getJson<ForecastStatus>(`${FORECAST_BASE_URL}/status`, 'the forecast status', signal)
}

export function fetchRunManifest(model: string, run: RunRef, signal: AbortSignal | null = null): Promise<RunManifest> {
  return getJson<RunManifest>(dataUrl(`${model}/${run.path}/manifest.json`), `the ${model} ${run.path} manifest`, signal)
}

// --- the ?run= URL parameter: a run's initialization time, e.g. `2026-10-29T00Z`

const RUN_PARAM = /^(\d{4})-(\d{2})-(\d{2})T(\d{2})(?::?(\d{2}))?Z?$/

/** The run folder (`2026/10/29/0000Z`) a `?run=` value names, or null if it isn't one. */
export function runPathFromParam(value: string): string | null {
  const match = RUN_PARAM.exec(value)
  if (!match) return null
  const [, year, month, day, hour, minute = '00'] = match
  return `${year}/${month}/${day}/${hour}${minute}Z`
}

/** The `?run=` value for a run: `2026-10-29T00Z`, or `2026-10-29T00:30Z` off the hour. */
export function runParam(run: RunRef): string {
  const [year, month, day, time] = run.path.split('/')
  const hour = time.slice(0, 2)
  const minute = time.slice(2, 4)
  return `${year}-${month}-${day}T${hour}${minute === '00' ? '' : `:${minute}`}Z`
}

/** The run of `model` to show: the one `runPath` names, or else the latest. Null if it has no such run. */
export function selectRun(model: ModelStatus, runPath: string | null): RunRef | null {
  if (runPath === null) return model.latest
  return model.runs.find((run) => run.path === runPath) ?? null
}

/** Map-ready layers of a run; layers with no frames yet are left out. */
export function runLayers(manifest: RunManifest, run: RunRef, latest: RunRef | null): ForecastLayer[] {
  const layers: ForecastLayer[] = []
  for (const layer of manifest.layers) {
    if (!layer.dimensions || !layer.coordinates || !layer.frames.length) continue
    layers.push({
      model: manifest.model,
      modelName: manifest.name,
      id: layer.id,
      name: layer.name,
      initialized: manifest.initialized,
      latest: latest?.path === run.path,
      dimensions: layer.dimensions,
      coordinates: layer.coordinates,
      frames: layer.frames
        .map((frame) => ({ hour: frame.hour, url: dataUrl(`${manifest.model}/${run.path}/${frame.path}`) }))
        .sort((a, b) => a.hour - b.hour),
      hours: layer.hours,
    })
  }
  return layers
}

export function layerKey(layer: Pick<ForecastLayer, 'model' | 'id'>): string {
  return `${layer.model}/${layer.id}`
}

export function layerLabel(layer: Pick<ForecastLayer, 'modelName' | 'name'>): string {
  return `${layer.modelName} · ${layer.name}`
}
