<script setup lang="ts">
import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { displayPosition, formatValue, loadLookup, loadValues, valueAt } from '@/forecast/frameValues'
import { preloadGeotiff } from '@/forecast/valueDecoder'
import type { ForecastLayer, LngLat } from '@/forecast/types'

const props = defineProps<{
  token: string
  layer: ForecastLayer
  /** Current frame URL. */
  url: string
  /** Current frame's value file, for the value under the cursor; null for layers without numbers. */
  valuesUrl: string | null
  /** 0–1 */
  opacity: number
  /** Drape everything over 3D terrain; the viewer tilts the camera themselves (ctrl-drag). */
  terrain: boolean
  /** A point to mark (the open point forecast's), besides the clicked point while its menu is open. */
  pin?: LngLat | null
}>()

defineSlots<{
  /** The menu for a clicked point; `close` closes it. */
  'point-menu'(props: { lngLat: LngLat; value: string | null; close: () => void }): unknown
}>()

const SOURCE_ID = 'forecast'
const LAYER_ID = 'forecast-raster'
const EXTENT_SOURCE_ID = 'forecast-extent'
const EXTENT_LAYER_ID = 'forecast-extent-line'
// Burnt orange: stands out on the satellite basemap and on the forecast's own colors without matching either.
const EXTENT_COLOR = '#cc5500'
// Satellite imagery with roads and place names; the same in light and dark mode.
const STYLE_URL = 'mapbox://styles/mapbox/satellite-streets-v12'
const DEM_SOURCE_ID = 'mapbox-dem'
const TERRAIN_EXAGGERATION = 1.5

const container = ref<HTMLDivElement | null>(null)
let map: mapboxgl.Map | undefined

function bounds(layer: ForecastLayer): mapboxgl.LngLatBounds {
  const result = new mapboxgl.LngLatBounds()
  for (const corner of layer.coordinates) result.extend(corner)
  return result
}

/** The outline of the layer's data, closed: its source grid's edges, or (in older manifests) the image's corners. */
function extent(layer: ForecastLayer) {
  const points = layer.extent?.length ? layer.extent : layer.coordinates
  const ring = [...points, points[0]]
  return { type: 'Feature' as const, properties: {}, geometry: { type: 'LineString' as const, coordinates: ring } }
}

/** Turn terrain on or off; the forecast and basemap raster layers drape over it. */
function applyTerrain(enabled: boolean) {
  if (!map) return
  if (enabled && !map.getSource(DEM_SOURCE_ID)) {
    map.addSource(DEM_SOURCE_ID, {
      type: 'raster-dem',
      url: 'mapbox://mapbox.mapbox-terrain-dem-v1',
      tileSize: 512,
      maxzoom: 14,
    })
  }
  map.setTerrain(enabled ? { source: DEM_SOURCE_ID, exaggeration: TERRAIN_EXAGGERATION } : null)
  // Atmosphere and horizon haze, which only matter once the camera is tilted.
  map.setFog(enabled ? {} : null)
}

/** (Re)add the overlay; a style change drops every source and layer. */
function addForecastLayer() {
  if (!map || map.getSource(SOURCE_ID)) return
  applyTerrain(props.terrain)
  map.addSource(SOURCE_ID, { type: 'image', url: props.url, coordinates: props.layer.coordinates })
  // Draw beneath the first label layer so place names stay readable over the forecast.
  const firstSymbol = map.getStyle()?.layers?.find((layer) => layer.type === 'symbol')?.id
  map.addLayer(
    {
      id: LAYER_ID,
      type: 'raster',
      source: SOURCE_ID,
      paint: {
        'raster-opacity': props.opacity,
        'raster-fade-duration': 0,
        'raster-resampling': 'nearest',
      },
    },
    firstSymbol,
  )
  map.addSource(EXTENT_SOURCE_ID, { type: 'geojson', data: extent(props.layer) })
  map.addLayer(
    {
      id: EXTENT_LAYER_ID,
      type: 'line',
      source: EXTENT_SOURCE_ID,
      paint: { 'line-color': EXTENT_COLOR, 'line-width': 2, 'line-opacity': 0.9 },
    },
    firstSymbol,
  )
}

function source(): mapboxgl.ImageSource | undefined {
  return map?.getSource<mapboxgl.ImageSource>(SOURCE_ID)
}

// --- the value under the cursor (layers with value files only)

/** Where the cursor is, in map and screen coordinates. */
let pointer: { lngLat: LngLat; x: number; y: number } | null = null
const hover = ref<{ x: number; y: number; text: string } | null>(null)
let hoverRequest = 0

