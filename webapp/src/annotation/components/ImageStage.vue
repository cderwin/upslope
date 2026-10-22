<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { GeographicPoint, ImageAnnotation } from '../types'

const props = defineProps<{
  src: string
  annotations: ImageAnnotation[]
  points: Record<string, GeographicPoint>
  /** `'fit'` scales to the container; a number is a multiple of natural size. */
  zoom: 'fit' | number
  showLabels: boolean
}>()

const emit = defineEmits<{
  pick: [x: number, y: number]
  edit: [point: string]
  load: [width: number, height: number]
  error: []
}>()

const wrap = ref<HTMLDivElement | null>(null)
const stage = ref<HTMLDivElement | null>(null)
const image = ref<HTMLImageElement | null>(null)

const natural = ref({ width: 0, height: 0 })
const scale = ref(1)
const cursor = ref<{ left: number; top: number; x: number; y: number } | null>(null)

const imageStyle = computed(() =>
  props.zoom === 'fit'
    ? { width: '100%', height: 'auto' }
    : {
        width: `${natural.value.width * props.zoom}px`,
        height: `${natural.value.height * props.zoom}px`,
      },
)

/** Recompute displayed pixels per natural image pixel. */
function measure(): number {
  const el = image.value
  if (!el?.naturalWidth) return scale.value
  scale.value = el.getBoundingClientRect().width / el.naturalWidth
  return scale.value
}

function onLoad() {
  const el = image.value
  if (!el) return
  natural.value = { width: el.naturalWidth, height: el.naturalHeight }
  void nextTick(measure)
  emit('load', el.naturalWidth, el.naturalHeight)
}

function onError() {
  natural.value = { width: 0, height: 0 }
  emit('error')
}

function pixelAt(event: MouseEvent): { x: number; y: number; inside: boolean } | null {
  const el = image.value
  if (!el?.naturalWidth) return null
  const rect = el.getBoundingClientRect()
  const factor = measure()
  return {
    x: Math.floor((event.clientX - rect.left) / factor),
    y: Math.floor((event.clientY - rect.top) / factor),
    inside:
      event.clientX >= rect.left &&
      event.clientX < rect.right &&
      event.clientY >= rect.top &&
      event.clientY < rect.bottom,
  }
}

/** Pointer travel (px) before a press becomes a pan instead of a click. */
const DRAG_THRESHOLD = 4

const panning = ref(false)
let drag: { x: number; y: number; left: number; top: number } | null = null
let suppressClick = false

function onPanStart(event: MouseEvent) {
  const host = wrap.value
  if (event.button !== 0 || !host) return
  // Stop native image dragging and text selection; click still fires.
  event.preventDefault()
  suppressClick = false
  drag = { x: event.clientX, y: event.clientY, left: host.scrollLeft, top: host.scrollTop }
  window.addEventListener('mousemove', onPanMove)
  window.addEventListener('mouseup', onPanEnd)
}

function onPanMove(event: MouseEvent) {
  const host = wrap.value
  if (!drag || !host) return
  const dx = event.clientX - drag.x
  const dy = event.clientY - drag.y
  if (!panning.value && Math.hypot(dx, dy) < DRAG_THRESHOLD) return
  panning.value = true
  cursor.value = null
  host.scrollLeft = drag.left - dx
  host.scrollTop = drag.top - dy
}

function onPanEnd() {
  window.removeEventListener('mousemove', onPanMove)
  window.removeEventListener('mouseup', onPanEnd)
  // A drag ends in a click on the stage or a marker; swallow it.
  suppressClick = panning.value
  panning.value = false
  drag = null
}

function onClickCapture(event: MouseEvent) {
  if (!suppressClick) return
  suppressClick = false
  event.stopPropagation()
}

function onMouseMove(event: MouseEvent) {
  if (panning.value) return
  const pixel = pixelAt(event)
  const host = stage.value
  if (!pixel?.inside || !host) {
    cursor.value = null
    return
  }
  const rect = host.getBoundingClientRect()
  cursor.value = {
    left: event.clientX - rect.left,
    top: event.clientY - rect.top,
    x: pixel.x,
    y: pixel.y,
  }
}

