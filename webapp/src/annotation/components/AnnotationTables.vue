<script setup lang="ts">
import type { GeographicPoint, PixelAnnotation } from '../types'

defineProps<{
  annotations: PixelAnnotation[]
  points: Record<string, GeographicPoint>
  pointRows: { key: string; point: GeographicPoint; usage: number }[]
}>()

const emit = defineEmits<{
  edit: [index: number]
  remove: [index: number]
  removePoint: [key: string]
}>()
</script>

<template>
  <ul class="nav nav-tabs" role="tablist">
    <li class="nav-item">
      <button class="nav-link active" data-bs-toggle="tab" data-bs-target="#pane-pixels" type="button">
        Points on image
      </button>
    </li>
    <li class="nav-item">
      <button class="nav-link" data-bs-toggle="tab" data-bs-target="#pane-points" type="button">
        Geographic points
      </button>
    </li>
  </ul>

  <div class="tab-content border border-top-0 rounded-bottom p-2">
    <div id="pane-pixels" class="tab-pane fade show active">
      <div class="table-responsive">
        <table class="table table-sm table-hover align-middle mb-0">
          <thead>
            <tr><th>Pixel</th><th>Point</th><th>Lat, Lon</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-if="!annotations.length">
              <td colspan="4" class="text-body-secondary small">
                No georegistered points on this image yet. Click the image to add one.
              </td>
            </tr>
            <tr v-for="(annotation, index) in annotations" :key="index">
              <td class="font-monospace">{{ annotation.pixel_x }}, {{ annotation.pixel_y }}</td>
              <td>{{ points[annotation.point]?.name ?? annotation.point }}</td>
              <td class="font-monospace">
                <template v-if="points[annotation.point]">
                  {{ points[annotation.point]!.latitude }}, {{ points[annotation.point]!.longitude }}
                </template>
                <template v-else>?</template>
              </td>
              <td class="text-end text-nowrap">
                <button type="button" class="btn btn-sm btn-outline-secondary" @click="emit('edit', index)">
                  Edit
                </button>
                <button type="button" class="btn btn-sm btn-outline-danger ms-1" @click="emit('remove', index)">
                  ×
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div id="pane-points" class="tab-pane fade">
      <div class="table-responsive">
        <table class="table table-sm table-hover align-middle mb-0">
          <thead>
            <tr><th>Name</th><th>Key</th><th>Lat, Lon</th><th>Used</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-if="!pointRows.length">
              <td colspan="5" class="text-body-secondary small">No geographic points defined.</td>
            </tr>
            <tr v-for="row in pointRows" :key="row.key">
              <td>{{ row.point.name }}</td>
              <td class="font-monospace small">{{ row.key }}</td>
              <td class="font-monospace">{{ row.point.latitude }}, {{ row.point.longitude }}</td>
              <td class="text-center">{{ row.usage }}</td>
              <td class="text-end">
                <button type="button" class="btn btn-sm btn-outline-danger" @click="emit('removePoint', row.key)">
                  ×
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