/** The current frame's value at a point, e.g. `Temperature: 28 °F`; null for layers without values or outside the
 * data. A frame's value file loads on first use. */
async function valueText(lngLat: LngLat): Promise<string | null> {
  const { layer, valuesUrl } = props
  const position = valuesUrl && layer.values && layer.lookupUrl ? displayPosition(layer, lngLat) : null
  if (!valuesUrl || !layer.values || !layer.lookupUrl || !position) return null
  const [lookup, values] = await Promise.all([loadLookup(layer.lookupUrl), loadValues(valuesUrl)])
  const value = valueAt(lookup, values, layer.values, position)
  return value === null ? null : `${layer.name}: ${formatValue(value, layer.legend)}`
}

/** Show the current frame's value under the pointer (not while a point's menu is open). */
async function updateHover() {
  const request = ++hoverRequest
  const at = pointer
  if (!at || menu.value) {
    hover.value = null
    return
  }
  try {
    const text = await valueText(at.lngLat)
    if (request !== hoverRequest) return // the pointer, frame or layer moved on meanwhile
    hover.value = text === null ? null : { x: at.x, y: at.y, text }
  } catch (e) {
    if (request === hoverRequest) hover.value = null
    console.error('[ForecastMap] value file', e)
  }
}

/** Fetch a layer's lookup as it's shown, so the first hover only waits for the frame's values. */
function preloadLookup(layer: ForecastLayer) {
  if (layer.lookupUrl) loadLookup(layer.lookupUrl).catch((e) => console.error('[ForecastMap] lookup file', e))
}

function onPointer(event: mapboxgl.MapMouseEvent) {
  pointer = { lngLat: [event.lngLat.lng, event.lngLat.lat], x: event.point.x, y: event.point.y }
  void updateHover()
}

// --- a clicked point's menu (the value there, and what to do with the point)

const menu = ref<{ lngLat: LngLat; x: number; y: number; value: string | null } | null>(null)
const menuElement = ref<HTMLElement | null>(null)
let menuRequest = 0

/** A click opens the point's menu, or closes an open one (a click elsewhere). On touch screens, which have no hover,
 * the menu also shows the value a tap used to. */
function onClick(event: mapboxgl.MapMouseEvent) {
  if (menu.value) {
    closeMenu()
    return
  }
  const lngLat: LngLat = [event.lngLat.lng, event.lngLat.lat]
  menu.value = { lngLat, x: event.point.x, y: event.point.y, value: null }
  hoverRequest++
  hover.value = null
  const request = ++menuRequest
  valueText(lngLat)
    .then((text) => {
      if (request === menuRequest && menu.value) menu.value.value = text
    })
    .catch((e) => console.error('[ForecastMap] value file', e))
  // Keyboard users land on the menu's first item.
  void nextTick(() => menuElement.value?.querySelector<HTMLElement>('button, a')?.focus({ preventScroll: true }))
}

function closeMenu() {
  menuRequest++
  menu.value = null
}

/** Keep the menu inside the map: it opens below and right of the point, flipping where it would overflow. */
const menuStyle = computed(() => {
  const at = menu.value
  const canvas = container.value
  if (!at || !canvas) return {}
  const flipX = at.x > canvas.clientWidth - 240
  const flipY = at.y > canvas.clientHeight - 140
  return {
    left: `${at.x}px`,
    top: `${at.y}px`,
    transform: `translate(${flipX ? 'calc(-100% - 10px)' : '10px'}, ${flipY ? 'calc(-100% - 10px)' : '10px'})`,
  }
})

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && menu.value) {
    closeMenu()
    event.stopPropagation()
  }
}

/** A press outside the map and the menu (the controls, say) closes it; presses on the map are clicks (above). */
function onDocumentPointerDown(event: PointerEvent) {
  const target = event.target as Node | null
  if (!menu.value || !target) return
  if (menuElement.value?.contains(target) || container.value?.contains(target)) return
  closeMenu()
}

/** Moving the map (by the viewer, not a refit) leaves the menu pointing nowhere: close it. */
function onMoveStart(event: { originalEvent?: unknown }) {
  if (event.originalEvent && menu.value) closeMenu()
}

// The marker on the clicked point (menu open), else on `pin` (the open point forecast's point).
const PIN_COLOR = EXTENT_COLOR
let marker: mapboxgl.Marker | undefined
const pinAt = computed<LngLat | null>(() => menu.value?.lngLat ?? props.pin ?? null)

function updateMarker(at: LngLat | null) {
  if (!map) return
  if (!at) {
    marker?.remove()
    marker = undefined
    return
  }
  if (!marker) {
    const element = document.createElement('div')
    element.className = 'point-pin'
    element.style.setProperty('--pin-color', PIN_COLOR)
    marker = new mapboxgl.Marker({ element, anchor: 'center' })
  }
  marker.setLngLat(at).addTo(map)
}

