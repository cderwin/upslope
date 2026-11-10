<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ForecastMap from '@/forecast/components/ForecastMap.vue'
import MapControls from '@/forecast/components/MapControls.vue'
import MapLegend from '@/forecast/components/MapLegend.vue'
import {
  FORECAST_BASE_URL,
  IN_PROGRESS,
  dataBaseUrl,
  fetchRunManifest,
  fetchStatus,
  runLayers,
  runParam,
  runLabel,
  runPathFromParam,
  selectRun,
} from '@/forecast/api'
import {
  modelChoices,
  pickModel,
  resolveSelection,
  sameValidHour,
  variableGroups,
  type Choices,
  type LoadedLayers,
} from '@/forecast/selection'
import { openRunStatus, setStatus } from '@/forecast/status'
import type { ForecastLayer, ForecastStatus, RunRef } from '@/forecast/types'
import { usePlayback } from '@/forecast/usePlayback'
import { adminToken } from '@/admin/token'
import { useAnnotationStore } from '@/annotation/store'
import { settings } from '@/settings/settings'

/** Route params, so the selected model and variable (layer) are a shareable URL under /map. */
const props = defineProps<{ model?: string; layer?: string }>()

const DEFAULT_OPACITY = 0.7
/** How often a preview of the in-progress run looks for new frames. */
const PREVIEW_REFRESH_MS = 3 * 60_000

const route = useRoute()
const router = useRouter()
const token = import.meta.env.VITE_MAPBOX_TOKEN ?? ''

const status = ref<ForecastStatus | null>(null)
/** Map-ready layers of each model's run, for the models whose manifest has loaded. */
const modelLayers = ref<Record<string, ForecastLayer[]>>({})
const error = ref('')

/** `?run=`: a run other than the latest, e.g. `2026-10-29T00Z`, or `in-progress` to preview the newest run. */
const runQuery = computed(() => (typeof route.query.run === 'string' ? route.query.run : undefined))
const previewing = computed(() => runQuery.value === IN_PROGRESS)
const runPath = computed(() => (runQuery.value === undefined ? null : runPathFromParam(runQuery.value)))

const choices = computed<Choices | null>(() => {
  if (!status.value) return null
  const loaded: LoadedLayers = {}
  for (const [model, layers] of Object.entries(modelLayers.value)) loaded[model] = new Set(layers.map((l) => l.id))
  return { status: status.value, runPath: runPath.value, loaded, preferred: settings.defaultModel }
})
const selection = computed(() => (choices.value ? resolveSelection(choices.value, props.model, props.layer) : null))
const variableOptions = computed(() => (choices.value ? variableGroups(choices.value) : []))
const modelOptions = computed(() =>
  choices.value && selection.value ? modelChoices(choices.value, selection.value.variable) : [],
)

/** The selected layer, once its model's manifest has loaded. */
const selectedLayer = computed(() => {
  const current = selection.value
  return current ? modelLayers.value[current.model]?.find((l) => l.id === current.variable) : undefined
})
/** What the map shows: the selected layer, or the one before it while the selected model's manifest loads. */
const layer = ref<ForecastLayer>()
watch(selectedLayer, (next) => next && (layer.value = next), { immediate: true })

/** Why nothing can be shown, once the status has loaded. */
const noForecast = computed(() => {
  if (!status.value || selection.value) return ''
  if (runPath.value === IN_PROGRESS) return 'No model run is in progress with any frames yet.'
  if (runPath.value !== null) return `No forecast is available for the ${runQuery.value} run. Runs are kept for a week.`
  return 'No forecast has been exported yet.'
})
/** Retained runs, when `?run=` names one that isn't available. */
const availableRuns = computed<RunRef[]>(() =>
  noForecast.value && runPath.value !== null && runPath.value !== IN_PROGRESS
    ? (status.value?.models ?? []).flatMap((model) => model.runs)
    : [],
)
const problem = computed(() => error.value || noForecast.value)

/** The banner for a preview: which run, and how far along. */
const previewLabel = computed(() => {
  const current = layer.value
  if (!previewing.value || !current) return ''
  const total = current.hours.length
  const done = current.frames.length
  return `Previewing the ${runLabel(current.initialized)} run${done < total ? `, still coming in (${done} of ${total} frames of this layer)` : ''}.`
})

const { index, frames, frame, url, playing, loadProgress, seekHour, step } = usePlayback(
  layer,
  computed(() => settings.frameIntervalMs),
)
const hours = computed(() => frames.value.map((f) => f.hour))

function queryNumber(name: string): number | undefined {
  const value = Number(route.query[name])
  return route.query[name] !== undefined && Number.isFinite(value) ? value : undefined
}

