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
        <div class="legend-steps" role="img" :aria-label="`Color scale for ${name}`">
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

.legend-swatch {
  height: 10px;
}

/* each label sits under its step's left edge, where that color starts */
.legend-label {
  font-size: 0.7rem;
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
  font-size: 0.7rem;
}
</style>
