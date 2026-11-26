<script setup lang="ts">
import { Mountain, RotateCcw } from '@lucide/vue'
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useBootstrapModal } from '@/annotation/useBootstrapModal'
import { fetchPointForecast, runLabel } from '@/forecast/api'
import type { LngLat } from '@/forecast/types'
import { settings } from '@/settings/settings'
import { CHARTS, chartOption, chartUnit, windowRange, type ChartContext } from '../charts'
import { modelOrder, useChartTheme } from '../palette'
import { nights } from '../sun'
import type { ElevationSource, ModelInfo, PointForecast } from '../types'
import { displayUnit, formatHeight, formatHeightChange, formatLatLon, heightToMetres } from '../units'
import ForecastChart from './ForecastChart.vue'
import PointForecastControls from './PointForecastControls.vue'

/** Every archived model's forecast at a point (`/fx/point`). Opened from the map's point menu, or `?point=`. */
const props = defineProps<{ point: LngLat }>()
const open = defineModel<boolean>('open', { required: true })

const element = ref<HTMLElement | null>(null)
useBootstrapModal(element, open, () => (open.value = false))

const forecast = ref<PointForecast | null>(null)
const loading = ref(false)
const error = ref('')
/** The viewer's elevation for the point, in metres; null for the server's elevation model. */
const elevation = ref<number | null>(null)
/** Fill the hours between each model's values (the server does it), or show only the hours each model has. */
const interpolate = ref(true)
/** When the forecast loaded: the charts' "now". */
const loadedAt = ref(Date.now())

let abort = new AbortController()
let generation = 0

async function load() {
  abort.abort()
  abort = new AbortController()
  const { signal } = abort
  const gen = ++generation
  const [lon, lat] = props.point
  loading.value = true
  error.value = ''
  try {
    const result = await fetchPointForecast(lat, lon, { elevation: elevation.value, interpolate: interpolate.value }, signal)
    if (gen !== generation) return
    forecast.value = result
    loadedAt.value = Date.now()
  } catch (e) {
    if (!signal.aborted && gen === generation) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (gen === generation) loading.value = false
  }
}

// A new point starts over with the elevation model's height; the dialog fetches each time it opens.
watch(
  () => props.point,
  () => {
    elevation.value = null
    forecast.value = null
    selected.value = null
    if (open.value) void load()
  },
)
watch(
  open,
  (isOpen) => {
    if (isOpen) void load()
    else abort.abort()
  },
  { immediate: true },
)
onBeforeUnmount(() => abort.abort())

const system = computed(() => settings.units)
const heightUnit = computed(() => displayUnit('m', system.value))

const SOURCES: Record<ElevationSource, string> = {
  usgs_3dep: 'USGS 3DEP',
  copernicus_glo30: 'Copernicus GLO-30',
  request: 'entered',
}

// --- the elevation field: the viewer's height for the point, in their units

const elevationInput = ref('')
watch(
  [() => forecast.value?.point.elevation_m, heightUnit],
  ([metres, unit]) => {
    elevationInput.value = metres === null || metres === undefined ? '' : String(Math.round(unit.convert(metres)))
  },
  { immediate: true },
)

function applyElevation() {
  const typed = Number(elevationInput.value)
  if (elevationInput.value.trim() === '' || !Number.isFinite(typed)) return
  elevation.value = Math.round(heightToMetres(typed, system.value))
  void load()
}

function resetElevation() {
  elevation.value = null
  void load()
}

watch(interpolate, () => void load())

// --- charts

const theme = useChartTheme()
/** The models shown; null until the viewer changes it (then every model with data). */
const selected = ref<Set<string> | null>(null)
const windowHours = ref<number | null>(72)
const visible = ref(new Set(CHARTS.filter((chart) => chart.default).map((chart) => chart.variable)))
/** A model to emphasize in every chart (its control is hovered). */
const highlight = ref<string | null>(null)

const modelChoices = computed(() => {
  const current = forecast.value
  if (!current) return []
  const ids = [...Object.keys(current.models), ...Object.keys(current.missing)].sort(modelOrder)
  return ids.map((id) => ({ id, name: current.models[id]?.name ?? id, missing: current.missing[id] ?? null }))
})
const shownModels = computed({
  get: () => selected.value ?? new Set(Object.keys(forecast.value?.models ?? {})),
  set: (models: Set<string>) => (selected.value = models),
})
/** The charts this forecast has data for. */
const availableCharts = computed(() => CHARTS.filter((chart) => forecast.value?.variables[chart.variable]))
const shownCharts = computed(() => availableCharts.value.filter((chart) => visible.value.has(chart.variable)))

const context = computed<ChartContext | null>(() => {
  const current = forecast.value
  if (!current) return null
  const range = windowRange(current, windowHours.value, loadedAt.value)
  return {
    forecast: current,
    models: Object.keys(current.models)
      .filter((id) => shownModels.value.has(id))
      .sort(modelOrder),
    window: range,
    nights: nights(range[0], range[1], current.point.lat, current.point.lon),
    now: loadedAt.value,
    units: system.value,
    theme: theme.value,
  }
})
const options = computed(() =>
  context.value ? Object.fromEntries(shownCharts.value.map((chart) => [chart.variable, chartOption(chart, context.value!)])) : {},
)

// --- models

interface ModelRow {
  id: string
  info: ModelInfo
}

const models = computed<ModelRow[]>(() =>
  Object.entries(forecast.value?.models ?? {}).map(([id, info]) => ({ id, info })),
)
const missing = computed(() => Object.entries(forecast.value?.missing ?? {}))

