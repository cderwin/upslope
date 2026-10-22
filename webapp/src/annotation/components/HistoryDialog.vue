<script setup lang="ts">
import { ref, toRef, watch } from 'vue'
import { useBootstrapModal } from '../useBootstrapModal'
import * as api from '../api'
import { useAnnotationStore } from '../store'
import type { HistorySummary } from '../types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()

const element = ref<HTMLElement | null>(null)
useBootstrapModal(element, toRef(props, 'open'), () => emit('update:open', false))

const store = useAnnotationStore()
const entries = ref<HistorySummary[]>([])
const notes = ref<Record<string, string>>({})
const loading = ref(false)
const error = ref('')

watch(
  () => props.open,
  async (isOpen) => {
    if (!isOpen) return
    loading.value = true
    error.value = ''
    try {
      entries.value = await api.history()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  },
)

async function showNote(id: string) {
  try {
    notes.value[id] = (await api.historyEntry(id)).note ?? '(no note)'
  } catch (e) {
    notes.value[id] = e instanceof Error ? e.message : String(e)
  }
}

async function restore(entry: HistorySummary) {
  const message = `Replace the draft with published r${entry.revision}? Unpublished changes are lost; publish afterwards to make it live.`
  if (window.confirm(message) && (await store.restore(entry.id))) emit('update:open', false)
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString()
}
</script>

<template>
  <div ref="element" class="modal fade" tabindex="-1">
    <div class="modal-dialog modal-lg modal-dialog-scrollable">
      <div class="modal-content">
        <div class="modal-header">
          <h5 class="modal-title">Published versions</h5>
          <button type="button" class="btn-close" @click="emit('update:open', false)"></button>
        </div>
        <div class="modal-body">
          <div v-if="error" class="alert alert-danger py-2">{{ error }}</div>
          <div v-else-if="loading" class="text-body-secondary small">Loading…</div>
          <table v-else class="table table-sm align-middle mb-0">
            <thead>
              <tr><th>Revision</th><th>Published</th><th>Note</th><th></th></tr>
            </thead>
            <tbody>
              <tr v-if="!entries.length">
                <td colspan="4" class="text-body-secondary small">Nothing has been published yet.</td>
              </tr>
              <tr v-for="entry in entries" :key="entry.id">
                <td>
                  r{{ entry.revision }}
                  <span v-if="entry.revision === store.state.publishedRevision" class="badge text-bg-primary ms-1">live</span>
                </td>
                <td class="small">{{ formatTime(entry.published_at) }}</td>
                <td class="small">
                  <template v-if="notes[entry.id] !== undefined">{{ notes[entry.id] }}</template>
                  <button v-else type="button" class="btn btn-link btn-sm p-0" @click="showNote(entry.id)">show</button>
                </td>
                <td class="text-end">
                  <button type="button" class="btn btn-sm btn-outline-secondary" :disabled="store.state.busy" @click="restore(entry)">
                    Restore to draft
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>
