<script setup lang="ts">
import { computed } from 'vue'
import { layerKey, layerLabel } from '@/forecast/api'
import type { ForecastLayer } from '@/forecast/types'

const props = defineProps<{
  layers: ForecastLayer[]
  layer: ForecastLayer
  hours: number[]
  loadProgress: number
}>()

const emit = defineEmits<{ selectLayer: [key: string]; step: [delta: number] }>()

const index = defineModel<number>('index', { required: true })
const opacity = defineModel<number>('opacity', { required: true })
const playing = defineModel<boolean>('playing', { required: true })
const terrain = defineModel<boolean>('terrain', { required: true })

const hourLabel = computed(() => {
  const hour = props.hours[index.value]
  return hour === undefined ? '—' : `+${String(hour).padStart(2, '0')}h`
})
</script>

<template>
  <div class="map-controls card shadow-sm">
    <div class="card-body vstack gap-3">
      <div>
        <label class="form-label small mb-1" for="forecast-layer">Domain / variable</label>
        <select
          id="forecast-layer"
          class="form-select form-select-sm"
          :value="layerKey(layer)"
          @change="emit('selectLayer', ($event.target as HTMLSelectElement).value)"
        >
          <option v-for="option in layers" :key="layerKey(option)" :value="layerKey(option)">
            {{ layerLabel(option) }}
          </option>
        </select>
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

      <div class="form-check form-switch mb-0">
        <input id="forecast-terrain" v-model="terrain" class="form-check-input" type="checkbox" role="switch" />
        <label class="form-check-label small" for="forecast-terrain">3D terrain</label>
        <div class="form-text mt-0">Ctrl-drag or right-drag to tilt and rotate.</div>
      </div>

      <div>
        <label class="form-label small mb-1 d-flex justify-content-between" for="forecast-hour">
          <span>Forecast hour</span>
          <span class="fw-semibold font-monospace">{{ hourLabel }}</span>
        </label>
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
          <small class="text-body-secondary ms-auto">{{ hours.length }} frames</small>
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