const opacity = ref(Math.min(Math.max(queryNumber('opacity') ?? DEFAULT_OPACITY, 0), 1))
/** 3D terrain is a viewer setting (in Settings), not part of the link. */
const terrain = computed(() => settings.terrain)

// On a new layer, show the same valid time as before (the nearest frame), seeded from ?hour= on first load.
// Models' runs start at different times, so keeping the forecast hour would jump in time.
let lastView: { initialized: string; hour: number } | null = null
watch(layer, (next) => {
  if (!next) return
  seekHour((lastView ? sameValidHour(lastView.initialized, lastView.hour, next.initialized) : queryNumber('hour')) ?? 0)
})
// After the watcher above, so it reads the view before the switch.
watch([layer, () => frame.value?.hour], ([current, hour]) => {
  if (current && hour !== undefined) lastView = { initialized: current.initialized, hour }
})

// Mirror the view state into the URL (replace, so playback doesn't flood history), once the selection shows.
watch([selectedLayer, () => frame.value?.hour, opacity], ([current, hour, alpha]) => {
  if (!current || current !== layer.value) return
  void router.replace({
    name: 'map-layer',
    params: { model: current.model, layer: current.id },
    query: {
      run: runQuery.value,
      hour: hour === undefined ? undefined : String(hour),
      opacity: String(alpha),
    },
  })
})

// Admins can send the current frame to the annotation page.
const annotations = useAnnotationStore()
const annotateError = ref('')

async function annotateFrame() {
  const current = layer.value
  const hour = frame.value?.hour
  if (!current || hour === undefined) return
  annotateError.value = ''
  const added = await annotations.promote(current.model, current.id, hour)
  if (added) void router.push({ name: 'annotation-image', params: { maptype: added.maptype, image: added.image } })
  else annotateError.value = annotations.state.error || 'Could not add the frame.'
}

function show(model: string, variable: string) {
  void router.replace({ name: 'map-layer', params: { model, layer: variable }, query: route.query })
}

/** Switch variable, keeping the model if it has the variable (else the viewer's, or the config's, default). */
function selectVariable(variable: string) {
  const model = choices.value && pickModel(choices.value, variable, selection.value?.model)
  if (model) show(model.id, variable)
}

function selectModel(model: string) {
  if (selection.value) show(model, selection.value.variable)
}

function showLatest() {
  void router.replace({ query: { ...route.query, run: undefined } })
}

let abort = new AbortController()
/** Bumped by each `load`, so manifests fetched for an older status are dropped. */
let generation = 0
const pending = new Map<string, Promise<void>>()

/** Load a model's manifest for the run shown, unless it's loaded; a load already under way is shared. */
function loadModel(id: string, gen: number, signal: AbortSignal): Promise<void> {
  const current = status.value
  const model = current?.models.find((m) => m.id === id)
  const run = model ? selectRun(model, runPath.value) : null
  const key = `${gen}/${id}`
  if (!current || !model || !run || modelLayers.value[id]) return Promise.resolve()
  let loading = pending.get(key)
  if (!loading) {
    const base = dataBaseUrl(current)
    loading = fetchRunManifest(base, id, run, signal)
      .then((manifest) => {
        if (gen === generation) modelLayers.value = { ...modelLayers.value, [id]: runLayers(base, manifest, run, model) }
      })
      .finally(() => pending.delete(key))
    pending.set(key, loading)
  }
  return loading
}

/** Load the selected model's manifest, then the others', so switching models doesn't wait. */
async function loadSelected(id: string) {
  const gen = generation
  const { signal } = abort
  try {
    await loadModel(id, gen, signal)
  } catch (e) {
    if (!signal.aborted && gen === generation) error.value = e instanceof Error ? e.message : String(e)
    return
  }
  for (const model of status.value?.models ?? []) {
    loadModel(model.id, gen, signal).catch(() => {
      // Tried again, with the error shown, if the model is selected.
    })
  }
}

watch(
  () => selection.value?.model,
  (id) => id && void loadSelected(id),
)

/** Load the status, then the selected model's manifest. A refresh keeps showing the map meanwhile. */
async function load() {
  abort.abort()
  abort = new AbortController()
  const { signal } = abort
  const gen = ++generation
  error.value = ''
  try {
    if (runQuery.value !== undefined && runPath.value === null) {
      throw new Error(`"${runQuery.value}" is not a model run; use a time like 2026-10-29T00Z.`)
    }
    const current = await fetchStatus(signal)
    if (gen !== generation) return
    status.value = current
    setStatus(current)
    modelLayers.value = {}
    if (selection.value) await loadSelected(selection.value.model)
  } catch (e) {
    if (!signal.aborted) error.value = e instanceof Error ? e.message : String(e)
  }
}

