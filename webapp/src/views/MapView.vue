<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ForecastMap from '@/forecast/components/ForecastMap.vue'
import MapControls from '@/forecast/components/MapControls.vue'
import {
  FORECAST_BASE_URL,
  fetchRunManifest,
  fetchStatus,
  layerKey,
  runLayers,
  runParam,
  runPathFromParam,
  selectRun,
} from '@/forecast/api'
import type { ForecastLayer, RunRef } from '@/forecast/types'
import { usePlayback } from '@/forecast/usePlayback'
import { adminToken } from '@/admin/token'
import { useAnnotationStore } from '@/annotation/store'
import { settings } from '@/settings/settings'

/** Route params, so the selected layer is a shareable URL under /map. */
const props = defineProps<{ model?: string; layer?: string }>()

const DEFAULT_OPACITY = 0.7

const route = useRoute()
const router = useRouter()
const token = import.meta.env.VITE_MAPBOX_TOKEN ?? ''

const layers = ref<ForecastLayer[]>([])
const error = ref('')
/** Retained runs, when `?run=` names one that isn't available. */
const availableRuns = ref<RunRef[]>([])

/** `?run=`: a run other than the latest, e.g. `2026-10-29T00Z`. */
const runQuery = computed(() => (typeof route.query.run === 'string' ? route.query.run : undefined))

const layer = computed(
  () => layers.value.find((l) => l.model === props.model && l.id === props.layer) ?? layers.value[0],
)

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
const terrain = ref(route.query.terrain === '1')

// Keep the hour when switching layers (nearest available), seeded from ?hour= on first load.
// `index` still points into the previous layer's frames when this runs.
watch(layer, (_next, previous) => {
  const hour = previous ? previous.frames[index.value]?.hour : queryNumber('hour')
  seekHour(hour ?? 0)
})

// Mirror the view state into the URL (replace, so playback doesn't flood history).
watch([layer, () => frame.value?.hour, opacity, terrain], ([current, hour, alpha, is3d]) => {
  if (!current) return
  void router.replace({
    name: 'map-layer',
    params: { model: current.model, layer: current.id },
    query: {
      run: runQuery.value,
      hour: hour === undefined ? undefined : String(hour),
      opacity: String(alpha),
      terrain: is3d ? '1' : undefined,
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

function selectLayer(key: string) {
  const next = layers.value.find((l) => layerKey(l) === key)
  if (!next) return
  void router.replace({
    name: 'map-layer',
    params: { model: next.model, layer: next.id },
    query: route.query,
  })
}

function showLatest() {
  void router.replace({ query: { ...route.query, run: undefined } })
}

let abort = new AbortController()

/** Load each model's latest run, or the run `?run=` names. */
async function load() {
  abort.abort()
  abort = new AbortController()
  const { signal } = abort
  error.value = ''
  availableRuns.value = []
  try {
    const runPath = runQuery.value === undefined ? null : runPathFromParam(runQuery.value)
    if (runQuery.value !== undefined && runPath === null) {
      throw new Error(`"${runQuery.value}" is not a model run; use a time like 2026-10-29T00Z.`)
    }
    const status = await fetchStatus(signal)
    const selected = status.models.flatMap((model) => {
      const run = selectRun(model, runPath)
      return run ? [{ model, run }] : []
    })
    const manifests = await Promise.all(selected.map(({ model, run }) => fetchRunManifest(model.id, run, signal)))
    const next = manifests.flatMap((manifest, i) => runLayers(manifest, selected[i].run, selected[i].model.latest))
    if (signal.aborted) return
    layers.value = next
    if (!next.length && runPath !== null) {
      availableRuns.value = status.models.flatMap((model) => model.runs)
      error.value = `No forecast is available for the ${runQuery.value} run. Runs are kept for a week.`
    } else if (!next.length) {
      error.value = 'No forecast has been exported yet.'
    }
  } catch (e) {
    if (!signal.aborted) error.value = e instanceof Error ? e.message : String(e)
  }
}

watch(runQuery, () => void load())

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
    <div v-else-if="error" class="container py-4">
      <div class="alert alert-danger">
        <p class="mb-1">{{ error }}</p>
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
        <small v-else>
          Forecast data comes from <code>{{ FORECAST_BASE_URL }}/status</code>. In development, run the local
          stack (<code>just up</code>), which the dev server proxies <code>/fx</code> to.
        </small>
      </div>
    </div>
    <template v-else-if="layer && url">
      <div v-if="!layer.latest" class="run-banner alert alert-warning py-1 px-2 mb-0 small shadow-sm">
        Showing an older model run.
        <a href="#" class="alert-link" @click.prevent="showLatest">Show the latest</a>
      </div>
      <ForecastMap :token="token" :layer="layer" :url="url" :opacity="opacity" :terrain="terrain" />
      <MapControls
        v-model:index="index"
        v-model:opacity="opacity"
        v-model:playing="playing"
        v-model:terrain="terrain"
        :layers="layers"
        :layer="layer"
        :hours="hours"
        :load-progress="loadProgress"
        @select-layer="selectLayer"
        @step="step"
      >
        <div v-if="adminToken.token">
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
