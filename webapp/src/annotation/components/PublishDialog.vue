<script setup lang="ts">
import { computed, ref, toRef, watch } from 'vue'
import { useBootstrapModal } from '../useBootstrapModal'
import { useAnnotationStore } from '../store'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()

const element = ref<HTMLElement | null>(null)
useBootstrapModal(element, toRef(props, 'open'), () => emit('update:open', false))

const store = useAnnotationStore()
const note = ref('')
/** Warnings from a finished publish; `null` until then. */
const warnings = ref<string[] | null>(null)

const fits = computed(() => Object.entries(store.state.fit).sort(([a], [b]) => a.localeCompare(b)))

watch(
  () => props.open,
  (isOpen) => {
    if (!isOpen) return
    note.value = ''
    warnings.value = null
  },
)

function formatMeters(meters: number | null): string {
  if (meters == null) return '—'
  return meters >= 1000 ? `${(meters / 1000).toFixed(2)} km` : `${Math.round(meters)} m`
}

async function submit() {
  const result = await store.publish(note.value)
  if (result === undefined) emit('update:open', false) // the error shows on the page
  else warnings.value = result
}
</script>

<template>
  <div ref="element" class="modal fade" tabindex="-1">
    <div class="modal-dialog modal-lg">
      <form class="modal-content" @submit.prevent="submit">
        <div class="modal-header">
          <h5 class="modal-title">Publish annotations</h5>
          <button type="button" class="btn-close" @click="emit('update:open', false)"></button>
        </div>

        <div v-if="warnings !== null" class="modal-body">
          <div class="alert alert-success py-2">
            Published r{{ store.state.publishedRevision }}. The next hourly export uses it.
          </div>
          <div v-if="warnings.length" class="alert alert-warning py-2 mb-0">
            <div class="fw-semibold">Fit warnings</div>
            <ul class="mb-0 small">
              <li v-for="warning in warnings" :key="warning">{{ warning }}</li>
            </ul>
          </div>
        </div>

        <div v-else class="modal-body">
          <p class="small text-body-secondary">
            Publishes draft r{{ store.state.document?.revision }}
            (now published: {{ store.state.publishedRevision === null ? 'nothing' : `r${store.state.publishedRevision}` }}).
            The next hourly export georegisters with it.
          </p>
          <table class="table table-sm align-middle">
            <thead>
              <tr><th>Maptype</th><th>Points</th><th>Mean error</th><th>Threshold</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-for="[maptype, fit] in fits" :key="maptype">
                <td>{{ maptype }}</td>
                <td>{{ fit.control_points }}</td>
                <td class="font-monospace">{{ formatMeters(fit.mean_error_m) }}</td>
                <td class="font-monospace">{{ formatMeters(fit.fit_threshold_m) }}</td>
                <td>
                  <span v-if="fit.error" class="badge text-bg-danger">can't fit</span>
                  <span v-else-if="fit.over_threshold" class="badge text-bg-warning">over threshold</span>
                  <span v-else class="badge text-bg-success">ok</span>
                </td>
              </tr>
            </tbody>
          </table>
          <label class="form-label mb-1" for="field-note">Note (optional)</label>
          <input id="field-note" v-model="note" type="text" class="form-control" placeholder="What changed" maxlength="500" />
        </div>

        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" @click="emit('update:open', false)">
            {{ warnings === null ? 'Cancel' : 'Close' }}
          </button>
          <button v-if="warnings === null" type="submit" class="btn btn-primary" :disabled="store.state.busy">Publish</button>
        </div>
      </form>
    </div>
  </div>
</template>
