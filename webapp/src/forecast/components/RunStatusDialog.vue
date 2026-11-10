<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, type RouteLocationRaw } from 'vue-router'
import { useBootstrapModal } from '@/annotation/useBootstrapModal'
import { IN_PROGRESS, ago, runLabel, runParam } from '@/forecast/api'
import { forecastStatus, refreshStatus } from '@/forecast/status'
import type { LayerState, LayerStatus, ModelStatus } from '@/forecast/types'

/** Each model's latest and in-progress runs, with every layer's progress. Opened from the navbar's status dot. */
const open = defineModel<boolean>('open', { required: true })

const element = ref<HTMLElement | null>(null)
useBootstrapModal(element, open, () => (open.value = false))

const route = useRoute()
const status = computed(() => forecastStatus.status)
const error = computed(() => forecastStatus.error)
const now = ref(Date.now())
let timer: number | undefined

/** Models whose section is open; every section starts collapsed each time the dialog opens. */
const expanded = ref(new Set<string>())

function toggle(model: string) {
  const next = new Set(expanded.value)
  if (!next.delete(model)) next.add(model)
  expanded.value = next
}

watch(
  open,
  (isOpen) => {
    window.clearInterval(timer)
    if (!isOpen) return
    expanded.value = new Set()
    now.value = Date.now()
    void refreshStatus()
    // the app refreshes the status every minute; this keeps the "… ago" times current
    timer = window.setInterval(() => (now.value = Date.now()), 30_000)
  },
  { immediate: true },
)
onBeforeUnmount(() => window.clearInterval(timer))

/** A link to the map showing `run` (`undefined` for the latest): this view of it on the map page, else the map. */
function runLink(run: string | undefined): RouteLocationRaw {
  const onMap = route.name === 'map' || route.name === 'map-layer'
  return onMap ? { query: { ...route.query, run } } : { name: 'map', query: { run } }
}

/** The badge in a collapsed section's header: what state the model's newest run is in. */
function summary(model: ModelStatus): { label: string; badge: string } {
  const run = model.in_progress
  const states = run?.layers.map((layer) => layer.state) ?? []
  if (states.includes('error')) return { label: 'errors', badge: 'text-bg-danger' }
  if (states.includes('stalled')) return { label: 'stalled', badge: 'text-bg-warning' }
  if (run && !run.complete) return { label: 'in progress', badge: 'text-bg-info' }
  if (model.latest) return { label: 'up to date', badge: 'text-bg-success' }
  return { label: 'no runs', badge: 'text-bg-secondary' }
}

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
        <div class="modal-body vstack gap-3">
          <div v-if="error" class="alert alert-danger py-2 mb-0 small">{{ error }}</div>
          <div v-if="!status" class="text-body-secondary">Loading…</div>

          <section v-for="model in status?.models ?? []" :key="model.id">
            <button
              type="button"
              class="model-toggle btn btn-link text-reset text-decoration-none p-0 w-100 d-flex align-items-center gap-2"
              :aria-expanded="expanded.has(model.id)"
              :aria-controls="`run-status-${model.id}`"
              @click="toggle(model.id)"
            >
              <span class="chevron" :class="{ open: expanded.has(model.id) }" aria-hidden="true">›</span>
              <span class="h6 mb-0">{{ model.name }}</span>
              <span class="small text-body-secondary">
                {{ model.latest ? `latest ${runLabel(model.latest.initialized)}` : '' }}
              </span>
              <span class="badge ms-auto" :class="summary(model).badge">{{ summary(model).label }}</span>
            </button>
            <div v-if="expanded.has(model.id)" :id="`run-status-${model.id}`" class="mt-2">
              <dl class="row small mb-2">
                <dt class="col-sm-3 fw-normal text-body-secondary">Latest complete run</dt>
                <dd class="col-sm-9 mb-1">
                  <template v-if="model.latest">
                    {{ runLabel(model.latest.initialized) }}
                    <RouterLink class="ms-2" :to="runLink(undefined)" @click="close">
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
                      :to="runLink(IN_PROGRESS)"
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
                    <RouterLink :to="runLink(runParam(run))" @click="close">
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
            </div>
          </section>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chevron {
  display: inline-block;
  width: 1ch;
  transition: transform 0.15s;
}

.chevron.open {
  transform: rotate(90deg);
}
</style>
