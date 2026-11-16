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
/* The library draws with fixed light-theme colors; these follow Bootstrap's theme instead. */
.skewt-host :deep(.axis path),
.skewt-host :deep(.axis line),
.skewt-host :deep(.windbarb) {
  stroke: var(--bs-body-color);
}

.skewt-host :deep(.axis),
.skewt-host :deep(.flag),
.skewt-host :deep(.focus text),
.skewt-host :deep(.skewt-wind-arrow) {
  fill: var(--bs-body-color);
}

.skewt-host :deep(.buttons) {
  background-color: var(--bs-secondary-bg);
}

.skewt-host :deep(.buttons.clicked) {
  background-color: var(--bs-secondary);
}
</style>
