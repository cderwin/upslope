<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useBootstrapModal } from '@/annotation/useBootstrapModal'
import { IN_PROGRESS, ago, fetchStatus, runLabel, runParam } from '@/forecast/api'
import type { ForecastStatus, LayerState, LayerStatus } from '@/forecast/types'

/** Each model's latest and in-progress runs, with every layer's progress. Refreshes while open. */
const open = defineModel<boolean>('open', { required: true })
/** The status the map loaded with, shown until the dialog's own fetch returns. */
const props = defineProps<{ initial: ForecastStatus | null }>()
const emit = defineEmits<{ loaded: [status: ForecastStatus] }>()

const REFRESH_MS = 60_000

const element = ref<HTMLElement | null>(null)
useBootstrapModal(element, open, () => (open.value = false))

const route = useRoute()
const status = ref<ForecastStatus | null>(props.initial)
const error = ref('')
const now = ref(Date.now())
let timer: number | undefined
let abort: AbortController | undefined

async function refresh() {
  abort?.abort()
  abort = new AbortController()
  try {
    status.value = await fetchStatus(abort.signal)
    emit('loaded', status.value)
    error.value = ''
  } catch (e) {
    if (!abort.signal.aborted) error.value = e instanceof Error ? e.message : String(e)
  }
  now.value = Date.now()
}

watch(
  open,
  (isOpen) => {
    window.clearInterval(timer)
    if (!isOpen) return abort?.abort()
    if (!status.value) status.value = props.initial
    void refresh()
    timer = window.setInterval(() => void refresh(), REFRESH_MS)
  },
  { immediate: true },
)
watch(
  () => props.initial,
  (next) => {
    if (next && !open.value) status.value = next
  },
)
onBeforeUnmount(() => {
  window.clearInterval(timer)
  abort?.abort()
})

const STATE_BADGES: Record<LayerState, string> = {
  complete: 'text-bg-success',
  waiting: 'text-bg-secondary',
  stalled: 'text-bg-warning',
  error: 'text-bg-danger',
}

function progress(layer: LayerStatus): number {
  return layer.available_hour === null ? 0 : Math.min(1, (layer.available_hour + 1) / (layer.last_hour + 1))
}

function hourLabel(layer: LayerStatus): string {
  return layer.available_hour === null ? `— / ${layer.last_hour}h` : `${layer.available_hour} / ${layer.last_hour}h`
}

function close() {
  open.value = false
}
</script>

<template>
  <div ref="element" class="modal fade" tabindex="-1" aria-labelledby="run-status-title">
    <div class="modal-dialog modal-lg modal-dialog-scrollable">
      <div class="modal-content">
        <div class="modal-header">
          <h5 id="run-status-title" class="modal-title">Model runs</h5>
          <button type="button" class="btn-close" aria-label="Close" @click="close"></button>
        </div>
        <div class="modal-body vstack gap-4">
          <div v-if="error" class="alert alert-danger py-2 mb-0 small">{{ error }}</div>
          <div v-if="!status" class="text-body-secondary">Loading…</div>

          <section v-for="model in status?.models ?? []" :key="model.id">
            <h6 class="mb-2">{{ model.name }}</h6>
            <dl class="row small mb-2">
              <dt class="col-sm-3 fw-normal text-body-secondary">Latest complete run</dt>
              <dd class="col-sm-9 mb-1">
                <template v-if="model.latest">
                  {{ runLabel(model.latest.initialized) }}
                  <RouterLink class="ms-2" :to="{ query: { ...route.query, run: undefined } }" @click="close">
                    Show
                  </RouterLink>
                </template>
                <span v-else class="text-body-secondary">none yet</span>
              </dd>
              <template v-if="model.in_progress">
                <dt class="col-sm-3 fw-normal text-body-secondary">Newest run</dt>
                <dd class="col-sm-9 mb-1">
                  {{ runLabel(model.in_progress.initialized) }}
                  <span v-if="model.in_progress.complete" class="badge text-bg-success ms-1">complete</span>
                  <span v-else class="badge text-bg-info ms-1">in progress</span>
                  <RouterLink
                    v-if="!model.in_progress.complete"
                    class="ms-2"
                    :to="{ query: { ...route.query, run: IN_PROGRESS } }"
                    @click="close"
                  >
                    Preview
                  </RouterLink>
                  <div class="text-body-secondary">Last checked {{ ago(model.in_progress.checked_at, now) }}</div>
                </dd>
              </template>
              <dt class="col-sm-3 fw-normal text-body-secondary">Kept runs</dt>
              <dd class="col-sm-9 mb-1">
                <template v-for="(run, i) in model.runs" :key="run.path">
                  <span v-if="i">, </span>
                  <RouterLink :to="{ query: { ...route.query, run: runParam(run) } }" @click="close">
                    {{ runLabel(run.initialized) }}
                  </RouterLink>
                </template>
                <span v-if="!model.runs.length" class="text-body-secondary">none</span>
              </dd>
            </dl>

            <table v-if="model.in_progress && !model.in_progress.complete" class="table table-sm small align-middle mb-0">
              <thead>
                <tr>
                  <th scope="col">Layer</th>
                  <th scope="col" class="w-50">Hours exported</th>
                  <th scope="col">State</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="layer in model.in_progress.layers" :key="layer.id">
                  <td>{{ layer.name }}</td>
                  <td>
                    <div class="d-flex align-items-center gap-2">
                      <div
                        class="progress flex-grow-1"
                        role="progressbar"
                        :aria-label="`${layer.name} hours exported`"
                        :aria-valuenow="Math.round(progress(layer) * 100)"
                        aria-valuemin="0"
                        aria-valuemax="100"
                        style="height: 6px"
                      >
                        <div class="progress-bar" :style="{ width: `${progress(layer) * 100}%` }"></div>
                      </div>
                      <span class="font-monospace text-nowrap">{{ hourLabel(layer) }}</span>
                    </div>
                  </td>
                  <td>
                    <span class="badge" :class="STATE_BADGES[layer.state]" :title="layer.error ?? undefined">
                      {{ layer.state }}
                    </span>
                    <div v-if="layer.error" class="text-danger">{{ layer.error }}</div>
                    <div v-else-if="layer.state !== 'complete' && layer.progressed_at" class="text-body-secondary">
                      last frame {{ ago(layer.progressed_at, now) }}
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>
        </div>
      </div>
    </div>
  </div>
</template>
