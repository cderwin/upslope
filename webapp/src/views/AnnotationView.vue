<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import AnnotationDialog from '@/annotation/components/AnnotationDialog.vue'
import AnnotationTables from '@/annotation/components/AnnotationTables.vue'
import ExportDialog from '@/annotation/components/ExportDialog.vue'
import ImageStage from '@/annotation/components/ImageStage.vue'
import ImportDialog from '@/annotation/components/ImportDialog.vue'
import { imageUrl, listDomains, listImages } from '@/annotation/manifest'
import { useAnnotationStore } from '@/annotation/store'
import type { AnnotationDraft, AnnotationResult } from '@/annotation/types'

/** Route params, so a selected image is a shareable URL under /annotation. */
const props = defineProps<{ domain?: string; image?: string }>()

const router = useRouter()
const store = useAnnotationStore()

const domains = computed(() => listDomains(Object.keys(store.document.domains)))
const domain = computed(() => props.domain ?? domains.value[0] ?? '')

const images = computed(() => listImages(domain.value, Object.keys(store.document.domains[domain.value] ?? {})))
const image = computed(() => props.image ?? images.value[0] ?? '')

const zoomChoice = ref<'fit' | '1' | '2' | '4'>('1')
const zoom = computed(() => (zoomChoice.value === 'fit' ? 'fit' : Number(zoomChoice.value)))
const showLabels = ref(true)

const status = ref('')
const annotations = computed(() => store.annotationsFor(domain.value, image.value))
const pointRows = computed(() =>
  store.sortedPoints.value.map(([key, point]) => ({ key, point, usage: store.usageCount(key) })),
)

const dialogOpen = ref(false)
const draft = ref<AnnotationDraft | null>(null)
const draftPointKey = ref<string | null>(null)

const exportOpen = ref(false)
const exportYaml = ref('')
const importOpen = ref(false)
const importDialog = ref<InstanceType<typeof ImportDialog> | null>(null)

/** Keep the URL as the single source of truth for the selection. */
function select(nextDomain: string, nextImage: string | undefined) {
  const target = nextImage ?? listImages(nextDomain, Object.keys(store.document.domains[nextDomain] ?? {}))[0]
  void router.replace(
    target
      ? { name: 'annotation-image', params: { domain: nextDomain, image: target } }
      : { name: 'annotation' },
  )
}

watch([domain, image], () => (status.value = ''))

function openForPixel(x: number, y: number) {
  draft.value = { index: null, pixel_x: x, pixel_y: y }
  draftPointKey.value = null
  dialogOpen.value = true
}

function openForIndex(index: number) {
  const existing = annotations.value[index]
  if (!existing) return
  draft.value = { index, pixel_x: existing.pixel_x, pixel_y: existing.pixel_y }
  draftPointKey.value = existing.point
  dialogOpen.value = true
}

function onSave(result: AnnotationResult) {
  const key = result.existingKey ?? store.uniqueKey(result.annotation.point)
  store.savePoint(key, result.point)

  const annotation = { ...result.annotation, point: key }
  if (result.index == null) store.addAnnotation(domain.value, image.value, annotation)
  else store.updateAnnotation(domain.value, image.value, result.index, annotation)

  dialogOpen.value = false
}

function onDelete(index: number) {
  store.removeAnnotation(domain.value, image.value, index)
  dialogOpen.value = false
}

function onRemovePoint(key: string) {
  const point = store.points.value[key]
  if (!point) return
  const usage = store.usageCount(key)
  const message = usage
    ? `Delete "${point.name}" and the ${usage} pixel reference(s) to it?`
    : `Delete "${point.name}"?`
  if (window.confirm(message)) store.removePoint(key)
}

function openExport() {
  exportYaml.value = store.toYaml()
  exportOpen.value = true
}

function onImport(text: string) {
  try {
    store.importYaml(text)
    importOpen.value = false
  } catch (error) {
    importDialog.value?.fail(`Could not parse: ${(error as Error).message}`)
  }
}

