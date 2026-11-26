import type { EChartsCoreOption } from 'echarts/core'
import type { Units } from '@/settings/settings'
import type { ChartTheme } from './palette'
import { modelColor } from './palette'
import type { PointForecast } from './types'
import { displayUnit, type DisplayUnit } from './units'

/**
 * The point forecast's charts: one per variable, comparing every model on the same hourly axis. Everything that
 * decides what a chart shows lives here, as plain functions; `ForecastChart.vue` only draws the result.
 */

export interface ChartSpec {
  /** The variable charted (an id in the response's `variables`). */
  variable: string
  title: string
  /** Shown unless the viewer turns it off. */
  default: boolean
  /** Its y axis starts at zero (amounts, percentages). */
  fromZero?: boolean
  /** Points rather than lines (wind direction: a line would sweep across 360°). */
  scatter?: boolean
  /** A variable shown beside each value in the tooltip, e.g. the hour's precipitation beside the total. */
  companion?: { variable: string; label: string }
}

/** Every chart, in the order shown. Variables a forecast lacks are left out. */
export const CHARTS: ChartSpec[] = [
  { variable: 'temperature_2m', title: 'Temperature', default: true },
  { variable: 'freezing_level', title: 'Freezing level', default: true },
  {
    variable: 'precipitation_accumulated',
    title: 'Precipitation total',
    default: true,
    fromZero: true,
    companion: { variable: 'precipitation', label: 'that hour' },
  },
  {
    variable: 'snowfall_water_equivalent_accumulated',
    title: 'Snowfall total (water equivalent)',
    default: true,
    fromZero: true,
    companion: { variable: 'snowfall_water_equivalent', label: 'that hour' },
  },
  { variable: 'snow_depth', title: 'Snow depth', default: true, fromZero: true },
  { variable: 'wind_speed_10m', title: 'Wind speed', default: true, fromZero: true },
  { variable: 'wind_gust_10m', title: 'Wind gusts', default: true, fromZero: true },
  { variable: 'wind_direction_10m', title: 'Wind direction (from)', default: true, scatter: true },
  { variable: 'cloud_cover', title: 'Cloud cover', default: true, fromZero: true },
  { variable: 'relative_humidity_2m', title: 'Relative humidity', default: true, fromZero: true },
  { variable: 'dew_point_2m', title: 'Dew point', default: true },
  { variable: 'shortwave_radiation', title: 'Solar radiation', default: true, fromZero: true },
  { variable: 'pressure_msl', title: 'Sea-level pressure', default: true },
  { variable: 'precipitation', title: 'Precipitation per hour', default: false, fromZero: true },
  { variable: 'snowfall_water_equivalent', title: 'Snowfall per hour (water equivalent)', default: false, fromZero: true },
  { variable: 'wind_u_10m', title: 'Wind, eastward', default: false },
  { variable: 'wind_v_10m', title: 'Wind, northward', default: false },
]

/** What every chart shares: the forecast, what to show of it, and how. */
export interface ChartContext {
  forecast: PointForecast
  /** Model ids to draw, in order. */
  models: string[]
  /** The x axis, ms. */
  window: [number, number]
  /** Night shading, ms. */
  nights: [number, number][]
  now: number
  units: Units
  theme: ChartTheme
}

const AXIS_TIME = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Los_Angeles', hour: 'numeric' })
const AXIS_DAY = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Los_Angeles', weekday: 'short', day: 'numeric' })
const AXIS_HOUR = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Los_Angeles', hour: 'numeric', hourCycle: 'h23' })
const TOOLTIP_TIME = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Los_Angeles',
  weekday: 'short',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  timeZoneName: 'short',
})

/** Axis labels in Pacific time, like the rest of the app: the day at midnight, else the hour (`Thu 27`, `6 AM`). */
function timeLabel(value: number): string {
  const date = new Date(value)
  return Number(AXIS_HOUR.format(date)) === 0 ? AXIS_DAY.format(date) : AXIS_TIME.format(date)
}

