<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useBootstrapModal } from '@/annotation/useBootstrapModal'
import { adminToken, dismissPrompt, setToken } from './token'

const element = ref<HTMLElement | null>(null)
const open = computed(() => adminToken.prompt !== null)
useBootstrapModal(element, open, dismissPrompt)

const value = ref('')
watch(open, (isOpen) => {
  if (isOpen) value.value = ''
})

function submit() {
  if (value.value.trim()) setToken(value.value)
}
</script>

<template>
  <div ref="element" class="modal fade" tabindex="-1">
    <div class="modal-dialog">
      <form class="modal-content" @submit.prevent="submit">
        <div class="modal-header">
          <h5 class="modal-title">Admin token</h5>
          <button type="button" class="btn-close" @click="dismissPrompt"></button>
        </div>
        <div class="modal-body">
          <div v-if="adminToken.prompt === 'rejected'" class="alert alert-danger py-2">
            That token was rejected. Check it and try again.
          </div>
          <p class="small text-body-secondary">
            Editing annotations needs the server's <code>ADMIN_TOKEN</code>. It is kept for this tab only.
          </p>
          <label class="form-label mb-1" for="field-token">Token</label>
          <input id="field-token" v-model="value" type="password" class="form-control font-monospace" autocomplete="off" required />
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" @click="dismissPrompt">Cancel</button>
          <button type="submit" class="btn btn-primary">Save</button>
        </div>
      </form>
    </div>
  </div>
</template>
