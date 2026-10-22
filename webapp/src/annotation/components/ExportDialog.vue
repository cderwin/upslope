<script setup lang="ts">
import { ref, toRef, watch } from 'vue'
import { useBootstrapModal } from '../useBootstrapModal'

const props = defineProps<{ open: boolean; yaml: string }>()
const emit = defineEmits<{ 'update:open': [value: boolean] }>()

const element = ref<HTMLElement | null>(null)
useBootstrapModal(element, toRef(props, 'open'), () => emit('update:open', false))

const text = ref(props.yaml)
const copyLabel = ref('Copy')

watch(
  () => props.yaml,
  (value) => (text.value = value),
)

async function copy() {
  try {
    await navigator.clipboard.writeText(text.value)
    copyLabel.value = 'Copied'
    setTimeout(() => (copyLabel.value = 'Copy'), 1200)
  } catch {
    copyLabel.value = 'Press ⌘C'
    setTimeout(() => (copyLabel.value = 'Copy'), 1600)
  }
}

function download() {
  const blob = new Blob([text.value], { type: 'text/yaml' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'annotations.yaml'
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
</script>

<template>
  <div ref="element" class="modal fade" tabindex="-1">
    <div class="modal-dialog modal-lg">
      <div class="modal-content">
        <div class="modal-header">
          <h5 class="modal-title">annotations.yaml</h5>
          <button type="button" class="btn-close" @click="emit('update:open', false)"></button>
        </div>
        <div class="modal-body">
          <p class="small text-body-secondary">
            Byte-compatible with the Python tool's output — this can replace
            <code>annotations.yaml</code> as-is.
          </p>
          <textarea v-model="text" class="form-control font-monospace yaml" rows="20" spellcheck="false"></textarea>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-outline-secondary" @click="copy">{{ copyLabel }}</button>
          <button type="button" class="btn btn-primary" @click="download">Download</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.yaml {
  font-size: 12px;
}
</style>
