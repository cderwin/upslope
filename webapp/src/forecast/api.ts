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

// --- the ?run= URL parameter: a run's initialization time (`2026-10-29T00Z`), or `in-progress`

export const IN_PROGRESS = 'in-progress'

const RUN_PARAM = /^(\d{4})-(\d{2})-(\d{2})T(\d{2})(?::?(\d{2}))?Z?$/

/** Which run a `?run=` value asks for: a run folder (`2026/10/29/0000Z`), `in-progress`, or null if neither. */
export function runPathFromParam(value: string): string | null {
  if (value === IN_PROGRESS) return IN_PROGRESS
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

/** The run of `model` to show: the one `runPath` names (or `in-progress`), else the latest. Null if there's none. */
export function selectRun(model: ModelStatus, runPath: string | null): RunRef | null {
  if (runPath === null) return model.latest
  if (runPath === IN_PROGRESS) return model.in_progress
  return model.runs.find((run) => run.path === runPath) ?? null
}

/** Map-ready layers of a run; layers with no frames yet are left out. */
export function runLayers(manifest: RunManifest, run: RunRef, model: ModelStatus): ForecastLayer[] {
  const layers: ForecastLayer[] = []
  for (const layer of manifest.layers) {
    if (!layer.dimensions || !layer.coordinates || !layer.frames.length) continue
    layers.push({
      model: manifest.model,
      modelName: manifest.name,
      id: layer.id,
      name: layer.name,
      initialized: manifest.initialized,
      latest: model.latest?.path === run.path,
      inProgress: model.in_progress?.path === run.path,
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

/** e.g. `Oct 22 06Z`, the convention for model run times. */
export function runLabel(initialized: string): string {
  const date = new Date(initialized)
  if (Number.isNaN(date.getTime())) return ''
  const parts = RUN_FORMAT.formatToParts(date)
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? ''
  return `${part('month')} ${part('day')} ${part('hour')}Z`
}

const RUN_FORMAT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  hourCycle: 'h23',
})

/** How long ago a time was, roughly: `just now`, `12 min ago`, `3 h ago`. */
export function ago(time: string | null, now: number = Date.now()): string {
  if (!time) return 'never'
  const minutes = Math.round((now - new Date(time).getTime()) / 60_000)
  if (minutes < 1) return 'just now'
  if (minutes < 90) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`
}

export function layerLabel(layer: Pick<ForecastLayer, 'modelName' | 'name'>): string {
  return `${layer.modelName} · ${layer.name}`
}
