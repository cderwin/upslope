<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <div class="container py-5">
    <h1 class="h3">Upslope</h1>
    <p class="text-body-secondary">Forecast maps and georegistration tooling for UW WRF output.</p>

    <div class="vstack gap-3" style="max-width: 32rem">
      <div class="card">
        <div class="card-body">
          <h2 class="h5 card-title">Georegistration</h2>
          <p class="card-text">
            Tie pixels in a WRF output image to geographic coordinates, and export them in the
            <code>annotations.yaml</code> format the Python tooling reads.
          </p>
          <RouterLink class="btn btn-primary" :to="{ name: 'annotation' }">Open /annotation</RouterLink>
        </div>
      </div>

      <div class="card">
        <div class="card-body">
          <h2 class="h5 card-title">Forecast map</h2>
          <p class="card-text">
            Overlay georegistered forecast frames on a map, and animate through the forecast hours.
          </p>
          <RouterLink class="btn btn-primary" :to="{ name: 'map' }">Open /map</RouterLink>
        </div>
      </div>
    </div>
  </div>
</template>
