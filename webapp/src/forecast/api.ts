import type { ForecastLayer, ForecastManifest } from './types'

/**
 * Forecast data is fetched at runtime, never bundled. In development the
 * `upslope-data-mounts` plugin serves `forecast/web_export` here; production
 * builds use the server's `/fx/web` proxy (see `.env.production`).
 */
export const FORECAST_BASE_URL = (import.meta.env.VITE_FORECAST_BASE_URL ?? '/api/forecast').replace(/\/+$/, '')

export async function fetchManifest(signal: AbortSignal | null = null): Promise<ForecastManifest> {
  const response = await fetch(`${FORECAST_BASE_URL}/manifest.json`, { signal, cache: 'no-cache' })
  if (!response.ok) throw new Error(`Could not load the forecast manifest (${response.status} ${response.statusText})`)
  const manifest = (await response.json()) as ForecastManifest
  for (const layer of manifest.layers) layer.frames.sort((a, b) => a.hour - b.hour)
  return manifest
}

export function frameUrl(path: string): string {
  return `${FORECAST_BASE_URL}/${path.split('/').map(encodeURIComponent).join('/')}`
}

export function layerKey(layer: Pick<ForecastLayer, 'model' | 'variable'>): string {
  return `${layer.model}/${layer.variable}`
}

/** The domain part of a model name: `uwwrf_12km` -> `12km`. */
export function modelDomain(model: string): string {
  const separator = model.indexOf('_')
  return separator === -1 ? model : model.slice(separator + 1)
}

export function layerLabel(layer: Pick<ForecastLayer, 'model' | 'variable'>): string {
  const variable = layer.variable.replace(/-/g, ' ')
  return `${modelDomain(layer.model)} · ${variable.charAt(0).toUpperCase()}${variable.slice(1)}`
}
