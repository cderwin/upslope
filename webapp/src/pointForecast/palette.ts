import { onBeforeUnmount, onMounted, ref } from 'vue'

/**
 * Colors for the point forecast's charts: one per model, the same in every chart and control, and chart chrome
 * from Bootstrap's own colors so the charts match the rest of the page in light and dark mode.
 */

/**
 * A categorical palette in a fixed order, checked for color-blind separation between neighbours, and stepped for
 * each mode (the dark column is the same hues, re-stepped for Bootstrap's dark surface, #212529). In light mode
 * four of the hues are under 3:1 against white, so values are always readable in the hover tooltip.
 */
const LIGHT = ['#257de0', '#f47230', '#00b894', '#f2a600', '#ee7fac', '#008a17', '#6a4ce2', '#ec4647']
const DARK = ['#338ced', '#e76624', '#00a888', '#d28e05', '#e24d8b', '#0aac23', '#9887ef', '#eb6263']
const OTHER = { light: '#8a8a8a', dark: '#9c9c9c' }

/**
 * Each archived model's slot, so a model keeps its color whichever models are shown (never assigned by rank). The
 * order is `python/config/archive.yaml`'s; a model added there takes the next slot.
 */
const MODEL_SLOTS = ['hrrr_3km', 'rrfs_3km', 'gfs_0.25', 'aifs_0.25', 'hrdps_2.5km', 'ifs_0.25', 'nbm_2.5km']

export function modelColor(model: string, dark: boolean): string {
  const slot = MODEL_SLOTS.indexOf(model)
  const colors = dark ? DARK : LIGHT
  return slot >= 0 && slot < colors.length ? colors[slot] : dark ? OTHER.dark : OTHER.light
}

/** Models in their slot order (unknown ones last, by id), so lists and legends always read the same way. */
export function modelOrder(a: string, b: string): number {
  const rank = (id: string) => (MODEL_SLOTS.includes(id) ? MODEL_SLOTS.indexOf(id) : MODEL_SLOTS.length)
  return rank(a) - rank(b) || a.localeCompare(b)
}

/** The chart chrome: Bootstrap's colors for the current theme. */
export interface ChartTheme {
  dark: boolean
  text: string
  muted: string
  grid: string
  surface: string
  /** Night shading. */
  shade: string
  fontFamily: string
}

export function readTheme(): ChartTheme {
  const root = document.documentElement
  const style = getComputedStyle(root)
  const get = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback
  const dark = root.dataset.bsTheme === 'dark'
  return {
    dark,
    text: get('--bs-body-color', dark ? '#dee2e6' : '#212529'),
    muted: get('--bs-secondary-color', dark ? 'rgba(222,226,230,.75)' : 'rgba(33,37,41,.75)'),
    grid: get('--bs-border-color-translucent', dark ? 'rgba(255,255,255,.15)' : 'rgba(0,0,0,.175)'),
    surface: get('--bs-body-bg', dark ? '#212529' : '#fff'),
    shade: dark ? 'rgba(255,255,255,0.045)' : 'rgba(33,37,41,0.05)',
    fontFamily: get('--bs-body-font-family', 'system-ui, sans-serif'),
  }
}

/** The chart theme, following `data-bs-theme` (which App.vue keeps in step with the OS). */
export function useChartTheme() {
  const theme = ref<ChartTheme>(readTheme())
  let observer: MutationObserver | undefined
  onMounted(() => {
    observer = new MutationObserver(() => (theme.value = readTheme()))
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-bs-theme'] })
  })
  onBeforeUnmount(() => observer?.disconnect())
  return theme
}
