<script setup lang="ts">
import { computed } from 'vue'
import { formatValue } from '@/forecast/frameValues'
import type { Legend } from '@/forecast/types'

const props = defineProps<{
  /** Layer name, for the title. */
  name: string
  legend: Legend | null
  attribution: string | null
}>()

/** A continuous legend: the stops as a gradient, labelled at its ticks, placed by value. */
const gradient = computed(() => {
  const legend = props.legend
  if (!legend?.continuous || legend.stops.length < 2) return null
  const first = legend.stops[0].value
  const span = legend.stops[legend.stops.length - 1].value - first || 1
  const at = (value: number) => ((value - first) / span) * 100
  return {
    background: `linear-gradient(to right, ${legend.stops.map((stop) => `${stop.color} ${at(stop.value)}%`).join(', ')})`,
    ticks: (legend.ticks ?? legend.stops.map((stop) => stop.value)).map((value) => ({
      left: at(value),
      label: formatValue(value, { units: '', decimals: legend.decimals }),
    })),
  }
})

/** One labeled step per stop; each color applies from its value up to the next. */
const steps = computed(() =>
  (props.legend?.stops ?? []).map((stop) => ({
    color: stop.color,
    label: formatValue(stop.value, { units: '', decimals: props.legend?.decimals ?? 0 }),
  })),
)
</script>

<template>
  <div v-if="legend || attribution" class="map-legend card shadow-sm small">
    <div class="card-body p-2">
      <template v-if="legend">
        <div class="fw-medium mb-1">{{ name }} ({{ legend.units }})</div>
        <div v-if="gradient" class="legend-gradient" role="img" :aria-label="`Color scale for ${name}`">
          <div class="legend-ramp" :style="{ background: gradient.background }"></div>
          <div class="legend-ticks">
            <span v-for="tick in gradient.ticks" :key="tick.left" class="legend-tick tnum" :style="{ left: `${tick.left}%` }">
              {{ tick.label }}
            </span>
          </div>
        </div>
        <div v-else class="legend-steps" role="img" :aria-label="`Color scale for ${name}`">
          <div v-for="(step, i) in steps" :key="i" class="legend-step">
            <div class="legend-swatch" :style="{ background: step.color }"></div>
            <div class="legend-label">{{ step.label }}</div>
          </div>
        </div>
      </template>
      <div v-if="attribution" class="attribution text-body-secondary" :class="{ 'mt-1': legend }">
        {{ attribution }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.map-legend {
  position: absolute;
  bottom: 36px;
  left: 10px;
  z-index: 1;
  width: min(360px, calc(100% - 20px));
}

/* On phones the controls dock at the bottom, so the legend goes to the top. */
@media (max-width: 575.98px) {
  .map-legend {
    bottom: auto;
    top: 10px;
  }
}

.legend-steps {
  display: flex;
}

.legend-step {
  flex: 1 1 0;
  min-width: 0;
}

.legend-swatch,
.legend-ramp {
  height: 10px;
}

.legend-ticks {
  position: relative;
  height: 1.2em;
}

/* centred on its value, except at the ends, which stay inside the legend */
.legend-tick {
  position: absolute;
  top: 0;
  font-size: 11px;
  line-height: 1.2;
  white-space: nowrap;
  color: var(--up-muted);
  transform: translateX(-50%);
}

.legend-tick:first-child {
  transform: none;
}

.legend-tick:last-child {
  transform: translateX(-100%);
}

/* each label sits under its step's left edge, where that color starts */
.legend-label {
  font-size: 11px;
  color: var(--up-muted);
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
  white-space: nowrap;
  transform: translateX(-2px);
}

.legend-step:nth-child(even) .legend-label {
  visibility: hidden;
}

.legend-steps:has(.legend-step:nth-child(-n + 8):last-child) .legend-label {
  visibility: visible;
}

.attribution {
  font-size: 11px;
}
</style>
