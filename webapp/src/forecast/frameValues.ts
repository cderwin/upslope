import mapboxgl from 'mapbox-gl'
import type { ForecastLayer, Legend, LngLat, ValueEncoding } from './types'

/** A decoded value image: RGBA bytes, row by row. */
export interface ValueImage {
  width: number
  height: number
  rgba: Uint8ClampedArray
}

// Value images are only fetched once the cursor is over a frame; keep the recent ones for playback.
const CACHE_SIZE = 64
const cache = new Map<string, Promise<ValueImage>>()

/** Fetch and decode a value image, exactly: no color management or alpha premultiplication. */
export function loadValueImage(url: string): Promise<ValueImage> {
  const cached = cache.get(url)
  if (cached) {
    cache.delete(url)
    cache.set(url, cached) // most recently used last
    return cached
  }
  const loading = decode(url)
  loading.catch(() => cache.delete(url)) // try again next time
  cache.set(url, loading)
  if (cache.size > CACHE_SIZE) cache.delete(cache.keys().next().value!)
  return loading
}

async function decode(url: string): Promise<ValueImage> {
  const response = await fetch(url)
  if (!response.ok) throw new Error(`Could not load ${url} (${response.status})`)
  const bitmap = await createImageBitmap(await response.blob(), {
    premultiplyAlpha: 'none',
    colorSpaceConversion: 'none',
  })
  const canvas = document.createElement('canvas')
  canvas.width = bitmap.width
  canvas.height = bitmap.height
  const context = canvas.getContext('2d', { willReadFrequently: true })
  if (!context) throw new Error('Canvas 2D is unavailable')
  context.drawImage(bitmap, 0, 0)
  bitmap.close()
  const { data } = context.getImageData(0, 0, canvas.width, canvas.height)
  return { width: canvas.width, height: canvas.height, rgba: data }
}

/**
 * The pixel of a layer's frames under a map position, or null outside them. Frames are axis-aligned in
 * Web Mercator between their corners, so the position maps linearly in Mercator coordinates.
 */
export function pixelAt(layer: Pick<ForecastLayer, 'coordinates' | 'dimensions'>, lngLat: LngLat): [number, number] | null {
  const [topLeft, , bottomRight] = layer.coordinates.map((corner) => mapboxgl.MercatorCoordinate.fromLngLat(corner))
  const point = mapboxgl.MercatorCoordinate.fromLngLat(lngLat)
  const [width, height] = layer.dimensions
  const col = Math.floor(((point.x - topLeft.x) / (bottomRight.x - topLeft.x)) * width)
  const row = Math.floor(((point.y - topLeft.y) / (bottomRight.y - topLeft.y)) * height)
  return col >= 0 && col < width && row >= 0 && row < height ? [col, row] : null
}

/** The number stored at a pixel, or null where there is no data. */
export function valueAt(image: ValueImage, encoding: ValueEncoding, col: number, row: number): number | null {
  const i = (row * image.width + col) * 4
  const code = image.rgba[i] * 65536 + image.rgba[i + 1] * 256 + image.rgba[i + 2]
  return code === 0 ? null : (code - 1) * encoding.scale + encoding.offset
}

/** e.g. `31 °F` or `0.25 in`, with the legend's decimal places. */
export function formatValue(value: number, legend: Pick<Legend, 'units' | 'decimals'> | null): string {
  const decimals = legend?.decimals ?? 1
  // avoid "-0" for values that round to zero
  const text = (Math.abs(value) < 0.5 * 10 ** -decimals ? 0 : value).toFixed(decimals)
  return legend?.units ? `${text} ${legend.units}` : text
}
