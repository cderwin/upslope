<script setup lang="ts">
import { computed, ref, toRef, watch } from 'vue'
import { useBootstrapModal } from '../useBootstrapModal'
import { toKey } from '../store'
import type { AnnotationDraft, AnnotationResult, GeographicPoint } from '../types'

const props = defineProps<{
  open: boolean
  draft: AnnotationDraft | null
  points: [string, GeographicPoint][]
  /** The point key an edited annotation currently references. */
  initialPointKey: string | null
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  save: [result: AnnotationResult]
  delete: [index: number]
}>()

const element = ref<HTMLElement | null>(null)
useBootstrapModal(element, toRef(props, 'open'), () => emit('update:open', false))

const pixelX = ref(0)
const pixelY = ref(0)
const selectedKey = ref('')
const name = ref('')
const key = ref('')
const latitude = ref('')
const longitude = ref('')
const error = ref('')

const isEdit = computed(() => props.draft?.index != null)
const isNewPoint = computed(() => selectedKey.value === '')

watch(
  () => props.draft,
  (draft) => {
    if (!draft) return
    error.value = ''
    pixelX.value = draft.pixel_x
    pixelY.value = draft.pixel_y
    selectedKey.value = props.initialPointKey ?? ''
    if (!selectedKey.value) {
      name.value = ''
      key.value = ''
      latitude.value = ''
      longitude.value = ''
    } else {
      fillFromSelected()
    }
  },
)

function fillFromSelected() {
  const found = props.points.find(([k]) => k === selectedKey.value)
  if (!found) return
  const [k, point] = found
  key.value = k
  name.value = point.name
  latitude.value = String(point.latitude)
  longitude.value = String(point.longitude)
}

function onSelectPoint() {
  error.value = ''
  if (isNewPoint.value) {
    name.value = ''
    key.value = ''
    latitude.value = ''
    longitude.value = ''
  } else {
    fillFromSelected()
  }
}

function onNameInput() {
  if (isNewPoint.value) key.value = toKey(name.value)
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
    index: props.draft?.index ?? null,
    annotation: { pixel_x: x, pixel_y: y, point: selectedKey.value || toKey(key.value || trimmedName) },
    existingKey: selectedKey.value || null,
    point: { name: trimmedName, latitude: lat, longitude: lon },
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
            <select id="field-point" v-model="selectedKey" class="form-select" @change="onSelectPoint">
              <option value="">— new point —</option>
              <option v-for="[pointKey, point] in points" :key="pointKey" :value="pointKey">
                {{ point.name }} ({{ point.latitude }}, {{ point.longitude }})
              </option>
            </select>
          </div>

          <div class="mb-2">
            <label class="form-label mb-1" for="field-name">Name</label>
            <input
              id="field-name"
              v-model="name"
              type="text"
              class="form-control"
              placeholder="Cape Flattery, WA"
              required
              @input="onNameInput"
            />
          </div>

          <div class="mb-2">
            <label class="form-label mb-1" for="field-key">Key</label>
            <input
              id="field-key"
              v-model="key"
              type="text"
              class="form-control font-monospace"
              placeholder="cape_flattery"
              :readonly="!isNewPoint"
            />
            <div class="form-text">
              {{
                isNewPoint
                  ? 'YAML key under geographic_points. Auto-filled from the name.'
                  : 'Existing key; edits here update this point everywhere it is used.'
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
            @click="emit('delete', props.draft!.index!)"
          >
            Delete
          </button>
          <div class="ms-auto d-flex gap-2">
            <button type="button" class="btn btn-secondary" @click="emit('update:open', false)">
              Cancel
            </button>
            <button type="submit" class="btn btn-primary">Save</button>
          </div>
        </div>
      </form>
    </div>
  </div>
</template>
