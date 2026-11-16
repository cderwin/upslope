/**
 * A typed wrapper over skewt-plus, which draws the skew-T diagram. It's a plain script with no types or
 * cleanup of its own, so how it loads, its options and its teardown all stay in this file.
 */
import 'skewt-plus/dist/skewt.css'
import type { SoundingLevel } from './api'

export interface SkewTChart {
  plot(levels: SoundingLevel[]): void
  destroy(): void
}

/** What the script puts on `window`. */
interface SkewTPlus {
  plot(levels: SoundingLevel[], options?: { add?: boolean; select?: boolean }): void
  clear(): void
}

type SkewTPlusConstructor = new (
  el: HTMLElement,
  options?: { isTouchDevice?: boolean; gradient?: number; topp?: number; parctempShift?: number },
) => SkewTPlus

/** d3's record of the listeners it added to an element. */
interface D3Listeners {
  __on?: { type: string; listener: EventListener; capture?: boolean }[]
}

export async function createChart(el: HTMLElement): Promise<SkewTChart> {
  // Not a module: the script bundles its own d3 and sets `window.SkewT`.
  await import('skewt-plus/dist/bundle.js')
  const SkewT = (window as unknown as { SkewT: SkewTPlusConstructor }).SkewT
  el.classList.add('skew-t')
  // Without it, the library probes for a Leaflet global (`L`) that isn't there.
  const chart = new SkewT(el, { isTouchDevice: matchMedia('(pointer: coarse)').matches })
  // Heights default to meters; the library only switches through its own feet/meter buttons, whose handler
  // needs a plotted sounding.
  let inFeet = false
  return {
    plot(levels) {
      chart.plot(levels)
      if (inFeet || !levels.length) return
      inFeet = true
      const buttons = [...el.querySelectorAll<HTMLElement>('.buttons.units')]
      buttons.find((button) => button.textContent === 'feet')?.click()
    },
    destroy() {
      chart.clear()
      // It has no destroy: drop the window resize handler it adds through d3, then its DOM.
      const win = window as unknown as D3Listeners
      for (const on of win.__on?.filter((on) => on.type === 'resize') ?? []) {
        window.removeEventListener(on.type, on.listener, on.capture)
      }
      if (win.__on) win.__on = win.__on.filter((on) => on.type !== 'resize')
      el.replaceChildren()
      el.classList.remove('skew-t')
    },
  }
}
