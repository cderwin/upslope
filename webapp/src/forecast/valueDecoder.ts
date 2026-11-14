/**
 * geotiff.js, which decodes value files, loaded in the background: after the page has loaded, once the browser
 * is idle, so it never slows the page down but is usually ready before the first hover. A hover that comes
 * sooner waits for the same download rather than starting another.
 */

type GeoTiffModule = typeof import('geotiff')

let loading: Promise<GeoTiffModule> | null = null

/** geotiff.js, loading it now if the background load hasn't started (or failed). */
export function geotiff(): Promise<GeoTiffModule> {
  if (!loading) {
    loading = import('geotiff')
    loading.catch(() => (loading = null)) // try again next time
  }
  return loading
}

/** Start loading geotiff.js once the page has loaded and the browser is idle. */
export function preloadGeotiff(): void {
  const start = () => void geotiff().catch((e) => console.error('[valueDecoder] could not load geotiff.js', e))
  // Safari has no requestIdleCallback
  const whenIdle = () => {
    if (typeof window.requestIdleCallback === 'function') window.requestIdleCallback(start, { timeout: 5000 })
    else setTimeout(start, 1000)
  }
  if (document.readyState === 'complete') whenIdle()
  else window.addEventListener('load', whenIdle, { once: true })
}
