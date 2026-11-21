<script setup lang="ts">
import { ChevronLeft, ChevronRight, Pause, Play } from '@lucide/vue'

/** Previous hour, play/pause and next hour, as one button group. */
const emit = defineEmits<{ step: [delta: number] }>()
const playing = defineModel<boolean>('playing', { required: true })
</script>

<template>
  <div class="btn-group btn-group-sm flex-shrink-0">
    <button type="button" class="btn btn-outline-secondary" title="Previous hour (←)" aria-label="Previous hour" @click="emit('step', -1)">
      <ChevronLeft :size="14" aria-hidden="true" />
    </button>
    <button
      type="button"
      class="btn btn-outline-primary"
      :title="playing ? 'Pause (space)' : 'Play (space)'"
      :aria-label="playing ? 'Pause' : 'Play'"
      @click="playing = !playing"
    >
      <component :is="playing ? Pause : Play" :size="14" aria-hidden="true" />
    </button>
    <button type="button" class="btn btn-outline-secondary" title="Next hour (→)" aria-label="Next hour" @click="emit('step', 1)">
      <ChevronRight :size="14" aria-hidden="true" />
    </button>
  </div>
</template>