function onClick(event: MouseEvent) {
  const pixel = pixelAt(event)
  if (pixel?.inside) emit('pick', pixel.x, pixel.y)
}

function markerStyle(annotation: ImageAnnotation) {
  return {
    left: `${(annotation.pixel_x + 0.5) * scale.value}px`,
    top: `${(annotation.pixel_y + 0.5) * scale.value}px`,
  }
}

function describe(annotation: ImageAnnotation): string {
  const point = props.points[annotation.point]
  const label = point
    ? `${point.name} (${point.latitude}, ${point.longitude})`
    : `unknown point: ${annotation.point}`
  return `${label}\npixel ${annotation.pixel_x}, ${annotation.pixel_y}`
}

function labelFor(annotation: ImageAnnotation): string {
  return props.points[annotation.point]?.name ?? annotation.point
}

watch(() => props.zoom, () => void nextTick(measure))
watch(() => props.src, () => (cursor.value = null))

const onResize = () => measure()
onMounted(() => window.addEventListener('resize', onResize))
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  onPanEnd()
})
</script>

<template>
  <div
    ref="wrap"
    class="stage-wrap"
    :class="{ panning }"
    @mousedown="onPanStart"
    @click.capture="onClickCapture"
  >
    <div
      ref="stage"
      class="stage"
      @mousemove="onMouseMove"
      @mouseleave="cursor = null"
      @click="onClick"
    >
      <img ref="image" :src="src" draggable="false" :style="imageStyle" alt="" @load="onLoad" @error="onError" />

      <template v-if="cursor">
        <div class="crosshair-h" :style="{ top: `${cursor.top}px` }"></div>
        <div class="crosshair-v" :style="{ left: `${cursor.left}px` }"></div>
        <div class="cursor-tip" :style="{ left: `${cursor.left}px`, top: `${cursor.top}px` }">
          x: {{ cursor.x }}&nbsp;&nbsp;y: {{ cursor.y }}
        </div>
      </template>

      <button
        v-for="annotation in annotations"
        :key="annotation.point"
        type="button"
        class="marker"
        :style="markerStyle(annotation)"
        :title="describe(annotation)"
        @click.stop="emit('edit', annotation.point)"
      >
        <span v-if="showLabels" class="label">{{ labelFor(annotation) }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.stage-wrap {
  overflow: auto;
  max-height: 78vh;
  background: var(--bs-tertiary-bg);
  border: 1px solid var(--bs-border-color);
  border-radius: 0.375rem;
  padding: 0.25rem;
}

.stage {
  position: relative;
  display: inline-block;
  line-height: 0;
  cursor: crosshair;
}

.stage-wrap.panning,
.stage-wrap.panning .stage,
.stage-wrap.panning .marker {
  cursor: grabbing;
}

.stage img {
  display: block;
  image-rendering: pixelated;
}

.cursor-tip {
  position: absolute;
  z-index: 20;
  pointer-events: none;
  background: rgba(0, 0, 0, 0.8);
  color: #fff;
  font: 12px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace;
  padding: 2px 6px;
  border-radius: 3px;
  white-space: nowrap;
  transform: translate(10px, -130%);
}

.crosshair-h,
.crosshair-v {
  position: absolute;
  z-index: 15;
  pointer-events: none;
  background: rgba(255, 0, 0, 0.35);
}

.crosshair-h {
  left: 0;
  right: 0;
  height: 1px;
}

.crosshair-v {
  top: 0;
  bottom: 0;
  width: 1px;
}

.marker {
  position: absolute;
  z-index: 18;
  width: 0;
  height: 0;
  padding: 0;
  border: 0;
  background: none;
  cursor: pointer;
}

.marker::before,
.marker::after {
  content: '';
  position: absolute;
  background: #e23;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.9);
}

.marker::before {
  left: -7px;
  top: -1px;
  width: 14px;
  height: 2px;
}

.marker::after {
  top: -7px;
  left: -1px;
  height: 14px;
  width: 2px;
}

.marker .label {
  position: absolute;
  left: 9px;
  top: -9px;
  font: 11px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace;
  background: rgba(255, 255, 255, 0.88);
  color: #111;
  border: 1px solid #e23;
  border-radius: 3px;
  padding: 0 3px;
  white-space: nowrap;
}
</style>