function adjustment(info: ModelInfo): string {
  const { adjustment: a } = info
  if (!a.applied) return `Not adjusted: ${a.reason ?? 'unknown'}`
  return `${formatHeightChange(a.delta_m, system.value)} at ${a.lapse_rate_k_per_km ?? '?'} K/km`
}
</script>

<template>
  <div ref="element" class="modal fade" tabindex="-1" aria-labelledby="point-forecast-title">
    <div class="modal-dialog modal-xl modal-fullscreen-lg-down modal-dialog-scrollable">
      <div class="modal-content">
        <div class="modal-header align-items-start">
          <div class="me-3">
            <h5 id="point-forecast-title" class="modal-title">Point forecast</h5>
            <div class="small text-body-secondary">{{ formatLatLon(props.point[1], props.point[0]) }}</div>
          </div>
          <form class="elevation ms-auto d-flex align-items-center gap-1 small" @submit.prevent="applyElevation">
            <Mountain :size="16" aria-hidden="true" class="text-body-secondary flex-shrink-0" />
            <label class="visually-hidden" for="point-elevation">Elevation ({{ heightUnit.label }})</label>
            <div class="input-group input-group-sm">
              <input
                id="point-elevation"
                v-model="elevationInput"
                class="form-control"
                type="number"
                inputmode="numeric"
                :placeholder="forecast && forecast.point.elevation_m === null ? 'unknown' : ''"
                title="The point's elevation: forecasts are adjusted to it. Change it if you know better."
              />
              <span class="input-group-text">{{ heightUnit.label }}</span>
            </div>
            <span v-if="forecast?.point.elevation_source" class="text-body-secondary text-nowrap">
              {{ SOURCES[forecast.point.elevation_source] }}
            </span>
            <button
              v-if="elevation !== null"
              type="button"
              class="btn btn-link btn-sm p-0 text-body-secondary"
              title="Use the elevation model's height"
              @click="resetElevation"
            >
              <RotateCcw :size="14" aria-hidden="true" />
              <span class="visually-hidden">Reset the elevation</span>
            </button>
          </form>
          <button type="button" class="btn-close ms-3" aria-label="Close" @click="open = false"></button>
        </div>
        <div class="modal-body vstack gap-3">
          <div v-if="error" class="alert alert-danger py-2 mb-0 small d-flex align-items-center gap-2">
            {{ error }}
            <button type="button" class="btn btn-outline-danger btn-sm ms-auto" @click="load">Try again</button>
          </div>
          <div v-if="loading && !forecast" class="d-flex align-items-center gap-2 text-body-secondary">
            <span class="spinner-border spinner-border-sm" aria-hidden="true"></span>
            Loading the forecast…
          </div>

          <template v-if="forecast && context">
            <PointForecastControls
              v-model:selected="shownModels"
              v-model:window-hours="windowHours"
              v-model:visible="visible"
              v-model:interpolate="interpolate"
              v-model:highlight="highlight"
              class="controls-bar sticky-top"
              :models="modelChoices"
              :dark="theme.dark"
              :charts="availableCharts"
            />

            <div class="row row-cols-1 row-cols-lg-2 g-3" :class="{ 'opacity-50': loading }">
              <section v-for="chart in shownCharts" :key="chart.variable" class="col">
                <h6 class="chart-title mb-0">
                  {{ chart.title }}
                  <span class="text-body-secondary fw-normal">{{ chartUnit(chart, forecast, system) }}</span>
                </h6>
                <ForecastChart :option="options[chart.variable]" group="point-forecast" :highlight="highlight" />
              </section>
            </div>
            <p class="form-text mb-0">
              Times are Pacific. Shading marks night; the vertical line is now. Hover a chart to read every model's
              value at that hour.
            </p>

            <section>
              <h6 class="mb-2">Models</h6>
              <div class="table-responsive">
                <table class="table table-sm small align-middle mb-0">
                  <thead>
                    <tr class="text-body-secondary">
                      <th scope="col" class="fw-normal">Model</th>
                      <th scope="col" class="fw-normal">Run</th>
                      <th scope="col" class="fw-normal">Model terrain</th>
                      <th scope="col" class="fw-normal">Adjusted to the point</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in models" :key="row.id">
                      <td class="fw-semibold text-nowrap">{{ row.info.name }}</td>
                      <td class="text-nowrap">{{ runLabel(row.info.init_time) }}</td>
                      <td class="text-nowrap">{{ formatHeight(row.info.cell.elevation_m, system) }}</td>
                      <td :class="{ 'text-body-secondary': !row.info.adjustment.applied }">{{ adjustment(row.info) }}</td>
                    </tr>
                    <tr v-for="[id, reason] in missing" :key="id" class="text-body-secondary">
                      <td class="text-nowrap">{{ id }}</td>
                      <td colspan="3">Not shown: {{ reason }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p class="form-text mb-0 mt-2">
                Temperature, dew point and humidity are adjusted from each model's terrain to the point's elevation
                with a standard lapse rate.
              </p>
            </section>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.elevation .input-group {
  width: 8.5rem;
}

/* The controls stay in view while the charts scroll under them. */
.controls-bar {
  top: calc(-1 * var(--bs-modal-padding));
  margin: calc(-1 * var(--bs-modal-padding)) calc(-1 * var(--bs-modal-padding)) 0;
  padding: 0.75rem var(--bs-modal-padding);
  background: var(--bs-modal-bg);
  border-bottom: var(--bs-border-width) solid var(--bs-border-color-translucent);
}

.chart-title {
  font-size: 0.875rem;
}
</style>
