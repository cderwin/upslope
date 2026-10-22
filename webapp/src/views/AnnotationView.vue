<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { adminToken, requestToken } from '@/admin/token'
import AnnotationDialog from '@/annotation/components/AnnotationDialog.vue'
import AnnotationTables from '@/annotation/components/AnnotationTables.vue'
import ImageStage from '@/annotation/components/ImageStage.vue'
import { imageUrl } from '@/annotation/api'
import { useAnnotationStore } from '@/annotation/store'
import type { AnnotationDraft, AnnotationResult } from '@/annotation/types'

/** Route params, so a selected image is a shareable URL under /annotation. */
const props = defineProps<{ maptype?: string; image?: string }>()

const router = useRouter()
const store = useAnnotationStore()
const busy = computed(() => store.state.busy)

const maptype = computed(() => props.maptype ?? store.maptypes.value[0] ?? '')
const images = computed(() => store.images(maptype.value))
const image = computed(() => props.image ?? images.value[0] ?? '')
const imageInfo = computed(() => store.image(maptype.value, image.value))

const zoomChoice = ref<'fit' | '1' | '2' | '4'>('1')
const zoom = computed(() => (zoomChoice.value === 'fit' ? 'fit' : Number(zoomChoice.value)))
const showLabels = ref(true)

const status = ref('')
const src = ref('')
const annotations = computed(() => store.annotationsFor(maptype.value, image.value))
const annotatedPoints = computed(() => annotations.value.map((a) => a.point))
const pointRows = computed(() =>
  store.sortedPoints.value.map(([key, point]) => ({ key, point, usage: store.usageCount(key) })),
)
const fit = computed(() => store.state.fit[maptype.value])

const dialogOpen = ref(false)
const draft = ref<AnnotationDraft | null>(null)

/** Keep the URL as the single source of truth for the selection. */
function select(nextMaptype: string, nextImage: string | undefined) {
  const target = nextImage ?? store.images(nextMaptype)[0]
  void router.replace(
    target
      ? { name: 'annotation-image', params: { maptype: nextMaptype, image: target } }
      : { name: 'annotation' },
  )
}

// Load the selected image through the authenticated API.
watch(
  [maptype, image],
  async ([m, i]) => {
    status.value = ''
    src.value = ''
    if (!m || !i || !store.image(m, i)) return
    try {
      const url = await imageUrl(m, i)
      if (m === maptype.value && i === image.value) src.value = url
    } catch (error) {
      status.value = `Could not load ${m}/${i}: ${error instanceof Error ? error.message : error}`
    }
  },
  { immediate: true },
)

function describeSource(imageId: string): string {
  const source = store.image(maptype.value, imageId)?.source
  if (!source) return imageId
  return `${source.variable} · hour ${source.hour} · run ${source.initialized.slice(0, 13).replace('T', ' ')}Z`
}

function openForPixel(x: number, y: number) {
  if (busy.value) return
  draft.value = { point: null, pixel_x: x, pixel_y: y }
  dialogOpen.value = true
}

function openForPoint(point: string) {
  const existing = annotations.value.find((a) => a.point === point)
  if (!existing) return
  draft.value = { point, pixel_x: existing.pixel_x, pixel_y: existing.pixel_y }
  dialogOpen.value = true
}

async function onSave(result: AnnotationResult) {
  if (await store.saveAnnotation(maptype.value, image.value, result)) dialogOpen.value = false
}

async function onDelete(point: string) {
  await store.removeAnnotation(maptype.value, image.value, point)
  dialogOpen.value = false
}

async function onRemovePoint(key: string) {
  const point = store.points.value[key]
  if (!point) return
  const usage = store.usageCount(key)
  const message = usage
    ? `Delete "${point.name}" and its ${usage} annotation(s) on every image?`
    : `Delete "${point.name}"?`
  if (window.confirm(message)) await store.removePoint(key)
}

async function onRemoveImage() {
  const count = annotations.value.length
  const message = `Remove ${image.value} from ${maptype.value}${count ? `, with its ${count} annotation(s)` : ''}?`
  if (!window.confirm(message)) return
  await store.removeImage(maptype.value, image.value)
  select(maptype.value, undefined)
}

function onImageLoad(width: number, height: number) {
  status.value = `${image.value} — ${width} x ${height} px`
}

function onImageError() {
  status.value = `Could not display ${maptype.value}/${image.value}`
}

function formatMeters(meters: number | null | undefined): string {
  if (meters == null) return '—'
  return meters >= 1000 ? `${(meters / 1000).toFixed(2)} km` : `${Math.round(meters)} m`
}

// Load once a token is available, and again whenever a new one is entered.
watch(
  () => adminToken.version,
  () => void store.load(),
)
onMounted(() => {
  if (adminToken.token) void store.load()
  else requestToken('missing')
})
</script>

