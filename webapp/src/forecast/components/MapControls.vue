<script setup lang="ts">
import { EllipsisVertical, Maximize2, Minimize2, Minus, Plus } from '@lucide/vue'
import { computed } from 'vue'
import { runLabel as formatRun } from '@/forecast/api'
import type { ModelChoice, Selection, VariableGroup } from '@/forecast/selection'
import type { ForecastLayer } from '@/forecast/types'
import PlaybackButtons from './PlaybackButtons.vue'
import { setControlsCollapsed, settings } from '@/settings/settings'

const props = defineProps<{
  variables: VariableGroup[]
  models: ModelChoice[]
  selection: Selection
  layer: ForecastLayer
  hours: number[]
  loadProgress: number
  /** 0–1 */
  opacity: number
  /** The ⋮ menu has items only on phones (where it stands in for the navbar), so hide it on larger screens. */
  menuPhoneOnly?: boolean
}>()

const emit = defineEmits<{
  selectVariable: [id: string]
  selectModel: [id: string]
  /** Move along the timeline by this many frames. */
  step: [delta: number]
  /** Raise (+1) or lower (-1) the overlay's opacity by one step. */
  stepOpacity: [delta: 1 | -1]
}>()

/** e.g. `HRRR 3km · Nov 9 12Z`, or `HRRR 3km · no run` for a model without the run being shown. */
function modelLabel(model: ModelChoice): string {
  return `${model.name} · ${model.initialized ? formatRun(model.initialized) : 'no run'}`
}

const index = defineModel<number>('index', { required: true })
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

/** A run still publishing: only some of its frames can be shown. */
const partial = computed(() => props.hours.length < props.layer.hours.length)

/** `41 frames`, or `12 of 41 frames` while a run is still publishing. */
const framesLabel = computed(() =>
  partial.value ? `${props.hours.length} of ${props.layer.hours.length} frames` : `${props.hours.length} frames`,
)
</script>

