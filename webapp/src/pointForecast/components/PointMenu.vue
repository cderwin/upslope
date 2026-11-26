<script setup lang="ts">
import { ChartSpline, MapPin } from '@lucide/vue'
import type { LngLat } from '@/forecast/types'
import { formatLatLon } from '../units'

/** What a click on the map offers for that point: its value on the current layer, and its forecast. */
const props = defineProps<{
  lngLat: LngLat
  /** The current layer's value there, e.g. `Temperature: 28 °F`; null while loading or for layers without values. */
  value: string | null
}>()

const emit = defineEmits<{ forecast: [lngLat: LngLat] }>()
</script>

<template>
  <div class="point-menu small" role="menu" :aria-label="`Point ${formatLatLon(props.lngLat[1], props.lngLat[0])}`">
    <div class="point-menu-header">
      <div class="d-flex align-items-center gap-1 fw-semibold text-nowrap">
        <MapPin :size="14" aria-hidden="true" />
        {{ formatLatLon(props.lngLat[1], props.lngLat[0]) }}
      </div>
      <div v-if="props.value" class="text-body-secondary text-nowrap">{{ props.value }}</div>
    </div>
    <button
      type="button"
      class="dropdown-item d-flex align-items-center gap-2"
      role="menuitem"
      @click="emit('forecast', props.lngLat)"
    >
      <ChartSpline :size="16" aria-hidden="true" />
      Point forecast
    </button>
  </div>
</template>

<style scoped>
/* The map's ⋮ menu style (MapControls' .small-menu): compact, for a few short actions. */
.point-menu {
  --bs-dropdown-item-padding-x: 0.75rem;
  --bs-dropdown-item-padding-y: 0.3rem;
  min-width: 12rem;
  padding-bottom: 0.25rem;
}

.point-menu-header {
  padding: 0.5rem 0.75rem 0.4rem;
  margin-bottom: 0.25rem;
  border-bottom: var(--bs-border-width) solid var(--bs-border-color-translucent);
}
</style>
