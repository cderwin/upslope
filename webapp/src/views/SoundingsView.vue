<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ago } from '@/forecast/api'
import { fetchLatestSounding, fetchStations, stationLabel, type Sounding, type Station } from '@/soundings/api'
import SkewTChart from '@/soundings/components/SkewTChart.vue'
import StationPicker from '@/soundings/components/StationPicker.vue'

/** Route param, so a station's sounding is a shareable URL under /soundings. */
const props = defineProps<{ station?: string }>()

const router = useRouter()

const stations = ref<Station[]>([])
const stationsError = ref('')
const sounding = ref<Sounding | null>(null)
const loading = ref(false)
const soundingError = ref('')

/** The URL's station; an absent optional param comes through as ''. */
const requested = computed(() => props.station || null)
/** The station shown: the URL's, else the nearest to Seattle. */
const stationId = computed(() => requested.value ?? stations.value[0]?.id ?? null)
const station = computed(() => stations.value.find((s) => s.id === stationId.value) ?? null)
const unknownStation = computed(() => stations.value.length > 0 && requested.value !== null && !station.value)

const selected = computed({
  get: () => stationId.value,
  set: (id) => id && void router.push({ name: 'soundings', params: { station: id } }),
})

let stationsRequest: AbortController | null = null
let soundingRequest: AbortController | null = null

onMounted(async () => {
  stationsRequest = new AbortController()
  try {
    stations.value = await fetchStations(stationsRequest.signal)
  } catch (e) {
    if (!stationsRequest.signal.aborted) stationsError.value = message(e)
  }
})

watch(
  station,
  async (next, previous) => {
    if (!next || next.id === previous?.id) return
    soundingRequest?.abort()
    const request = (soundingRequest = new AbortController())
    sounding.value = null
    soundingError.value = ''
    loading.value = true
    try {
      sounding.value = await fetchLatestSounding(next.id, request.signal)
    } catch (e) {
      if (!request.signal.aborted) soundingError.value = message(e)
    } finally {
      if (soundingRequest === request) loading.value = false
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  stationsRequest?.abort()
  soundingRequest?.abort()
})

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

const VALID_FORMAT = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Los_Angeles',
  weekday: 'short',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
  timeZoneName: 'short',
})

/** e.g. `Sun, Nov 15, 4:00 AM PST (12Z, 3 h ago)`. */
const validLabel = computed(() => {
  if (!sounding.value) return ''
  const valid = new Date(sounding.value.valid)
  const utcHour = String(valid.getUTCHours()).padStart(2, '0')
  return `${VALID_FORMAT.format(valid)} (${utcHour}Z, ${ago(sounding.value.valid)})`
})
</script>

<template>
  <div class="container py-4 soundings">
    <h1 class="h4 mb-3">Soundings</h1>

    <div v-if="stationsError" class="alert alert-danger">{{ stationsError }}</div>
    <template v-else>
      <StationPicker v-model="selected" class="mb-3" :stations="stations" :disabled="!stations.length" />

      <div v-if="!stations.length" class="text-body-secondary">Loading stations…</div>
      <div v-else-if="unknownStation" class="alert alert-warning">
        There's no active sounding station <code>{{ requested }}</code>. Pick one above.
      </div>
      <template v-else-if="station">
        <p class="text-body-secondary mb-3">
          <template v-if="sounding">
            {{ sounding.station }} launch, {{ validLabel }}
          </template>
          <template v-else>{{ stationLabel(station) }}</template>
          <template v-if="station.elevationFt !== null">
            · elevation {{ station.elevationFt.toLocaleString() }} ft
          </template>
          · {{ Math.round(station.distanceMi).toLocaleString() }} mi from Seattle
        </p>

        <div v-if="soundingError" class="alert alert-danger">{{ soundingError }}</div>
        <div v-else-if="loading" class="text-body-secondary">Loading the latest sounding…</div>
        <div v-else-if="!sounding" class="alert alert-secondary">
          No sounding from {{ station.name }} in the last two days.
        </div>
        <SkewTChart v-else :levels="sounding.levels" />

        <p class="small text-body-secondary mt-3">
          Data from the
          <a href="https://mesonet.agron.iastate.edu/archive/raob/" target="_blank" rel="noopener">
            Iowa Environmental Mesonet</a
          >.
        </p>
      </template>
    </template>
  </div>
</template>

<style scoped>
.soundings {
  max-width: 56rem;
}
</style>
