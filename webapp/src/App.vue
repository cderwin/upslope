<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import { Moon, Settings, Sun } from '@lucide/vue'
import { RouterLink, RouterView } from 'vue-router'
import TokenDialog from '@/admin/TokenDialog.vue'
import UpslopeMark from '@/components/UpslopeMark.vue'
import { adminToken } from '@/admin/token'
import RunStatusDialog from '@/forecast/components/RunStatusDialog.vue'
import { forecastStatus, openRunStatus, refreshStatus, statusLabel, statusTone } from '@/forecast/status'
import SettingsDialog from '@/settings/SettingsDialog.vue'
import { openSettings, settingsDialog } from '@/settings/settings'
import { useTheme } from '@/theme/useTheme'

const { theme, toggle: toggleTheme } = useTheme()

/** How often the navbar's status dot refreshes. */
const STATUS_REFRESH_MS = 60_000
let statusTimer: number | undefined

onMounted(() => {
  void refreshStatus()
  statusTimer = window.setInterval(() => void refreshStatus(), STATUS_REFRESH_MS)
})

onBeforeUnmount(() => window.clearInterval(statusTimer))
</script>

<template>
  <!-- Phones get the map alone: its ⋮ menu has Settings and Model runs, and the other pages aren't needed there. -->
  <nav class="navbar navbar-expand d-none d-sm-flex">
    <div class="container-fluid">
      <RouterLink class="navbar-brand d-flex align-items-center gap-2" :to="{ name: 'map' }">
        <UpslopeMark :size="28" />
        Upslope
      </RouterLink>
      <ul class="navbar-nav me-auto">
        <li class="nav-item">
          <RouterLink class="nav-link" :to="{ name: 'map' }">Map</RouterLink>
        </li>
        <li class="nav-item">
          <RouterLink class="nav-link" :to="{ name: 'soundings' }">Soundings</RouterLink>
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
        class="btn btn-link nav-link px-2 d-flex align-items-center"
        :title="statusLabel"
        :aria-label="statusLabel"
        @click="openRunStatus"
      >
        <span class="status-dot" :class="`bg-${statusTone}`" aria-hidden="true"></span>
      </button>
      <button
        type="button"
        class="btn btn-link nav-link px-2 d-flex align-items-center"
        :title="theme === 'dark' ? 'Switch to the light theme' : 'Switch to the dark theme'"
        :aria-label="theme === 'dark' ? 'Switch to the light theme' : 'Switch to the dark theme'"
        @click="toggleTheme"
      >
        <Sun v-if="theme === 'dark'" :size="18" aria-hidden="true" />
        <Moon v-else :size="18" aria-hidden="true" />
      </button>
      <button
        type="button"
        class="btn btn-link nav-link px-2"
        title="Settings"
        aria-label="Settings"
        @click="openSettings"
      >
        <Settings :size="18" aria-hidden="true" />
      </button>
    </div>
  </nav>

  <main>
    <RouterView />
  </main>
  <TokenDialog />
  <SettingsDialog v-model:open="settingsDialog.open" />
  <RunStatusDialog v-model:open="forecastStatus.dialogOpen" />
</template>

<style scoped>
.status-dot {
  display: inline-block;
  width: 0.75rem;
  height: 0.75rem;
  border-radius: 50%;
}
</style>