<template>
  <div class="map-controls card shadow-sm" :class="{ collapsed: settings.controlsCollapsed }">
    <div class="card-body vstack gap-2">
      <!-- Collapsed: just where playback is, and the way back. -->
      <div v-if="settings.controlsCollapsed" class="d-flex align-items-center gap-2">
        <button
          type="button"
          class="btn btn-sm btn-link text-body-secondary border icon-button corner-button"
          title="Show the controls"
          aria-label="Show the controls"
          @click="setControlsCollapsed(false)"
        >
          <Maximize2 :size="14" aria-hidden="true" />
        </button>
        <PlaybackButtons v-model:playing="playing" @step="(delta) => emit('step', delta)" />
        <span class="small min-width-0">
          <span class="fw-semibold">{{ validLabel }}</span>
          <span v-if="offsetLabel" class="text-body-secondary tnum text-nowrap ms-1">{{ offsetLabel }}</span>
        </span>
      </div>

      <template v-else>
        <div class="d-flex align-items-center gap-1">
          <button
            type="button"
            class="btn btn-sm btn-link text-body-secondary border icon-button corner-button"
            title="Hide the controls"
            aria-label="Hide the controls"
            @click="setControlsCollapsed(true)"
          >
            <Minimize2 :size="14" aria-hidden="true" />
          </button>
          <select
            class="form-select form-select-sm min-width-0"
            aria-label="Variable"
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
          <div v-if="$slots.menu" class="dropdown" :class="{ 'd-sm-none': menuPhoneOnly }">
            <button
              type="button"
              class="btn btn-sm btn-link text-body-secondary border icon-button corner-button"
              data-bs-toggle="dropdown"
              aria-expanded="false"
              title="More actions"
              aria-label="More actions"
            >
              <EllipsisVertical :size="16" aria-hidden="true" />
            </button>
            <ul class="dropdown-menu dropdown-menu-end small-menu">
              <slot name="menu" />
            </ul>
          </div>
        </div>
        <select
          class="form-select form-select-sm"
          aria-label="Model"
          :value="selection.model"
          @change="emit('selectModel', ($event.target as HTMLSelectElement).value)"
        >
          <option v-for="model in models" :key="model.id" :value="model.id" :disabled="model.disabled">
            {{ modelLabel(model) }}
          </option>
        </select>

        <!-- Buttons, not a slider, so it can't be mistaken for the timeline. -->
        <div class="d-flex align-items-center gap-1 small">
          <span class="text-body-secondary">Opacity</span>
          <button
            type="button"
            class="btn btn-sm btn-outline-secondary icon-button step-button ms-auto"
            title="Less opaque ([)"
            aria-label="Less opaque"
            :disabled="opacity <= 0"
            @click="emit('stepOpacity', -1)"
          >
            <Minus :size="14" aria-hidden="true" />
          </button>
          <span class="opacity-value text-center" aria-live="polite">{{ Math.round(opacity * 100) }}%</span>
          <button
            type="button"
            class="btn btn-sm btn-outline-secondary icon-button step-button"
            title="More opaque (])"
            aria-label="More opaque"
            :disabled="opacity >= 1"
            @click="emit('stepOpacity', 1)"
          >
            <Plus :size="14" aria-hidden="true" />
          </button>
        </div>

        <hr class="my-0 full-bleed" />

        <!-- The run is in the model picker's label; only a run other than the latest needs calling out. -->
        <div class="small d-flex flex-wrap align-items-center column-gap-1">
          <span class="fw-semibold">{{ validLabel }}</span>
          <span v-if="offsetLabel" class="text-body-secondary tnum">{{ offsetLabel }}</span>
          <span v-if="layer.inProgress && !layer.latest" class="badge text-bg-info">in progress</span>
          <span v-else-if="!layer.latest" class="badge text-bg-warning">not the latest</span>
          <span v-if="partial" class="text-body-secondary">· {{ framesLabel }}</span>
        </div>
        <div class="d-flex align-items-center gap-2">
          <PlaybackButtons v-model:playing="playing" @step="(delta) => emit('step', delta)" />
          <input
            v-model.number="index"
            class="form-range min-width-0"
            type="range"
            aria-label="Forecast hour"
            :title="framesLabel"
            min="0"
            :max="Math.max(hours.length - 1, 0)"
            step="1"
          />
        </div>
      </template>

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

/* Collapsed, the panel is one row: as wide as that row. */
.map-controls.collapsed {
  width: auto;
  max-width: calc(100% - 20px);
}

/* Flex items default to their content's width; let selects and the slider shrink in a narrow panel. */
.min-width-0 {
  min-width: 0;
}

/* Divider from edge to edge of the card, through the body's padding. */
.full-bleed {
  margin-inline: calc(-1 * var(--bs-card-spacer-x));
}

/* On phones, dock at the bottom so the map's zoom buttons stay clear, above the scale bar (bottom right). */
@media (max-width: 575.98px) {
  .map-controls {
    top: auto;
    bottom: 84px;
  }
}

/* Center the icon in the button; an inline SVG otherwise sits on the text baseline. */
.icon-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  padding: 0;
}

.step-button {
  width: 1.75rem;
  height: 1.5rem;
}

.opacity-value {
  min-width: 2.75rem;
}

/* Collapse/expand (top left) and the ⋮ menu (top right): squares as tall as the small selects beside it
   (Bootstrap's .form-select-sm and .btn-sm height, at the same 0.875rem font size). */
.corner-button {
  --size: calc(1.5em + 0.5rem + 2 * var(--bs-border-width));
  font-size: 0.875rem;
  width: var(--size);
  height: var(--size);
}

/* The ⋮ menu holds a few short actions; Bootstrap's default menu is roomy for that. */
.small-menu {
  --bs-dropdown-min-width: 0;
  --bs-dropdown-font-size: 0.8125rem;
  --bs-dropdown-padding-y: 0.25rem;
  --bs-dropdown-item-padding-x: 0.75rem;
  --bs-dropdown-item-padding-y: 0.2rem;
}
</style>
