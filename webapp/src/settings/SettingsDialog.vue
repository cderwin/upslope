<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBootstrapModal } from '@/annotation/useBootstrapModal'
import { adminToken, clearToken, requestToken } from '@/admin/token'
import {
  FRAME_INTERVAL_DEFAULT_MS,
  FRAME_INTERVAL_MAX_MS,
  FRAME_INTERVAL_MIN_MS,
  setFrameInterval,
  settings,
} from './settings'

const open = defineModel<boolean>('open', { required: true })

const element = ref<HTMLElement | null>(null)
useBootstrapModal(element, open, () => (open.value = false))

const route = useRoute()
const router = useRouter()

const seconds = computed({
  get: () => settings.frameIntervalMs / 1000,
  set: (value: number) => setFrameInterval(value * 1000),
})

function logOut() {
  clearToken()
  open.value = false
  // The annotation page is only for logged-in users.
  if (route.name === 'annotation' || route.name === 'annotation-image') void router.push({ name: 'map' })
}

function logIn() {
  open.value = false
  requestToken('missing')
}
</script>

<template>
  <div ref="element" class="modal fade" tabindex="-1" aria-labelledby="settings-title">
    <div class="modal-dialog">
      <div class="modal-content">
        <div class="modal-header">
          <h5 id="settings-title" class="modal-title">Settings</h5>
          <button type="button" class="btn-close" aria-label="Close" @click="open = false"></button>
        </div>
        <div class="modal-body vstack gap-4">
          <section>
            <h6>Playback speed</h6>
            <label class="form-label small mb-1 d-flex justify-content-between" for="settings-speed">
              <span>Time per frame</span>
              <span class="fw-semibold font-monospace">{{ seconds.toFixed(2) }} s</span>
            </label>
            <input
              id="settings-speed"
              v-model.number="seconds"
              class="form-range"
              type="range"
              :min="FRAME_INTERVAL_MIN_MS / 1000"
              :max="FRAME_INTERVAL_MAX_MS / 1000"
              step="0.25"
            />
            <div class="d-flex justify-content-between small text-body-secondary">
              <span>Faster</span>
              <button
                type="button"
                class="btn btn-link btn-sm p-0"
                :disabled="settings.frameIntervalMs === FRAME_INTERVAL_DEFAULT_MS"
                @click="setFrameInterval(FRAME_INTERVAL_DEFAULT_MS)"
              >
                Reset
              </button>
              <span>Slower</span>
            </div>
          </section>

          <section>
            <h6>Admin</h6>
            <template v-if="adminToken.token">
              <p class="small text-body-secondary">You're logged in with the admin token.</p>
              <button type="button" class="btn btn-outline-danger btn-sm" @click="logOut">Log out</button>
            </template>
            <template v-else>
              <p class="small text-body-secondary">Log in with the admin token to edit annotations.</p>
              <button type="button" class="btn btn-outline-primary btn-sm" @click="logIn">Log in</button>
            </template>
          </section>
        </div>
      </div>
    </div>
  </div>
</template>
