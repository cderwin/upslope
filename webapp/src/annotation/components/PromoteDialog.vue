<script setup lang="ts">
import { computed, ref, toRef, watch } from 'vue'
import { useBootstrapModal } from '../useBootstrapModal'
import * as api from '../api'
import type { PromotableLayer } from '../types'

const props = defineProps<{
  open: boolean
  /** Preselect a layer of this maptype. */
  maptype: string
  busy: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  promote: [layer: { model: string; variable: string; hour: number }]
}>()

const element = ref<HTMLElement | null>(null)
useBootstrapModal(element, toRef(props, 'open'), () => emit('update:open', false))

const layers = ref<PromotableLayer[]>([])
const error = ref('')
const selected = ref('')
const hour = ref<number | null>(null)

const byMaptype = computed(() => {
  const groups: Record<string, PromotableLayer[]> = {}
  for (const layer of layers.value) (groups[layer.maptype] ??= []).push(layer)
  return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b))
})
const layer = computed(() => layers.value.find((l) => key(l) === selected.value))

function key(l: PromotableLayer): string {
  return `${l.model}/${l.variable}`
}

watch(
  () => props.open,
  async (isOpen) => {
    if (!isOpen) return
    error.value = ''
    try {
      if (!layers.value.length) layers.value = await api.sources()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
      return
    }
    const preferred = layers.value.find((l) => l.maptype === props.maptype) ?? layers.value[0]
    if (!layer.value && preferred) selected.value = key(preferred)
  },
)

watch(layer, (l) => {
  if (l && (hour.value === null || !l.hours.includes(hour.value))) hour.value = l.hours[0] ?? null
})

function submit() {
  if (!layer.value || hour.value === null) return
  emit('promote', { model: layer.value.model, variable: layer.value.variable, hour: hour.value })
}
</script>

<template>
  <div ref="element" class="modal fade" tabindex="-1">
    <div class="modal-dialog">
      <form class="modal-content" @submit.prevent="submit">
        <div class="modal-header">
          <h5 class="modal-title">Add a forecast image</h5>
          <button type="button" class="btn-close" @click="emit('update:open', false)"></button>
        </div>
        <div class="modal-body">
          <p class="small text-body-secondary">
            Downloads the frame from UW's latest model run and adds it to the draft for annotation.
          </p>
          <div v-if="error" class="alert alert-danger py-2">{{ error }}</div>
          <div class="mb-3">
            <label class="form-label mb-1" for="field-layer">Layer</label>
            <select id="field-layer" v-model="selected" class="form-select" required>
              <optgroup v-for="[group, groupLayers] in byMaptype" :key="group" :label="group">
                <option v-for="l in groupLayers" :key="key(l)" :value="key(l)">{{ l.model }} · {{ l.variable }}</option>
              </optgroup>
            </select>
          </div>
          <div>
            <label class="form-label mb-1" for="field-hour">Forecast hour</label>
            <select id="field-hour" v-model.number="hour" class="form-select" required>
              <option v-for="h in layer?.hours ?? []" :key="h" :value="h">{{ h }}</option>
            </select>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" @click="emit('update:open', false)">Cancel</button>
          <button type="submit" class="btn btn-primary" :disabled="busy || !layer || hour === null">
            <span v-if="busy" class="spinner-border spinner-border-sm me-1"></span>Add image
          </button>
        </div>
      </form>
    </div>
  </div>
</template>
