<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBootstrapModal } from '@/annotation/useBootstrapModal'
import { adminToken, clearToken, requestToken } from '@/admin/token'
import { fetchStatus } from '@/forecast/api'
import type { ModelStatus } from '@/forecast/types'
import {
  FRAME_INTERVAL_DEFAULT_MS,
  FRAME_INTERVAL_MAX_MS,
  FRAME_INTERVAL_MIN_MS,
  setDefaultModel,
  setFrameInterval,
  setTerrain,
  settings,
} from './settings'

const open = defineModel<boolean>('open', { required: true })

// The models to choose a default from, fetched when the dialog opens (the status is served from memory).
const models = ref<ModelStatus[]>([])
const serverDefault = ref<string | null>(null)
const modelsError = ref('')
watch(open, async (isOpen) => {
  if (!isOpen || models.value.length) return
  try {
    const status = await fetchStatus()
    models.value = status.models
    serverDefault.value = status.default_model
    modelsError.value = ''
  } catch (e) {
    modelsError.value = e instanceof Error ? e.message : String(e)
  }
})

const serverDefaultName = computed(() => models.value.find((model) => model.id === serverDefault.value)?.name)

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
            <h6>Map</h6>
            <div class="form-check form-switch mb-0">
              <input
                id="settings-terrain"
                class="form-check-input"
                type="checkbox"
                role="switch"
                :checked="settings.terrain"
                @change="setTerrain(($event.target as HTMLInputElement).checked)"
              />
              <label class="form-check-label" for="settings-terrain">3D terrain</label>
              <div class="form-text mt-0">Ctrl-drag or right-drag the map to tilt and rotate.</div>
            </div>
          </section>

          <section>
            <h6>Default model</h6>
            <label class="form-label small mb-1" for="settings-model">
              The model the map opens on, and switches to when the one you're viewing doesn't have a variable.
            </label>
            <select
              id="settings-model"
              class="form-select form-select-sm"
              :value="settings.defaultModel ?? ''"
              @change="setDefaultModel(($event.target as HTMLSelectElement).value)"
            >
              <option value="">Upslope's default{{ serverDefaultName ? ` (${serverDefaultName})` : '' }}</option>
              <option v-for="model in models" :key="model.id" :value="model.id">{{ model.name }}</option>
              <!-- a saved model that's no longer configured -->
              <option
                v-if="settings.defaultModel && models.length && !models.some((model) => model.id === settings.defaultModel)"
                :value="settings.defaultModel"
              >
                {{ settings.defaultModel }} (no longer available)
              </option>
            </select>
            <div v-if="modelsError" class="small text-danger mt-1">Could not load the models: {{ modelsError }}</div>
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
