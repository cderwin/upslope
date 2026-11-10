import { computed, reactive } from 'vue'
import { fetchStatus } from './api'
import type { ForecastStatus } from './types'

/**
 * The forecast status shared by the navbar's status dot, the Model runs dialog and the map: whoever fetches it
 * last (the app's refresh every minute, the dialog when it opens, the map when it loads) updates it for all.
 */

const state = reactive({
  status: null as ForecastStatus | null,
  error: '',
  /** Whether the Model runs dialog is open. */
  dialogOpen: false,
})

export const forecastStatus = state

export function setStatus(status: ForecastStatus): void {
  state.status = status
  state.error = ''
}

let abort: AbortController | undefined

/** Fetch the status now; a failure keeps the last one and records the error. */
export async function refreshStatus(): Promise<void> {
  abort?.abort()
  abort = new AbortController()
  const { signal } = abort
  try {
    setStatus(await fetchStatus(signal))
  } catch (e) {
    if (!signal.aborted) state.error = e instanceof Error ? e.message : String(e)
  }
}

export function openRunStatus(): void {
  state.dialogOpen = true
}

export type StatusTone = 'success' | 'info' | 'warning' | 'danger' | 'secondary'

/** The status dot's color: blue while a run is coming in, amber if a layer is stuck, red on errors, grey if unknown. */
export const statusTone = computed<StatusTone>(() => {
  if (!state.status) return state.error ? 'danger' : 'secondary'
  const runs = state.status.models.map((model) => model.in_progress).filter((run) => run !== null)
  const states = runs.flatMap((run) => run.layers.map((layer) => layer.state))
  if (states.includes('error')) return 'danger'
  if (states.includes('stalled')) return 'warning'
  if (runs.some((run) => !run.complete)) return 'info'
  return 'success'
})

const TONE_LABELS: Record<StatusTone, string> = {
  success: 'every model is up to date',
  info: 'a model run is coming in',
  warning: 'a layer has stalled',
  danger: 'a layer has errors',
  secondary: 'status not loaded yet',
}

export const statusLabel = computed(() =>
  state.error && !state.status ? `Model runs: ${state.error}` : `Model runs: ${TONE_LABELS[statusTone.value]}`,
)
