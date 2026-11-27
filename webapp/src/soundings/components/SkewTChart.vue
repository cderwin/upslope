<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { SoundingLevel } from '../api'
import { createChart, type SkewTChart } from '../skewt'

/** A skew-T log-P diagram of one sounding. */
const props = defineProps<{ levels: SoundingLevel[] }>()

const host = ref<HTMLElement>()
const error = ref('')
let chart: SkewTChart | null = null
/** Set on unmount, so a chart that finishes loading after that is dropped. */
let unmounted = false

onMounted(async () => {
  if (!host.value) return
  try {
    const next = await createChart(host.value)
    if (unmounted) return next.destroy()
    chart = next
    chart.plot(props.levels)
  } catch (e) {
    if (!unmounted) error.value = `Could not draw the sounding: ${e instanceof Error ? e.message : String(e)}`
  }
})

watch(
  () => props.levels,
  (levels) => chart?.plot(levels),
)
onBeforeUnmount(() => {
  unmounted = true
  chart?.destroy()
})
</script>

<template>
  <div>
    <div v-if="error" class="alert alert-danger">{{ error }}</div>
    <div ref="host" class="skewt-host"></div>
  </div>
</template>

<style scoped>
/* The library draws with fixed light-theme colors; these follow the design tokens instead (see STYLE.md). */
.skewt-host :deep(.axis path),
.skewt-host :deep(.axis line),
.skewt-host :deep(.windbarb) {
  stroke: var(--up-ink);
}

.skewt-host :deep(.axis),
.skewt-host :deep(.flag),
.skewt-host :deep(.focus text),
.skewt-host :deep(.skewt-wind-arrow) {
  fill: var(--up-ink);
}

.skewt-host :deep(.buttons) {
  background-color: var(--up-raised);
  color: var(--up-ink);
}

.skewt-host :deep(.buttons.clicked) {
  background-color: var(--up-accent);
  color: var(--up-on-accent);
}

/* The sounding: temperature red and dew point blue (the usual convention), the parcel green, in the series colors. */
.skewt-host :deep(.temp) {
  stroke: var(--up-series-8);
}

.skewt-host :deep(.dwpt) {
  stroke: var(--up-series-1);
}

.skewt-host :deep(.parcel) {
  stroke: var(--up-series-6);
}

.skewt-host :deep(.elr) {
  stroke: var(--up-series-7);
}

.skewt-host :deep(.cond-level),
.skewt-host :deep(.tempzero) {
  stroke: var(--up-muted);
}

/* Background guides: faint versions of the same hues, so the sounding stands out. */
.skewt-host :deep(.templine) {
  stroke: color-mix(in oklch, var(--up-series-8) 35%, transparent);
}

.skewt-host :deep(.dryadiabat),
.skewt-host :deep(.moistadiabat) {
  stroke: color-mix(in oklch, var(--up-series-6) 35%, transparent);
}

.skewt-host :deep(.isohume) {
  stroke: color-mix(in oklch, var(--up-series-1) 35%, transparent);
}

.skewt-host :deep(.pressure) {
  stroke: var(--up-line);
}
</style>
