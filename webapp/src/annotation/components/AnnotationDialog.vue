<script setup lang="ts">
import { computed, ref, toRef, watch } from 'vue'
import { useBootstrapModal } from '../useBootstrapModal'
import { toKey } from '../store'
import type { AnnotationDraft, AnnotationResult, GeographicPoint } from '../types'

const props = defineProps<{
  open: boolean
  draft: AnnotationDraft | null
  points: [string, GeographicPoint][]
  /** Points already annotated on this image; picking one moves its annotation here. */
  annotatedPoints: string[]
  busy: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  save: [result: AnnotationResult]
  delete: [point: string]
}>()

const element = ref<HTMLElement | null>(null)
useBootstrapModal(element, toRef(props, 'open'), () => emit('update:open', false))

const pixelX = ref(0)
const pixelY = ref(0)
const selectedKey = ref('')
const name = ref('')
const latitude = ref('')
const longitude = ref('')
const error = ref('')

const isEdit = computed(() => props.draft?.point != null)
const isNewPoint = computed(() => selectedKey.value === '')
const keyPreview = computed(() => (isNewPoint.value ? (name.value.trim() ? toKey(name.value) : '') : selectedKey.value))

/** Picking a point that is already elsewhere on this image moves that annotation. */
const movesExisting = computed(
  () =>
    !isNewPoint.value &&
    selectedKey.value !== props.draft?.point &&
    props.annotatedPoints.includes(selectedKey.value),
)

watch(
  () => props.draft,
  (draft) => {
    if (!draft) return
    error.value = ''
    pixelX.value = draft.pixel_x
    pixelY.value = draft.pixel_y
    selectedKey.value = draft.point ?? ''
    fillFromSelected()
  },
)

function fillFromSelected() {
  const found = props.points.find(([k]) => k === selectedKey.value)
  name.value = found?.[1].name ?? ''
  latitude.value = found ? String(found[1].latitude) : ''
  longitude.value = found ? String(found[1].longitude) : ''
}

function submit() {
  const x = Number.parseInt(String(pixelX.value), 10)
  const y = Number.parseInt(String(pixelY.value), 10)
  if (!Number.isFinite(x) || !Number.isFinite(y)) {
    error.value = 'Pixel coordinates are required.'
    return
  }

  const trimmedName = name.value.trim()
  const lat = Number(latitude.value)
  const lon = Number(longitude.value)
  if (!trimmedName) {
    error.value = 'Give the point a name.'
    return
  }
  if (latitude.value === '' || !Number.isFinite(lat) || lat < -90 || lat > 90) {
    error.value = 'Latitude must be a number between -90 and 90.'
    return
  }
  if (longitude.value === '' || !Number.isFinite(lon) || lon < -180 || lon > 180) {
    error.value = 'Longitude must be a number between -180 and 180.'
    return
  }

  emit('save', {
    previousPoint: props.draft?.point ?? null,
    pointKey: selectedKey.value || null,
    point: { name: trimmedName, latitude: lat, longitude: lon },
    pixel_x: x,
    pixel_y: y,
  })
}
</script>

<template>
  <div ref="element" class="modal fade" tabindex="-1">
    <div class="modal-dialog">
      <form class="modal-content" @submit.prevent="submit">
        <div class="modal-header">
          <h5 class="modal-title">
            {{ isEdit ? 'Edit georegistered point' : 'Georegister pixel' }}
          </h5>
          <button type="button" class="btn-close" @click="emit('update:open', false)"></button>
        </div>

        <div class="modal-body">
          <div class="row g-2 mb-3">
            <div class="col-6">
              <label class="form-label mb-1" for="field-px">Pixel X</label>
              <input id="field-px" v-model="pixelX" type="number" step="1" class="form-control font-monospace" required />
            </div>
            <div class="col-6">
              <label class="form-label mb-1" for="field-py">Pixel Y</label>
              <input id="field-py" v-model="pixelY" type="number" step="1" class="form-control font-monospace" required />
            </div>
          </div>

          <div class="mb-3">
            <label class="form-label mb-1" for="field-point">Geographic point</label>
            <select id="field-point" v-model="selectedKey" class="form-select" @change="fillFromSelected">
              <option value="">— new point —</option>
              <option v-for="[pointKey, point] in points" :key="pointKey" :value="pointKey">
                {{ point.name }} ({{ point.latitude }}, {{ point.longitude }})
              </option>
            </select>
            <div v-if="movesExisting" class="form-text text-warning-emphasis">
              This point is already on this image; saving moves it here.
            </div>
          </div>

          <div class="mb-2">
            <label class="form-label mb-1" for="field-name">Name</label>
            <input id="field-name" v-model="name" type="text" class="form-control" placeholder="Cape Flattery, WA" required />
          </div>

          <div class="mb-2">
            <label class="form-label mb-1" for="field-key">Key</label>
            <input id="field-key" :value="keyPreview" type="text" class="form-control font-monospace" readonly />
            <div class="form-text">
              {{
                isNewPoint
                  ? 'Generated from the name when saved (a suffix is added if it is taken).'
                  : 'Changing the name or location updates this point everywhere it is used.'
              }}
            </div>
          </div>

          <div class="row g-2">
            <div class="col-6">
              <label class="form-label mb-1" for="field-lat">Latitude</label>
              <input id="field-lat" v-model="latitude" type="number" step="any" class="form-control font-monospace" placeholder="48.385978" required />
            </div>
            <div class="col-6">
              <label class="form-label mb-1" for="field-lon">Longitude</label>
              <input id="field-lon" v-model="longitude" type="number" step="any" class="form-control font-monospace" placeholder="-124.726914" required />
            </div>
          </div>

          <div v-if="error" class="alert alert-danger mt-3 mb-0 py-2">{{ error }}</div>
        </div>

        <div class="modal-footer justify-content-between">
          <button
            v-if="isEdit"
            type="button"
            class="btn btn-outline-danger"
            :disabled="busy"
            @click="emit('delete', props.draft!.point!)"
          >
            Remove from image
          </button>
          <div class="ms-auto d-flex gap-2">
            <button type="button" class="btn btn-secondary" @click="emit('update:open', false)">Cancel</button>
            <button type="submit" class="btn btn-primary" :disabled="busy">Save</button>
          </div>
        </div>
      </form>
    </div>
  </div>
</template>