watch(runQuery, () => void load())

// A preview picks up new frames as the run comes in.
let previewTimer: number | undefined
watch(
  previewing,
  (on) => {
    window.clearInterval(previewTimer)
    if (on) previewTimer = window.setInterval(() => void load(), PREVIEW_REFRESH_MS)
  },
  { immediate: true },
)

function onKeydown(event: KeyboardEvent) {
  const target = event.target as HTMLElement | null
  if (target?.closest('input, select, textarea, button')) return
  if (event.key === ' ') playing.value = !playing.value
  else if (event.key === 'ArrowLeft') step(-1)
  else if (event.key === 'ArrowRight') step(1)
  else return
  event.preventDefault()
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  void load()
})

onBeforeUnmount(() => {
  window.clearInterval(previewTimer)
  window.removeEventListener('keydown', onKeydown)
  abort.abort()
})
</script>

<template>
  <div class="map-view">
    <div v-if="!token" class="container py-4">
      <div class="alert alert-warning">
        Set <code>VITE_MAPBOX_TOKEN</code> in the repo-root <code>.env.local</code> and restart the dev server to use the map.
      </div>
    </div>
    <div v-else-if="problem" class="container py-4">
      <div class="alert alert-danger">
        <p class="mb-1">{{ problem }}</p>
        <div v-if="availableRuns.length || runQuery !== undefined" class="small">
          <template v-if="availableRuns.length">
            Available runs:
            <template v-for="(run, i) in availableRuns" :key="run.path">
              <span v-if="i">, </span>
              <RouterLink :to="{ query: { ...route.query, run: runParam(run) } }">{{ runParam(run) }}</RouterLink>
            </template>
            ·
          </template>
          <a href="#" @click.prevent="showLatest">Show the latest run</a>
        </div>
        <div v-if="status" class="small mt-1">
          <RouterLink
            v-if="!previewing && status.models.some((model) => model.in_progress)"
            :to="{ query: { ...route.query, run: IN_PROGRESS } }"
          >
            Preview the run in progress
          </RouterLink>
          <span v-if="!previewing && status.models.some((model) => model.in_progress)"> · </span>
          <a href="#" @click.prevent="openRunStatus">Model run status</a>
        </div>
        <small v-else>
          Forecast data comes from <code>{{ FORECAST_BASE_URL }}/status</code>. In development, run the local
          stack (<code>just up</code>), which the dev server proxies <code>/fx</code> to.
        </small>
      </div>
    </div>
    <template v-else-if="layer && url && selection">
      <div v-if="previewing" class="run-banner alert alert-info py-1 px-2 mb-0 small shadow-sm">
        {{ previewLabel }}
        <a href="#" class="alert-link" @click.prevent="showLatest">Show the latest complete run</a>
      </div>
      <div v-else-if="!layer.latest" class="run-banner alert alert-warning py-1 px-2 mb-0 small shadow-sm">
        Showing {{ layer.inProgress ? 'a run that is still coming in' : 'an older model run' }}.
        <a href="#" class="alert-link" @click.prevent="showLatest">Show the latest</a>
      </div>
      <ForecastMap
        :token="token"
        :layer="layer"
        :url="url"
        :values-url="frame?.valuesUrl ?? null"
        :opacity="opacity"
        :terrain="terrain"
      />
      <MapLegend :name="layer.name" :legend="layer.legend" :attribution="layer.attribution" />
      <MapControls
        v-model:index="index"
        v-model:opacity="opacity"
        v-model:playing="playing"
        :variables="variableOptions"
        :models="modelOptions"
        :selection="selection"
        :layer="layer"
        :hours="hours"
        :load-progress="loadProgress"
        @select-variable="selectVariable"
        @select-model="selectModel"
        @step="step"
      >
        <!-- only UW WRF frames (which have a maptype) are georegistered with annotations -->
        <div v-if="adminToken.token && layer.maptype">
          <button
            type="button"
            class="btn btn-sm btn-outline-secondary w-100"
            :disabled="annotations.state.busy"
            title="Download this frame from the model's latest run and open it on the annotation page"
            @click="annotateFrame"
          >
            <span v-if="annotations.state.busy" class="spinner-border spinner-border-sm me-1"></span>
            Annotate this frame
          </button>
          <div v-if="annotateError" class="small text-danger mt-1">{{ annotateError }}</div>
        </div>
      </MapControls>
    </template>
    <div v-else class="d-flex h-100 align-items-center justify-content-center text-body-secondary">
      Loading forecast…
    </div>
  </div>
</template>

<style scoped>
.map-view {
  position: absolute;
  inset: 0;
}

.run-banner {
  position: absolute;
  top: 10px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 2;
}
</style>