/** Values converted to display units, null where a model has none. */
function displayed(values: (number | null)[], unit: DisplayUnit, variable: string): (number | null)[] {
  return values.map((value) => {
    if (value === null) return null
    // GFS and RRFS report 0 when the freezing level is below their terrain (#46): a gap, not "at sea level".
    if (variable === 'freezing_level' && value === 0) return null
    return unit.convert(value)
  })
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] ?? c)
}

function formatNumber(value: number, decimals: number): string {
  return value.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
}

/** The chart's reference lines: freezing on temperature, the point's elevation on the freezing level. */
function referenceLines(spec: ChartSpec, context: ChartContext, unit: DisplayUnit) {
  if (spec.variable === 'temperature_2m') return [{ yAxis: unit.convert(0), name: 'freezing' }]
  const elevation = context.forecast.point.elevation_m
  if (spec.variable === 'freezing_level' && elevation !== null) return [{ yAxis: unit.convert(elevation), name: 'here' }]
  return []
}

/** One variable's chart: every selected model's series, the night shading, now, and reference lines. */
export function chartOption(spec: ChartSpec, context: ChartContext): EChartsCoreOption {
  const { forecast, theme } = context
  const variable = forecast.variables[spec.variable]
  const unit = displayUnit(variable.units, context.units)
  const times = forecast.times.map((t) => t * 1000)
  const companion = spec.companion && forecast.variables[spec.companion.variable]
  const companionUnit = companion ? displayUnit(companion.units, context.units) : null
  const names = Object.fromEntries(context.models.map((id) => [id, forecast.models[id]?.name ?? id]))
  const ids = Object.fromEntries(context.models.map((id) => [names[id], id]))

  const series: object[] = context.models
    .filter((id) => variable.series[id])
    .map((id) => {
      const values = displayed(variable.series[id], unit, spec.variable)
      const color = modelColor(id, theme.dark)
      return {
        id,
        name: names[id],
        type: spec.scatter ? 'scatter' : 'line',
        data: times.map((t, i) => [t, values[i]]),
        color,
        ...(spec.scatter
          ? { symbolSize: 4, emphasis: { focus: 'series', scale: 1.8 } }
          : {
              showSymbol: !forecast.interpolated,
              symbolSize: 4,
              connectNulls: !forecast.interpolated,
              triggerLineEvent: true,
              lineStyle: { width: 1.5 },
              emphasis: { focus: 'series', lineStyle: { width: 2.5 } },
              blur: { lineStyle: { opacity: 0.25 }, itemStyle: { opacity: 0.25 } },
            }),
      }
    })

  // Shading, now and the reference lines ride on an empty series, so they draw whichever models are shown. They
  // keep their look while a model is emphasized (which blurs every other series, this one included).
  series.push({
    id: '__marks',
    type: 'line',
    data: [],
    silent: true,
    markArea: {
      silent: true,
      itemStyle: { color: theme.shade },
      blur: { itemStyle: { opacity: 1 } },
      data: context.nights.map(([start, end]) => [{ xAxis: start }, { xAxis: end }]),
    },
    markLine: {
      silent: true,
      symbol: 'none',
      animation: false,
      blur: { lineStyle: { opacity: 1 }, label: { opacity: 1 } },
      data: [
        { xAxis: context.now, lineStyle: { color: theme.muted, type: 'solid', width: 1 }, label: { show: false } },
        ...referenceLines(spec, context, unit).map((line) => ({
          yAxis: line.yAxis,
          lineStyle: { color: theme.muted, type: 'dashed', width: 1 },
          label: { formatter: line.name, position: 'insideEndTop', color: theme.muted, fontSize: 10 },
        })),
      ],
    },
  })

  const direction = spec.variable === 'wind_direction_10m'
  return {
    animation: false,
    textStyle: { fontFamily: theme.fontFamily, color: theme.text },
    grid: { left: 52, right: 14, top: 10, bottom: 26 },
    xAxis: {
      type: 'time',
      min: context.window[0],
      max: context.window[1],
      axisLine: { lineStyle: { color: theme.grid } },
      axisTick: { show: false },
      axisLabel: { color: theme.muted, fontSize: 11, hideOverlap: true, formatter: timeLabel },
      splitLine: { show: false },
      axisPointer: { snap: true, lineStyle: { color: theme.muted, width: 1 } },
    },
    yAxis: {
      type: 'value',
      scale: !spec.fromZero && !direction,
      min: direction ? 0 : spec.fromZero ? 0 : undefined,
      max: direction ? 360 : spec.variable.includes('humidity') || spec.variable === 'cloud_cover' ? 100 : undefined,
      interval: direction ? 90 : undefined,
      splitNumber: 4,
      axisLabel: {
        color: theme.muted,
        fontSize: 11,
        formatter: direction
          ? (value: number) => ['N', 'E', 'S', 'W', 'N'][Math.round(value / 90)] ?? ''
          : (value: number) => `${formatNumber(value, value % 1 === 0 ? 0 : 1)}${unit.label === '%' ? '%' : ''}`,
      },
      splitLine: { lineStyle: { color: theme.grid } },
    },
    tooltip: {
      trigger: 'axis',
      confine: true,
      order: 'valueDesc',
      backgroundColor: theme.surface,
      borderColor: theme.grid,
      textStyle: { color: theme.text, fontSize: 12 },
      axisPointer: { type: 'line' },
      formatter: (params: unknown) => {
        const items = (Array.isArray(params) ? params : [params]) as {
          seriesName: string
          value: [number, number | null]
          color: string
        }[]
        const shown = items.filter((item) => item.value[1] !== null && item.value[1] !== undefined)
        if (!items.length) return ''
        const time = TOOLTIP_TIME.format(new Date(items[0].value[0]))
        const rows = shown
          .sort((a, b) => (b.value[1] ?? 0) - (a.value[1] ?? 0))
          .map((item) => {
            const value = item.value[1] as number
            let text = `${formatNumber(value, unit.decimals)} ${escapeHtml(unit.label)}`
            if (companion && companionUnit) {
              const index = forecast.times.indexOf(item.value[0] / 1000)
              const raw = companion.series[ids[item.seriesName]]?.[index]
              if (raw !== null && raw !== undefined) {
                text += ` <span style="color:${theme.muted}">(${formatNumber(companionUnit.convert(raw), companionUnit.decimals)} ${escapeHtml(companionUnit.label)} ${spec.companion?.label})</span>`
              }
            }
            return (
              `<div style="display:flex;align-items:center;gap:8px">` +
              `<span style="width:10px;height:3px;border-radius:2px;background:${item.color}"></span>` +
              `<span style="flex:1">${escapeHtml(item.seriesName)}</span>` +
              `<span style="font-variant-numeric:tabular-nums;font-weight:600">${text}</span></div>`
            )
          })
        return `<div style="margin-bottom:4px;color:${theme.muted}">${escapeHtml(time)}</div>${rows.join('') || '<span>No data</span>'}`
      },
    },
    series,
  }
}

/** The label for a chart's units, e.g. `°F`. */
export function chartUnit(spec: ChartSpec, forecast: PointForecast, units: Units): string {
  const variable = forecast.variables[spec.variable]
  return variable ? displayUnit(variable.units, units).label : ''
}

/** Time windows the viewer can pick, in hours from now; null shows every hour. */
export const WINDOWS: { label: string; hours: number | null }[] = [
  { label: '24 h', hours: 24 },
  { label: '48 h', hours: 48 },
  { label: '3 days', hours: 72 },
  { label: '7 days', hours: 168 },
  { label: 'All', hours: null },
]

/** The x axis for a window: from a few hours ago (to see the recent past) to `hours` ahead, within the forecast. */
export function windowRange(forecast: PointForecast, hours: number | null, now: number): [number, number] {
  const first = (forecast.times[0] ?? now / 1000) * 1000
  const last = (forecast.times.at(-1) ?? now / 1000) * 1000
  if (hours === null) return [first, last]
  const start = Math.max(first, now - 3 * 3_600_000)
  return [start, Math.min(last, start + (hours + 3) * 3_600_000)]
}
