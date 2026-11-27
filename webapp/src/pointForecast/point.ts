import type { LngLat } from '@/forecast/types'

/**
 * The `?point=lat,lon` URL parameter: the point whose forecast is open, so it can be shared or reloaded.
 * Four decimals is about 10 m, finer than any model or the elevation model.
 */

const POINT_PARAM = /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/

/** The point a `?point=` value names, or null if it isn't a valid `lat,lon`. */
export function pointFromParam(value: unknown): LngLat | null {
  if (typeof value !== 'string') return null
  const match = POINT_PARAM.exec(value)
  if (!match) return null
  const lat = Number(match[1])
  const lon = Number(match[2])
  if (Math.abs(lat) > 90 || Math.abs(lon) > 180) return null
  return [lon, lat]
}

/** The `?point=` value for a point: `46.8356,-121.7314`. */
export function pointParam([lon, lat]: LngLat): string {
  return `${lat.toFixed(4)},${lon.toFixed(4)}`
}
