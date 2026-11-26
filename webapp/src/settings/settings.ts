import { reactive, readonly, watch } from 'vue'

/**
 * Viewer preferences, kept in localStorage. Losing them (private windows,
 * cleared site data) only resets them to their defaults.
 */

const STORAGE_KEY = 'upslope-settings'

export const FRAME_INTERVAL_DEFAULT_MS = 2000
export const FRAME_INTERVAL_MIN_MS = 250
export const FRAME_INTERVAL_MAX_MS = 4000

/** Imperial: °F, ft, in, mph. Metric: °C, m, mm, m/s. */
export type Units = 'imperial' | 'metric'

interface Settings {
  /** Playback delay between forecast frames. */
  frameIntervalMs: number
  /** Model the map opens on, and switches to when the current one lacks a variable; null for the server's default. */
  defaultModel: string | null
  /** Drape the map over 3D terrain (the camera only tilts when the viewer tilts it). */
  terrain: boolean
  /** The map's control panel is collapsed to the valid time and playback buttons. */
  controlsCollapsed: boolean
  /** Units for point forecasts (the server sends metric). */
  units: Units
}

function clampInterval(ms: number): number {
  if (!Number.isFinite(ms)) return FRAME_INTERVAL_DEFAULT_MS
  return Math.min(Math.max(ms, FRAME_INTERVAL_MIN_MS), FRAME_INTERVAL_MAX_MS)
}

function readStored(): Settings {
  const settings: Settings = {
    frameIntervalMs: FRAME_INTERVAL_DEFAULT_MS,
    defaultModel: null,
    terrain: false,
    controlsCollapsed: false,
    units: 'imperial',
  }
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}') as Partial<Settings>
    if (typeof stored.frameIntervalMs === 'number') settings.frameIntervalMs = clampInterval(stored.frameIntervalMs)
    if (typeof stored.defaultModel === 'string') settings.defaultModel = stored.defaultModel
    if (typeof stored.terrain === 'boolean') settings.terrain = stored.terrain
    if (typeof stored.controlsCollapsed === 'boolean') settings.controlsCollapsed = stored.controlsCollapsed
    if (stored.units === 'imperial' || stored.units === 'metric') settings.units = stored.units
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

/** Null uses the server's default model. */
export function setDefaultModel(model: string | null): void {
  state.defaultModel = model || null
}

export function setTerrain(enabled: boolean): void {
  state.terrain = enabled
}

export function setControlsCollapsed(collapsed: boolean): void {
  state.controlsCollapsed = collapsed
}

export function setUnits(units: Units): void {
  state.units = units
}

/** Whether the Settings dialog is open: the navbar opens it, and on phones (no navbar) the map's ⋮ menu. */
export const settingsDialog = reactive({ open: false })

export function openSettings(): void {
  settingsDialog.open = true
}
