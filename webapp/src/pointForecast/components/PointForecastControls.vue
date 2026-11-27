<script setup lang="ts">
import { Check, Layers } from '@lucide/vue'
import { WINDOWS, type ChartSpec } from '../charts'
import { modelColorVar } from '@/theme/charts'

/** The point forecast's controls: which models, how far ahead, which charts, and whether to fill between steps. */
const props = defineProps<{
  /** Every model in the forecast, in order, with its display name and why it's missing (if it is). */
  models: { id: string; name: string; missing: string | null }[]
  /** The charts this forecast has. */
  charts: ChartSpec[]
}>()

const selected = defineModel<Set<string>>('selected', { required: true })
const windowHours = defineModel<number | null>('windowHours', { required: true })
const visible = defineModel<Set<string>>('visible', { required: true })
const interpolate = defineModel<boolean>('interpolate', { required: true })
const highlight = defineModel<string | null>('highlight', { required: true })

function toggle(set: Set<string>, id: string): Set<string> {
  const next = new Set(set)
  if (!next.delete(id)) next.add(id)
  return next
}
</script>

<template>
  <div class="controls d-flex flex-wrap align-items-center gap-2">
    <div class="d-flex flex-wrap gap-1" role="group" aria-label="Models">
      <button
        v-for="model in props.models"
        :key="model.id"
        type="button"
        class="model-chip btn btn-sm d-inline-flex align-items-center gap-1"
        :class="selected.has(model.id) && !model.missing ? 'active' : ''"
        :disabled="Boolean(model.missing)"
        :aria-pressed="selected.has(model.id)"
        :title="model.missing ? `${model.name}: ${model.missing}` : `Show or hide ${model.name}`"
        @click="selected = toggle(selected, model.id)"
        @mouseenter="highlight = model.missing || !selected.has(model.id) ? null : model.id"
        @mouseleave="highlight = null"
        @focus="highlight = model.missing || !selected.has(model.id) ? null : model.id"
        @blur="highlight = null"
      >
        <span class="swatch" :style="{ background: modelColorVar(model.id) }" aria-hidden="true"></span>
        {{ model.name }}
      </button>
    </div>

    <div class="btn-group btn-group-sm ms-auto" role="group" aria-label="Time window">
      <button
        v-for="option in WINDOWS"
        :key="option.label"
        type="button"
        class="btn btn-outline-secondary"
        :class="{ active: windowHours === option.hours }"
        :aria-pressed="windowHours === option.hours"
        @click="windowHours = option.hours"
      >
        {{ option.label }}
      </button>
    </div>

    <div class="form-check form-switch mb-0 small" title="Fill the hours between each model's values">
      <input id="point-interpolate" v-model="interpolate" class="form-check-input" type="checkbox" role="switch" />
      <label class="form-check-label" for="point-interpolate">Fill hours</label>
    </div>

    <div class="dropdown">
      <button
        type="button"
        class="btn btn-sm btn-outline-secondary d-inline-flex align-items-center gap-1"
        data-bs-toggle="dropdown"
        data-bs-auto-close="outside"
        aria-expanded="false"
      >
        <Layers :size="14" aria-hidden="true" />
        Charts
      </button>
      <ul class="dropdown-menu dropdown-menu-end small-menu">
        <li v-for="chart in props.charts" :key="chart.variable">
          <button
            type="button"
            class="dropdown-item d-flex align-items-center gap-2"
            :aria-pressed="visible.has(chart.variable)"
            @click="visible = toggle(visible, chart.variable)"
          >
            <Check :size="14" aria-hidden="true" :class="{ invisible: !visible.has(chart.variable) }" />
            {{ chart.title }}
          </button>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.model-chip {
  --bs-btn-padding-y: 0.15rem;
  --bs-btn-padding-x: 0.5rem;
  --bs-btn-font-size: 12px;
  --bs-btn-font-weight: 500;
  --bs-btn-color: var(--bs-secondary-color);
  --bs-btn-border-color: var(--bs-border-color);
  --bs-btn-hover-color: var(--bs-body-color);
  --bs-btn-hover-bg: var(--bs-tertiary-bg);
  --bs-btn-hover-border-color: var(--bs-border-color);
  --bs-btn-active-color: var(--bs-body-color);
  --bs-btn-active-bg: var(--bs-secondary-bg);
  --bs-btn-active-border-color: var(--bs-secondary-color);
  --bs-btn-disabled-border-color: var(--bs-border-color);
  border-radius: 999px;
}

/* Off: a hollow swatch, so hidden models still show their color (and the chip isn't colour-only). */
.model-chip:not(.active) .swatch {
  background: transparent !important;
  box-shadow: inset 0 0 0 1.5px var(--bs-secondary-color);
}

.swatch {
  width: 0.7rem;
  height: 0.7rem;
  border-radius: 50%;
  flex-shrink: 0;
}

/* Segmented control: 12px / 500, like the chips. */
.btn-group .btn {
  --bs-btn-font-size: 12px;
  --bs-btn-font-weight: 500;
}

.small-menu {
  --bs-dropdown-min-width: 0;
  --bs-dropdown-font-size: 0.8125rem;
  --bs-dropdown-padding-y: 0.25rem;
  --bs-dropdown-item-padding-x: 0.75rem;
  --bs-dropdown-item-padding-y: 0.2rem;
}
</style>
