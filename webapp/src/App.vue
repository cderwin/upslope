<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import TokenDialog from '@/admin/TokenDialog.vue'
import { adminToken } from '@/admin/token'
import SettingsDialog from '@/settings/SettingsDialog.vue'

const settingsOpen = ref(false)

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
      <RouterLink class="navbar-brand" :to="{ name: 'map' }">Upslope</RouterLink>
      <ul class="navbar-nav me-auto">
        <li class="nav-item">
          <RouterLink class="nav-link" :to="{ name: 'map' }">Map</RouterLink>
        </li>
        <li class="nav-item">
          <RouterLink class="nav-link" :to="{ name: 'about' }">About</RouterLink>
        </li>
        <!-- Only admins (a stored token) can use the annotation tool. -->
        <li v-if="adminToken.token" class="nav-item">
          <RouterLink class="nav-link" :to="{ name: 'annotation' }">Annotation</RouterLink>
        </li>
      </ul>
      <button
        type="button"
        class="btn btn-link nav-link px-2"
        title="Settings"
        aria-label="Settings"
        @click="settingsOpen = true"
      >
        <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
          <path
            d="M9.405 1.05c-.413-1.4-2.397-1.4-2.81 0l-.1.34a1.464 1.464 0 0 1-2.105.872l-.31-.17c-1.283-.698-2.686.705-1.987 1.987l.169.311c.446.82.023 1.841-.872 2.105l-.34.1c-1.4.413-1.4 2.397 0 2.81l.34.1a1.464 1.464 0 0 1 .872 2.105l-.17.31c-.698 1.283.705 2.686 1.987 1.987l.311-.169a1.464 1.464 0 0 1 2.105.872l.1.34c.413 1.4 2.397 1.4 2.81 0l.1-.34a1.464 1.464 0 0 1 2.105-.872l.31.17c1.283.698 2.686-.705 1.987-1.987l-.169-.311a1.464 1.464 0 0 1 .872-2.105l.34-.1c1.4-.413 1.4-2.397 0-2.81l-.34-.1a1.464 1.464 0 0 1-.872-2.105l.17-.31c.698-1.283-.705-2.686-1.987-1.987l-.311.169a1.464 1.464 0 0 1-2.105-.872zM8 10.93a2.929 2.929 0 1 1 0-5.86 2.929 2.929 0 0 1 0 5.858z"
          />
        </svg>
      </button>
    </div>
  </nav>

  <main>
    <RouterView />
  </main>
  <TokenDialog />
  <SettingsDialog v-model:open="settingsOpen" />
</template>