watch(pinAt, (at) => updateMarker(at))

onMounted(() => {
  preloadGeotiff()
  preloadLookup(props.layer)
  mapboxgl.accessToken = props.token
  map = new mapboxgl.Map({
    container: container.value!,
    style: STYLE_URL,
    // The frames are registered in Web Mercator; the default globe view would bend them.
    projection: 'mercator',
    bounds: bounds(props.layer),
    fitBoundsOptions: { padding: 40 },
  })
  map.addControl(new mapboxgl.NavigationControl({ visualizePitch: true }), 'top-right')
  map.addControl(new mapboxgl.ScaleControl(), 'bottom-right')
  map.on('style.load', addForecastLayer)
  map.on('error', (event) => console.error('[ForecastMap]', event.error))
  map.on('mousemove', onPointer)
  map.on('click', onClick)
  map.on('movestart', onMoveStart)
  map.on('mouseout', () => {
    pointer = null
    hoverRequest++
    hover.value = null
  })
  updateMarker(pinAt.value)
  window.addEventListener('keydown', onKeydown, true)
  document.addEventListener('pointerdown', onDocumentPointerDown, true)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown, true)
  document.removeEventListener('pointerdown', onDocumentPointerDown, true)
  marker?.remove()
  map?.remove()
  map = undefined
})

watch(
  () => [props.layer, props.url] as const,
  ([layer, url], [previousLayer]) => {
    source()?.updateImage({ url, coordinates: layer.coordinates })
    if (layer !== previousLayer) {
      preloadLookup(layer)
      map?.getSource<mapboxgl.GeoJSONSource>(EXTENT_SOURCE_ID)?.setData(extent(layer))
      // Keep the view when flipping between models and variables over the same spot; refit only when the
      // new layer doesn't cover where the viewer is looking (a smaller domain, say).
      const next = bounds(layer)
      if (map && !next.contains(map.getCenter())) map.fitBounds(next, { padding: 40 })
    }
  },
)

// playback and layer changes update the value under a still cursor, and in an open menu
watch(
  () => [props.valuesUrl, props.layer] as const,
  () => {
    void updateHover()
    const at = menu.value
    if (!at) return
    const request = ++menuRequest
    valueText(at.lngLat)
      .then((text) => {
        if (request === menuRequest && menu.value) menu.value.value = text
      })
      .catch((e) => console.error('[ForecastMap] value file', e))
  },
)

watch(
  () => props.terrain,
  (enabled) => {
    if (!map?.getSource(SOURCE_ID)) return // style.load hasn't run yet; it applies terrain itself
    // The camera stays where the viewer put it: only they tilt it, with ctrl-drag.
    applyTerrain(enabled)
  },
)

watch(
  () => props.opacity,
  (opacity) => {
    if (map?.getLayer(LAYER_ID)) map.setPaintProperty(LAYER_ID, 'raster-opacity', opacity)
  },
)
</script>

<template>
  <div class="forecast-map">
    <div ref="container" class="forecast-map-canvas"></div>
    <div
      v-if="hover"
      class="value-tooltip badge text-bg-dark shadow-sm"
      role="status"
      :style="{ left: `${hover.x}px`, top: `${hover.y}px` }"
    >
      {{ hover.text }}
    </div>
    <div v-if="menu" ref="menuElement" class="point-menu-popup dropdown-menu show shadow p-0" :style="menuStyle">
      <slot name="point-menu" :lng-lat="menu.lngLat" :value="menu.value" :close="closeMenu"></slot>
    </div>
  </div>
</template>

<style scoped>
.forecast-map,
.forecast-map-canvas {
  position: absolute;
  inset: 0;
}

.value-tooltip {
  position: absolute;
  z-index: 1;
  /* above and to the right of the cursor, out of its way */
  transform: translate(12px, calc(-100% - 8px));
  pointer-events: none;
  font-weight: 500;
  white-space: nowrap;
}

/* Bootstrap's .dropdown-menu, placed at the clicked point rather than under a toggle. */
.point-menu-popup {
  position: absolute;
  z-index: 3;
  margin: 0;
}

/* The marker is created outside Vue (mapboxgl.Marker), so its style can't be scoped. */
:global(.point-pin) {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--pin-color);
  border: 2px solid #fff;
  box-shadow: 0 0 0 1px rgb(0 0 0 / 0.35), 0 1px 4px rgb(0 0 0 / 0.4);
  pointer-events: none;
}
</style>
