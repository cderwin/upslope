<script setup lang="ts">
import { computed } from 'vue'
import { runLabel as formatRun } from '@/forecast/api'
import type { ModelChoice, Selection, VariableGroup } from '@/forecast/selection'
import type { ForecastLayer } from '@/forecast/types'

const props = defineProps<{
  variables: VariableGroup[]
  models: ModelChoice[]
  selection: Selection
  layer: ForecastLayer
  hours: number[]
  loadProgress: number
}>()

const emit = defineEmits<{ selectVariable: [id: string]; selectModel: [id: string]; step: [delta: number] }>()

/** e.g. `HRRR 3km · Nov 9 12Z`, or `HRRR 3km · no run` for a model without the run being shown. */
function modelLabel(model: ModelChoice): string {
  return `${model.name} · ${model.initialized ? formatRun(model.initialized) : 'no run'}`
}

const index = defineModel<number>('index', { required: true })
const opacity = defineModel<number>('opacity', { required: true })
const playing = defineModel<boolean>('playing', { required: true })

// The forecast covers the Pacific Northwest, so times read in Pacific time wherever the viewer is.
const validFormat = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Los_Angeles',
  weekday: 'short',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  timeZoneName: 'short',
})

const initialized = computed(() => new Date(props.layer.initialized))

const offsetLabel = computed(() => {
  const hour = props.hours[index.value]
  return hour === undefined ? '' : `+${String(hour).padStart(2, '0')}h`
})

const validLabel = computed(() => {
  const hour = props.hours[index.value]
  const start = initialized.value.getTime()
  if (hour === undefined || Number.isNaN(start)) return offsetLabel.value || '—'
  return validFormat.format(new Date(start + hour * 3_600_000))
})

const runLabel = computed(() => formatRun(props.layer.initialized))

/** `41 frames`, or `12 of 41 frames` while a run is still publishing. */
const framesLabel = computed(() =>
  props.hours.length < props.layer.hours.length
    ? `${props.hours.length} of ${props.layer.hours.length} frames`
    : `${props.hours.length} frames`,
)
</script>

<template>
  <div class="map-controls card shadow-sm">
    <div class="card-body vstack gap-3">
      <div class="vstack gap-2">
        <div>
          <label class="form-label small mb-1" for="forecast-variable">Variable</label>
          <select
            id="forecast-variable"
            class="form-select form-select-sm"
            :value="selection.variable"
            @change="emit('selectVariable', ($event.target as HTMLSelectElement).value)"
          >
            <template v-for="group in variables" :key="group.name ?? ''">
              <optgroup v-if="group.name" :label="group.name">
                <option v-for="v in group.variables" :key="v.id" :value="v.id" :disabled="v.disabled">{{ v.name }}</option>
              </optgroup>
              <template v-else>
                <option v-for="v in group.variables" :key="v.id" :value="v.id" :disabled="v.disabled">{{ v.name }}</option>
              </template>
            </template>
          </select>
        </div>
        <div>
          <label class="form-label small mb-1" for="forecast-model">Model</label>
          <select
            id="forecast-model"
            class="form-select form-select-sm"
            :value="selection.model"
            @change="emit('selectModel', ($event.target as HTMLSelectElement).value)"
          >
            <option v-for="model in models" :key="model.id" :value="model.id" :disabled="model.disabled">
              {{ modelLabel(model) }}
            </option>
          </select>
        </div>
      </div>

      <div>
        <label class="form-label small mb-1 d-flex justify-content-between" for="forecast-opacity">
          <span>Opacity</span>
          <span class="text-body-secondary">{{ Math.round(opacity * 100) }}%</span>
        </label>
        <input
          id="forecast-opacity"
          v-model.number="opacity"
          class="form-range"
          type="range"
          min="0"
          max="1"
          step="0.05"
        />
      </div>

      <div>
        <label class="form-label small mb-1 d-flex justify-content-between align-items-baseline" for="forecast-hour">
          <span>Valid time</span>
          <span>
            <span class="fw-semibold">{{ validLabel }}</span>
            <span v-if="offsetLabel" class="text-body-secondary font-monospace ms-1">{{ offsetLabel }}</span>
          </span>
        </label>
        <div v-if="runLabel" class="small text-body-secondary">
          Model run {{ runLabel }}
          <span v-if="layer.inProgress && !layer.latest" class="badge text-bg-info ms-1">in progress</span>
          <span v-else-if="!layer.latest" class="badge text-bg-warning ms-1">not the latest</span>
        </div>
        <input
          id="forecast-hour"
          v-model.number="index"
          class="form-range"
          type="range"
          min="0"
          :max="Math.max(hours.length - 1, 0)"
          step="1"
        />
        <div class="d-flex align-items-center gap-2">
          <div class="btn-group btn-group-sm">
            <button type="button" class="btn btn-outline-secondary" title="Previous hour (←)" @click="emit('step', -1)">
              ‹
            </button>
            <button
              type="button"
              class="btn btn-outline-primary play-button"
              :title="playing ? 'Pause (space)' : 'Play (space)'"
              @click="playing = !playing"
            >
              {{ playing ? 'Pause' : 'Play' }}
            </button>
            <button type="button" class="btn btn-outline-secondary" title="Next hour (→)" @click="emit('step', 1)">
              ›
            </button>
          </div>
          <small class="text-body-secondary ms-auto">{{ framesLabel }}</small>
        </div>
      </div>

      <slot />

      <div
        v-if="loadProgress < 1"
        class="progress"
        role="progressbar"
        aria-label="Loading frames"
        :aria-valuenow="Math.round(loadProgress * 100)"
        aria-valuemin="0"
        aria-valuemax="100"
        style="height: 4px"
      >
        <div class="progress-bar" :style="{ width: `${loadProgress * 100}%` }"></div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.map-controls {
  position: absolute;
  top: 10px;
  left: 10px;
  z-index: 1;
  width: min(320px, calc(100% - 20px));
}

/* On phones, dock at the bottom so the map's zoom buttons stay clear. */
@media (max-width: 575.98px) {
  .map-controls {
    top: auto;
    bottom: 36px;
  }
}

.play-button {
  min-width: 4.5rem;
}
</style>