<template>
  <div class="container-fluid px-3 px-md-4 px-xl-5 py-4">
    <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
      <h1 class="h4 mb-0">Georegistration</h1>
      <div class="d-flex flex-wrap align-items-center gap-2">
        <span v-if="store.state.document" class="small text-body-secondary">
          Draft r{{ store.state.document.revision }}
        </span>
        <span v-if="busy" class="spinner-border spinner-border-sm text-secondary" role="status"></span>
      </div>
    </div>

    <div v-if="store.state.notice" class="alert alert-info alert-dismissible py-2">
      {{ store.state.notice }}
      <button type="button" class="btn-close" @click="store.clearMessages()"></button>
    </div>
    <div v-if="store.state.error" class="alert alert-danger alert-dismissible py-2">
      {{ store.state.error }}
      <ul v-if="store.state.problems.length" class="mb-0 mt-1 small">
        <li v-for="problem in store.state.problems" :key="problem">{{ problem }}</li>
      </ul>
      <button type="button" class="btn-close" @click="store.clearMessages()"></button>
    </div>

    <div v-if="!store.state.document" class="text-body-secondary">
      <template v-if="store.state.loading">Loading annotations…</template>
      <template v-else-if="!adminToken.token">
        Annotations need the admin token.
        <button type="button" class="btn btn-sm btn-primary ms-2" @click="requestToken('missing')">Enter token</button>
      </template>
    </div>

    <div v-else class="row g-3">
      <div class="col-lg-8">
        <div class="row g-2 align-items-end mb-2">
          <div class="col-sm-3">
            <label class="form-label mb-1" for="sel-maptype">Maptype</label>
            <select
              id="sel-maptype"
              class="form-select form-select-sm"
              :value="maptype"
              @change="select(($event.target as HTMLSelectElement).value, undefined)"
            >
              <option v-for="name in store.maptypes.value" :key="name" :value="name">{{ name }}</option>
            </select>
          </div>
          <div class="col-sm-5">
            <label class="form-label mb-1" for="sel-image">Image</label>
            <div class="input-group input-group-sm">
              <select
                id="sel-image"
                class="form-select form-select-sm"
                :value="image"
                @change="select(maptype, ($event.target as HTMLSelectElement).value)"
              >
                <option v-for="name in images" :key="name" :value="name">{{ describeSource(name) }}</option>
              </select>
              <button
                type="button"
                class="btn btn-outline-danger"
                title="Remove this image from the draft"
                :disabled="busy || !imageInfo"
                @click="onRemoveImage"
              >
                ×
              </button>
            </div>
          </div>
          <div class="col-sm-2">
            <label class="form-label mb-1" for="sel-zoom">Zoom</label>
            <select id="sel-zoom" v-model="zoomChoice" class="form-select form-select-sm">
              <option value="fit">Fit width</option>
              <option value="1">100%</option>
              <option value="2">200%</option>
              <option value="4">400%</option>
            </select>
          </div>
          <div class="col-sm-2">
            <div class="form-check">
              <input id="chk-labels" v-model="showLabels" class="form-check-input" type="checkbox" />
              <label class="form-check-label" for="chk-labels">Labels</label>
            </div>
          </div>
        </div>

        <ImageStage
          v-if="imageInfo && src"
          :src="src"
          :annotations="annotations"
          :points="store.points.value"
          :zoom="zoom"
          :show-labels="showLabels"
          @pick="openForPixel"
          @edit="openForPoint"
          @load="onImageLoad"
          @error="onImageError"
        />
        <div v-else-if="!imageInfo" class="alert alert-warning">No images for this maptype yet.</div>
        <div v-else class="text-body-secondary small py-4">Loading image…</div>

        <div class="d-flex justify-content-between mt-2 small text-body-secondary">
          <span class="font-monospace">{{ status }}</span>
          <span>Click the image to georegister a pixel. Click a marker to edit it.</span>
        </div>
      </div>

      <div class="col-lg-4">
        <div class="card mb-3">
          <div class="card-body py-2">
            <div class="d-flex justify-content-between align-items-baseline">
              <h2 class="h6 mb-1">Fit for {{ maptype }}</h2>
              <span
                v-if="fit && fit.mean_error_m != null"
                class="badge"
                :class="fit.over_threshold ? 'text-bg-warning' : 'text-bg-success'"
              >
                {{ fit.over_threshold ? 'over threshold' : 'ok' }}
              </span>
            </div>
            <div v-if="fit?.error" class="small text-danger">{{ fit.error }} (has {{ fit.control_points }})</div>
            <div v-else-if="fit" class="small">
              Mean error <strong>{{ formatMeters(fit.mean_error_m) }}</strong> from {{ fit.control_points }} points;
              threshold {{ formatMeters(fit.fit_threshold_m) }}.
            </div>
            <div v-else class="small text-body-secondary">No fit yet.</div>
          </div>
        </div>

        <AnnotationTables
          :annotations="annotations"
          :points="store.points.value"
          :point-rows="pointRows"
          :point-errors="fit?.point_errors ?? {}"
          :busy="busy"
          @edit="openForPoint"
          @remove="(point) => store.removeAnnotation(maptype, image, point)"
          @remove-point="onRemovePoint"
        />
      </div>
    </div>

    <AnnotationDialog
      v-model:open="dialogOpen"
      :draft="draft"
      :points="store.sortedPoints.value"
      :annotated-points="annotatedPoints"
      :busy="busy"
      @save="onSave"
      @delete="onDelete"
    />
  </div>
</template>
