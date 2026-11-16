<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { stationLabel, type Station } from '../api'

/** A searchable list of stations, in the order given (nearest Seattle first). */
const props = defineProps<{ stations: Station[]; disabled?: boolean }>()
const selected = defineModel<string | null>({ required: true })

const query = ref('')
const open = ref(false)
/** Index into `matches` of the keyboard-highlighted option. */
const active = ref(0)
const list = ref<HTMLElement>()

const current = computed(() => props.stations.find((station) => station.id === selected.value) ?? null)

const matches = computed(() => {
  const words = query.value.toLowerCase().split(/\s+/).filter(Boolean)
  if (!words.length) return props.stations
  return props.stations.filter((station) => {
    const text = `${station.id} ${station.name} ${station.region ?? ''} ${station.country}`.toLowerCase()
    return words.every((word) => text.includes(word))
  })
})

watch(query, () => (active.value = 0))

function show() {
  if (props.disabled) return
  open.value = true
  query.value = ''
  active.value = Math.max(0, matches.value.findIndex((station) => station.id === selected.value))
  void scrollToActive()
}

function choose(station: Station) {
  selected.value = station.id
  open.value = false
  query.value = ''
}

function move(step: number) {
  if (!open.value) return show()
  if (!matches.value.length) return
  active.value = (active.value + step + matches.value.length) % matches.value.length
  void scrollToActive()
}

async function scrollToActive() {
  await nextTick()
  list.value?.querySelector('.active')?.scrollIntoView({ block: 'nearest' })
}

function enter() {
  const station = matches.value[active.value]
  if (open.value && station) choose(station)
}
</script>

<template>
  <div class="station-picker position-relative">
    <input
      class="form-control"
      type="search"
      role="combobox"
      aria-label="Station"
      aria-autocomplete="list"
      :aria-expanded="open"
      :disabled="disabled"
      :placeholder="current ? stationLabel(current) : 'Search stations'"
      :value="open ? query : current ? stationLabel(current) : ''"
      @focus="show"
      @click="open || show()"
      @input="query = ($event.target as HTMLInputElement).value"
      @keydown.down.prevent="move(1)"
      @keydown.up.prevent="move(-1)"
      @keydown.enter.prevent="enter"
      @keydown.esc="open = false"
      @blur="open = false"
    />
    <ul v-if="open" ref="list" class="dropdown-menu show w-100 shadow" role="listbox">
      <li v-if="!matches.length" class="dropdown-item-text text-body-secondary">No stations match.</li>
      <li v-for="(station, i) in matches" :key="station.id" role="option" :aria-selected="station.id === selected">
        <!-- mousedown, not click: it fires before the input's blur closes the list. -->
        <button
          type="button"
          class="dropdown-item d-flex justify-content-between gap-3"
          :class="{ active: i === active }"
          tabindex="-1"
          @mousedown.prevent="choose(station)"
        >
          <span class="text-truncate">{{ stationLabel(station) }}</span>
          <span class="small opacity-75 text-nowrap">{{ Math.round(station.distanceMi).toLocaleString() }} mi</span>
        </button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.dropdown-menu {
  max-height: 20rem;
  overflow-y: auto;
}
</style>
