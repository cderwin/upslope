<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ForecastMap from '@/forecast/components/ForecastMap.vue'
import MapControls from '@/forecast/components/MapControls.vue'
import { FORECAST_BASE_URL, fetchManifest, layerKey } from '@/forecast/api'
import type { ForecastManifest } from '@/forecast/types'
import { usePlayback } from '@/forecast/usePlayback'
import { adminToken } from '@/admin/token'
import { useAnnotationStore } from '@/annotation/store'

/** Route params, so the selected layer is a shareable URL under /map. */
const props = defineProps<{ model?: string; variable?: string }>()

const DEFAULT_OPACITY = 0.7

const route = useRoute()
const router = useRouter()
const token = import.meta.env.VITE_MAPBOX_TOKEN ?? ''

const manifest = ref<ForecastManifest | null>(null)
const error = ref('')

const layers = computed(() => manifest.value?.layers ?? [])
const layer = computed(
  () =>
    layers.value.find((l) => l.model === props.model && l.variable === props.variable) ?? layers.value[0],
)

const { index, frames, frame, url, playing, loadProgress, seekHour, step } = usePlayback(layer)
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
    params: { model: current.model, variable: current.variable },
    query: {
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
  const added = await annotations.promote(current.model, current.variable, hour)
  if (added) void router.push({ name: 'annotation-image', params: { maptype: added.maptype, image: added.image } })
  else annotateError.value = annotations.state.error || 'Could not add the frame.'
}

function selectLayer(key: string) {
  const next = layers.value.find((l) => layerKey(l) === key)
  if (!next) return
  void router.replace({
    name: 'map-layer',
    params: { model: next.model, variable: next.variable },
    query: route.query,
  })
}

function onKeydown(event: KeyboardEvent) {
  const target = event.target as HTMLElement | null
  if (target?.closest('input, select, textarea, button')) return
  if (event.key === ' ') playing.value = !playing.value
  else if (event.key === 'ArrowLeft') step(-1)
  else if (event.key === 'ArrowRight') step(1)
  else return
  event.preventDefault()
}

const abort = new AbortController()
onMounted(async () => {
  window.addEventListener('keydown', onKeydown)
  try {
    manifest.value = await fetchManifest(abort.signal)
    if (!manifest.value.layers.length) error.value = 'The forecast manifest lists no layers.'
  } catch (e) {
    if (!abort.signal.aborted) error.value = e instanceof Error ? e.message : String(e)
  }
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
        <small>
          Expected <code>{{ FORECAST_BASE_URL }}/manifest.json</code>. In development, put an
          <code>upslope export-web</code> export (manifest and frames) in <code>forecast/web_export/</code>.
        </small>
      </div>
    </div>
    <template v-else-if="layer && url">
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
            title="Download this frame from UW's latest run and open it on the annotation page"
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
</style>
