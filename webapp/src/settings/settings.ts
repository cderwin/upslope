import { reactive, readonly, watch } from 'vue'

/**
 * Viewer preferences, kept in localStorage. Losing them (private windows,
 * cleared site data) only resets them to their defaults.
 */

const STORAGE_KEY = 'upslope-settings'

export const FRAME_INTERVAL_DEFAULT_MS = 2000
export const FRAME_INTERVAL_MIN_MS = 250
export const FRAME_INTERVAL_MAX_MS = 4000

interface Settings {
  /** Playback delay between forecast frames. */
  frameIntervalMs: number
}

function clampInterval(ms: number): number {
  if (!Number.isFinite(ms)) return FRAME_INTERVAL_DEFAULT_MS
  return Math.min(Math.max(ms, FRAME_INTERVAL_MIN_MS), FRAME_INTERVAL_MAX_MS)
}

function readStored(): Settings {
  const settings: Settings = { frameIntervalMs: FRAME_INTERVAL_DEFAULT_MS }
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as Partial<Settings>
    if (typeof stored.frameIntervalMs === 'number') settings.frameIntervalMs = clampInterval(stored.frameIntervalMs)
  } catch {
    // Unreadable or blocked storage: use the defaults.
  }
  return settings
}

const state = reactive(readStored())

watch(state, (next) => {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
  } catch {
    // Not persisted; the setting still applies to this page.
  }
})

export const settings = readonly(state)

export function setFrameInterval(ms: number): void {
  state.frameIntervalMs = clampInterval(ms)
}
