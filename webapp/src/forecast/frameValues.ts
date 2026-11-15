import mapboxgl from 'mapbox-gl'
import { geotiff } from './valueDecoder'
import type { ForecastLayer, Legend, LngLat, ValueEncoding } from './types'

/** A decoded GeoTIFF: its bands, each row by row. */
interface Grid<Band> {
  width: number
  height: number
  bands: Band[]
}

/** A frame's values: int16 codes on the model's grid (python `encode_values`). */
export type ValueGrid = Grid<Int16Array>
/** A layer's lookup: grid column and row at every `lookup_step`-th display pixel (python `encode_lookup`). */
export type LookupGrid = Grid<Float32Array>

// int16 code for no data in value files
const NODATA = -32768
// Value files are only fetched once the cursor is over a frame; keep the recent ones for playback. Lookups
// are one per layer.
const VALUES_CACHE_SIZE = 64
const LOOKUP_CACHE_SIZE = 8
const valuesCache = new Map<string, Promise<ValueGrid>>()
const lookupCache = new Map<string, Promise<LookupGrid>>()

function cached<T>(cache: Map<string, Promise<T>>, size: number, url: string, load: (url: string) => Promise<T>): Promise<T> {
  const hit = cache.get(url)
  if (hit) {
    cache.delete(url)
    cache.set(url, hit) // most recently used last
    return hit
  }
  const loading = load(url)
  loading.catch(() => cache.delete(url)) // try again next time
  cache.set(url, loading)
  if (cache.size > size) cache.delete(cache.keys().next().value!)
  return loading
}

async function readGrid<Band>(url: string): Promise<Grid<Band>> {
  const response = await fetch(url)
  if (!response.ok) throw new Error(`Could not load ${url} (${response.status})`)
  const { fromArrayBuffer } = await geotiff()
  const image = await (await fromArrayBuffer(await response.arrayBuffer())).getImage()
  const bands = await image.readRasters({ interleave: false })
  return { width: image.getWidth(), height: image.getHeight(), bands: Array.from(bands) as Band[] }
}

/** Fetch and decode a frame's value file. */
export function loadValues(url: string): Promise<ValueGrid> {
  return cached(valuesCache, VALUES_CACHE_SIZE, url, readGrid<Int16Array>)
}

/** Fetch and decode a layer's lookup file; worth calling ahead of time, as a layer is shown. */
export function loadLookup(url: string): Promise<LookupGrid> {
  return cached(lookupCache, LOOKUP_CACHE_SIZE, url, readGrid<Float32Array>)
}

/**
 * Where a map position falls on a layer's frame image, in (fractional) pixels from its top-left corner, or null
 * outside it. Frames are axis-aligned in Web Mercator between their corners, so it maps linearly there.
 */
export function displayPosition(layer: Pick<ForecastLayer, 'coordinates' | 'dimensions'>, lngLat: LngLat): [number, number] | null {
  const [topLeft, , bottomRight] = layer.coordinates.map((corner) => mapboxgl.MercatorCoordinate.fromLngLat(corner))
  const point = mapboxgl.MercatorCoordinate.fromLngLat(lngLat)
  const [width, height] = layer.dimensions
  const x = ((point.x - topLeft.x) / (bottomRight.x - topLeft.x)) * width
  const y = ((point.y - topLeft.y) / (bottomRight.y - topLeft.y)) * height
  return x >= 0 && x < width && y >= 0 && y < height ? [x, y] : null
}

/** The lookup interpolated at a display position: a (continuous, corner-based) position on the values' grid. */
function gridPosition(lookup: LookupGrid, step: number, [x, y]: [number, number]): [number, number] | null {
  const i = x / step
  const j = y / step
  const i0 = Math.min(Math.floor(i), lookup.width - 2)
  const j0 = Math.min(Math.floor(j), lookup.height - 2)
  const fi = i - i0
  const fj = j - j0
  const at = (band: Float32Array) => {
    const k = j0 * lookup.width + i0
    return (
      band[k] * (1 - fi) * (1 - fj) +
      band[k + 1] * fi * (1 - fj) +
      band[k + lookup.width] * (1 - fi) * fj +
      band[k + lookup.width + 1] * fi * fj
    )
  }
  const col = at(lookup.bands[0])
  const row = at(lookup.bands[1])
  return Number.isFinite(col) && Number.isFinite(row) ? [col, row] : null
}

/**
 * The value at a display position, interpolated between the four grid cells around it as the image's colors
 * are, or null where there's no data. Cells without data are left out of the interpolation, unless the
 * position is in one.
 */
export function valueAt(lookup: LookupGrid, values: ValueGrid, encoding: ValueEncoding, position: [number, number]): number | null {
  const found = gridPosition(lookup, encoding.lookup_step, position)
  if (!found) return null
  const [col, row] = found
  const { width, height } = values
  const codes = values.bands[0]
  if (col < 0 || row < 0 || col >= width || row >= height) return null
  if (codes[Math.floor(row) * width + Math.floor(col)] === NODATA) return null
  // cell (c, r) spans c..c+1, so its center is at c + 0.5
  const u = col - 0.5
  const v = row - 0.5
  const c0 = Math.floor(u)
  const r0 = Math.floor(v)
  let sum = 0
  let weight = 0
  for (const [dc, dr] of [[0, 0], [1, 0], [0, 1], [1, 1]]) {
    const c = Math.min(Math.max(c0 + dc, 0), width - 1)
    const r = Math.min(Math.max(r0 + dr, 0), height - 1)
    const code = codes[r * width + c]
    if (code === NODATA) continue
    const w = (dc ? u - c0 : 1 - (u - c0)) * (dr ? v - r0 : 1 - (v - r0))
    sum += w * code
    weight += w
  }
  return weight > 0 ? (sum / weight) * encoding.scale + encoding.offset : null
}

/** e.g. `31 °F` or `0.25 in`, with the legend's decimal places. */
export function formatValue(value: number, legend: Pick<Legend, 'units' | 'decimals'> | null): string {
  const decimals = legend?.decimals ?? 1
  // avoid "-0" for values that round to zero
  const text = (Math.abs(value) < 0.5 * 10 ** -decimals ? 0 : value).toFixed(decimals)
  return legend?.units ? `${text} ${legend.units}` : text
}
