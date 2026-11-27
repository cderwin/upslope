import type { Units } from '@/settings/settings'

/**
 * Display units for the point forecast. The server sends standard (metric) units and names them; imperial
 * converts in the browser.
 */

export interface DisplayUnit {
  /** e.g. `°F` */
  label: string
  /** Decimals to show. */
  decimals: number
  /** From the server's units to these. */
  convert: (value: number) => number
}

const same = (value: number) => value

/** Server units -> display units, per system. Anything unlisted shows as the server sends it. */
const UNITS: Record<string, Record<Units, DisplayUnit>> = {
  degC: {
    imperial: { label: '°F', decimals: 0, convert: (c) => (c * 9) / 5 + 32 },
    metric: { label: '°C', decimals: 1, convert: same },
  },
  m: {
    imperial: { label: 'ft', decimals: 0, convert: (m) => m / 0.3048 },
    metric: { label: 'm', decimals: 0, convert: same },
  },
  mm: {
    imperial: { label: 'in', decimals: 2, convert: (mm) => mm / 25.4 },
    metric: { label: 'mm', decimals: 1, convert: same },
  },
  'm s-1': {
    imperial: { label: 'mph', decimals: 0, convert: (ms) => ms * 2.236936 },
    metric: { label: 'm/s', decimals: 1, convert: same },
  },
  '%': {
    imperial: { label: '%', decimals: 0, convert: same },
    metric: { label: '%', decimals: 0, convert: same },
  },
  degree: {
    imperial: { label: '°', decimals: 0, convert: same },
    metric: { label: '°', decimals: 0, convert: same },
  },
  'W m-2': {
    imperial: { label: 'W/m²', decimals: 0, convert: same },
    metric: { label: 'W/m²', decimals: 0, convert: same },
  },
  hPa: {
    imperial: { label: 'hPa', decimals: 0, convert: same },
    metric: { label: 'hPa', decimals: 0, convert: same },
  },
}

/** How to show values the server gives in `units`. */
export function displayUnit(units: string, system: Units): DisplayUnit {
  return UNITS[units]?.[system] ?? { label: units, decimals: 1, convert: same }
}

/** A value in display units with its label, e.g. `28 °F`; `—` for none. */
export function formatMeasure(value: number | null | undefined, unit: DisplayUnit): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  const shown = unit.convert(value).toLocaleString('en-US', {
    minimumFractionDigits: unit.decimals,
    maximumFractionDigits: unit.decimals,
  })
  const space = unit.label.startsWith('°') && unit.label.length === 1 ? '' : ' '
  return `${shown}${space}${unit.label}`
}

/** A height in metres, in the viewer's units, e.g. `10,066 ft`. */
export function formatHeight(metres: number | null | undefined, system: Units): string {
  return formatMeasure(metres, displayUnit('m', system))
}

/** A height difference, signed, e.g. `+1,695 ft`. */
export function formatHeightChange(metres: number | null | undefined, system: Units): string {
  const text = formatHeight(metres, system)
  return metres !== null && metres !== undefined && metres > 0 ? `+${text}` : text
}

/** Metres from a height the viewer typed in their units. */
export function heightToMetres(value: number, system: Units): number {
  return system === 'imperial' ? value * 0.3048 : value
}

/** `46.8356° N, 121.7314° W` */
export function formatLatLon(lat: number, lon: number): string {
  const ns = lat >= 0 ? 'N' : 'S'
  const ew = lon >= 0 ? 'E' : 'W'
  return `${Math.abs(lat).toFixed(4)}° ${ns}, ${Math.abs(lon).toFixed(4)}° ${ew}`
}
