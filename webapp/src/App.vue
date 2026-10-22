<script setup lang="ts">
import { onMounted } from 'vue'
import { RouterLink, RouterView } from 'vue-router'

onMounted(() => {
  // Follow the OS theme; Bootstrap 5.3 restyles itself from this attribute.
  const dark = window.matchMedia('(prefers-color-scheme: dark)')
  const apply = () => document.documentElement.setAttribute('data-bs-theme', dark.matches ? 'dark' : 'light')
  apply()
  dark.addEventListener('change', apply)
})
</script>

<template>
  <nav class="navbar navbar-expand bg-body-tertiary border-bottom">
    <div class="container-fluid">
      <RouterLink class="navbar-brand" :to="{ name: 'home' }">Upslope</RouterLink>
      <ul class="navbar-nav">
        <li class="nav-item">
          <RouterLink class="nav-link" :to="{ name: 'annotation' }">Annotation</RouterLink>
        </li>
        <li class="nav-item">
          <RouterLink class="nav-link" :to="{ name: 'map' }">Map</RouterLink>
        </li>
      </ul>
    </div>
  </nav>

  <main>
    <RouterView />
  </main>
</template>