function onReset() {
  if (window.confirm('Discard all georegistered points saved in this browser and reload the seed data?'))
    store.reset()
}

function onImageLoad(width: number, height: number) {
  status.value = `${image.value} — ${width} x ${height} px`
}

function onImageError() {
  status.value = `Could not load ${imageUrl(domain.value, image.value)}`
}
</script>

<template>
  <div class="container-fluid px-3 px-md-4 px-xl-5 py-4">
    <div class="d-flex justify-content-between align-items-center mb-3">
      <h1 class="h4 mb-0">Georegistration</h1>
      <div class="d-flex gap-2">
        <button type="button" class="btn btn-sm btn-outline-secondary" @click="importOpen = true">
          Import YAML
        </button>
        <button type="button" class="btn btn-sm btn-primary" @click="openExport">
          Export annotations.yaml
        </button>
      </div>
    </div>

    <div class="row g-3">
      <div class="col-lg-8">
        <div class="row g-2 align-items-end mb-2">
          <div class="col-sm-4">
            <label class="form-label mb-1" for="sel-domain">Domain</label>
            <select
              id="sel-domain"
              class="form-select form-select-sm"
              :value="domain"
              @change="select(($event.target as HTMLSelectElement).value, undefined)"
            >
              <option v-for="name in domains" :key="name" :value="name">{{ name }}</option>
            </select>
          </div>
          <div class="col-sm-4">
            <label class="form-label mb-1" for="sel-image">Image</label>
            <select
              id="sel-image"
              class="form-select form-select-sm"
              :value="image"
              @change="select(domain, ($event.target as HTMLSelectElement).value)"
            >
              <option v-for="name in images" :key="name" :value="name">{{ name }}</option>
            </select>
          </div>
          <div class="col-sm-2">
            <label class="form-label mb-1" for="sel-zoom">Zoom</label>
            <select id="sel-zoom" v-model="zoomChoice" class="form-select form-select-sm">
              <option value="fit">Fit width</option>
              <option value="1">100%</option>
              <option value="2">200%</option>
              <option value="4">400%</option>
            </select>
          </div>
          <div class="col-sm-2">
            <div class="form-check">
              <input id="chk-labels" v-model="showLabels" class="form-check-input" type="checkbox" />
              <label class="form-check-label" for="chk-labels">Labels</label>
            </div>
          </div>
        </div>

        <ImageStage
          v-if="domain && image"
          :src="imageUrl(domain, image)"
          :annotations="annotations"
          :points="store.points.value"
          :zoom="zoom"
          :show-labels="showLabels"
          @pick="openForPixel"
          @edit="openForIndex"
          @load="onImageLoad"
          @error="onImageError"
        />
        <div v-else class="alert alert-warning">
          No images found under <code>data/georegistration</code>.
        </div>

        <div class="d-flex justify-content-between mt-2 small text-body-secondary">
          <span class="font-monospace">{{ status }}</span>
          <span>Click the image to georegister a pixel. Click a marker to edit it.</span>
        </div>
      </div>

      <div class="col-lg-4">
        <AnnotationTables
          :annotations="annotations"
          :points="store.points.value"
          :point-rows="pointRows"
          @edit="openForIndex"
          @remove="(index) => store.removeAnnotation(domain, image, index)"
          @remove-point="onRemovePoint"
        />
        <div class="mt-2">
          <button type="button" class="btn btn-sm btn-outline-danger" @click="onReset">
            Reset all saved state
          </button>
        </div>
      </div>
    </div>

    <AnnotationDialog
      v-model:open="dialogOpen"
      :draft="draft"
      :points="store.sortedPoints.value"
      :initial-point-key="draftPointKey"
      @save="onSave"
      @delete="onDelete"
    />
    <ExportDialog v-model:open="exportOpen" :yaml="exportYaml" />
    <ImportDialog ref="importDialog" v-model:open="importOpen" @import="onImport" />
  </div>
</template>
