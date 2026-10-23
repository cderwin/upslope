<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <div class="container py-5">
    <h1 class="h3">Not found</h1>
    <p class="text-body-secondary">That page does not exist.</p>
    <RouterLink class="btn btn-outline-secondary" :to="{ name: 'map' }">Back to the map</RouterLink>
  </div>
</template>
