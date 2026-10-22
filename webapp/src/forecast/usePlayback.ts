import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'
import { frameUrl } from './api'
import type { ForecastLayer } from './types'

export const FRAME_INTERVAL_MS = 2000

/**
 * Frame selection, looping playback and preloading for one forecast layer.
 *
 * Frames are preloaded when the layer changes; playback skips frames that are
 * not decoded yet rather than stalling on them.
 */
export function usePlayback(layer: Ref<ForecastLayer | undefined>, intervalMs = FRAME_INTERVAL_MS) {
  const index = ref(0)
  const playing = ref(false)
  const loaded = ref(new Set<number>())
  const failed = ref(new Set<number>())

  const frames = computed(() => layer.value?.frames ?? [])
  const frame = computed(() => frames.value[index.value])
  const url = computed(() => (frame.value ? frameUrl(frame.value.path) : undefined))
  const loadProgress = computed(() =>
    frames.value.length ? (loaded.value.size + failed.value.size) / frames.value.length : 1,
  )

  let generation = 0
  watch(
    frames,
    (next) => {
      const current = ++generation
      loaded.value = new Set()
      failed.value = new Set()
      next.forEach((f, i) => {
        const image = new Image()
        image.src = frameUrl(f.path)
        image
          .decode()
          .then(() => current === generation && (loaded.value = new Set(loaded.value).add(i)))
          .catch(() => current === generation && (failed.value = new Set(failed.value).add(i)))
      })
    },
    { immediate: true },
  )

  /** Select the frame whose hour is closest to `hour`. */
  function seekHour(hour: number) {
    let best = 0
    frames.value.forEach((f, i) => {
      if (Math.abs(f.hour - hour) < Math.abs(frames.value[best].hour - hour)) best = i
    })
    index.value = best
  }

  function step(delta: number) {
    const count = frames.value.length
    if (count) index.value = (index.value + delta + count) % count
  }

  /** Advance to the next loaded frame, if any is loaded. */
  function advance() {
    const count = frames.value.length
    for (let offset = 1; offset <= count; offset++) {
      const next = (index.value + offset) % count
      if (loaded.value.has(next)) {
        index.value = next
        return
      }
    }
  }

  let timer: ReturnType<typeof setInterval> | undefined
  watch(playing, (isPlaying) => {
    clearInterval(timer)
    if (isPlaying) timer = setInterval(advance, intervalMs)
  })
  onScopeDispose(() => clearInterval(timer))

  return { index, frames, frame, url, playing, loadProgress, seekHour, step }
}
