import { computed } from 'vue'
import { useTheme } from './useTheme'

/**
 * Chart colors, read at runtime from the design tokens (src/styles/tokens.css), so charts follow the theme. Tokens
 * are OKLCH, which ECharts' and Mapbox's own color parsing doesn't understand; `toColor` hands them an rgb() the
 * browser converted. Map colormaps aren't here: the exporter draws them (python/upslope/models/dynamical/scales.py)
 * and manifests carry their legends.
 */

/** A CSS custom property on <html>, as written (`oklch(0.74 0.14 252)`). */
export function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

let context: CanvasRenderingContext2D | null = null
const converted = new Map<string, string>()

/** Any CSS color as `rgb(r g b)` / `rgba(r, g, b, a)`, which every library parses (out-of-gamut colors are clipped). */
export function toColor(color: string): string {
  const cached = converted.get(color)
  if (cached) return cached
  context ??= document.createElement('canvas').getContext('2d', { willReadFrequently: true })
  if (!context) return color
  context.clearRect(0, 0, 1, 1)
  context.fillStyle = color
  context.fillRect(0, 0, 1, 1)
  const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data
  const result = a === 255 ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${+(a / 255).toFixed(3)})`
  converted.set(color, result)
  return result
}

/** A token as a color every library parses. */
export function token(name: string): string {
  return toColor(css(name))
}

/**
 * Each model's series slot (`--up-series-N`), keyed by the archive's model id (python/config/archive.yaml), so a
 * model keeps its color in every chart and whichever models are shown. GEFS, GDPS and ICON hold their slots for
 * when they're archived.
 */
export const MODEL_ORDER = [
  'hrrr_3km', // HRRR
  'rrfs_3km', // RRFS
  'gfs_0.25', // GFS
  'aifs_0.25', // ECMWF AIFS
  'hrdps_2.5km', // HRDPS
  'ifs_0.25', // ECMWF IFS
  'nbm_2.5km', // NBM
  'gefs', // GEFS
  'gdps', // GDPS
  'icon', // ICON
]

function seriesSlot(model: string): number {
  const index = MODEL_ORDER.indexOf(model)
  return ((index < 0 ? MODEL_ORDER.length : index) % 10) + 1
}

/** A model's series color, for chart libraries. */
export function modelColor(model: string): string {
  return token(`--up-series-${seriesSlot(model)}`)
}

/** A model's series color as a CSS value, for the DOM (follows the theme without re-reading). */
export function modelColorVar(model: string): string {
  return `var(--up-series-${seriesSlot(model)})`
}

/** Models in their slot order (unknown ones last, by id), so lists and legends always read the same way. */
export function modelOrder(a: string, b: string): number {
  const rank = (id: string) => (MODEL_ORDER.includes(id) ? MODEL_ORDER.indexOf(id) : MODEL_ORDER.length)
  return rank(a) - rank(b) || a.localeCompare(b)
}

/** What a chart draws besides its series. */
export interface ChartChrome {
  grid: string
  tick: string
  text: string
  night: string
  /** The "now" marker: ink at 60%. */
  now: string
  tooltipBg: string
  tooltipBorder: string
  font: string
}

export function chartChrome(): ChartChrome {
  return {
    grid: token('--up-line'),
    tick: token('--up-muted'),
    text: token('--up-ink'),
    night: token('--up-night'),
    now: toColor(`color-mix(in oklch, ${css('--up-ink')} 60%, transparent)`),
    tooltipBg: token('--up-raised'),
    tooltipBorder: token('--up-line'),
    font: css('--up-font'),
  }
}

export type StatusLevel = 'good' | 'watch' | 'warning' | 'danger'

/** A status level's colors: `dot` for icons and lines, `bg` + `fg` for chips. */
export function statusColor(level: StatusLevel): { dot: string; bg: string; fg: string } {
  return { dot: token(`--up-${level}`), bg: token(`--up-${level}-bg`), fg: token(`--up-${level}-fg`) }
}

/** The chart chrome for the current theme, re-read when the theme changes. */
export function useChartChrome() {
  const { theme } = useTheme()
  return computed(() => {
    void theme.value // re-read the tokens when the theme flips
    return chartChrome()
  })
}
