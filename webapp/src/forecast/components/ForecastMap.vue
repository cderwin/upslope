<script setup lang="ts">
import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ForecastLayer } from '@/forecast/types'

const props = defineProps<{
  token: string
  layer: ForecastLayer
  /** Current frame URL. */
  url: string
  /** 0–1 */
  opacity: number
  /** Drape everything over 3D terrain and tilt the camera. */
  terrain: boolean
}>()

const SOURCE_ID = 'forecast'
const LAYER_ID = 'forecast-raster'
// Satellite imagery with roads and place names; the same in light and dark mode.
const STYLE_URL = 'mapbox://styles/mapbox/satellite-streets-v12'
const DEM_SOURCE_ID = 'mapbox-dem'
const TERRAIN_EXAGGERATION = 1.5
const TERRAIN_PITCH = 60

const container = ref<HTMLDivElement | null>(null)
let map: mapboxgl.Map | undefined

function bounds(layer: ForecastLayer): mapboxgl.LngLatBounds {
  const result = new mapboxgl.LngLatBounds()
  for (const corner of layer.coordinates) result.extend(corner)
  return result
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
}

function source(): mapboxgl.ImageSource | undefined {
  return map?.getSource<mapboxgl.ImageSource>(SOURCE_ID)
}

onMounted(() => {
  mapboxgl.accessToken = props.token
  map = new mapboxgl.Map({
    container: container.value!,
    style: STYLE_URL,
    // The frames are registered in Web Mercator; the default globe view would bend them.
    projection: 'mercator',
    bounds: bounds(props.layer),
    fitBoundsOptions: { padding: 40 },
    pitch: props.terrain ? TERRAIN_PITCH : 0,
  })
  map.addControl(new mapboxgl.NavigationControl({ visualizePitch: true }), 'top-right')
  map.addControl(new mapboxgl.ScaleControl(), 'bottom-right')
  map.on('style.load', addForecastLayer)
  map.on('error', (event) => console.error('[ForecastMap]', event.error))
})

onBeforeUnmount(() => {
  map?.remove()
  map = undefined
})

watch(
  () => [props.layer, props.url] as const,
  ([layer, url], [previousLayer]) => {
    source()?.updateImage({ url, coordinates: layer.coordinates })
    if (layer !== previousLayer) map?.fitBounds(bounds(layer), { padding: 40 })
  },
)

watch(
  () => props.terrain,
  (enabled) => {
    if (!map?.getSource(SOURCE_ID)) return // style.load hasn't run yet; it applies terrain itself
    applyTerrain(enabled)
    map.easeTo({ pitch: enabled ? TERRAIN_PITCH : 0, duration: 1000 })
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
  <div ref="container" class="forecast-map"></div>
</template>

<style scoped>
.forecast-map {
  position: absolute;
  inset: 0;
}
</style>
