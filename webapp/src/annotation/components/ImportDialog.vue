<script setup lang="ts">
import { ref, toRef, watch } from 'vue'
import { useBootstrapModal } from '../useBootstrapModal'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{
  'update:open': [value: boolean]
  import: [text: string]
}>()

const element = ref<HTMLElement | null>(null)
useBootstrapModal(element, toRef(props, 'open'), () => emit('update:open', false))

const text = ref('')
const error = ref('')

watch(
  () => props.open,
  (open) => {
    if (open) {
      text.value = ''
      error.value = ''
    }
  },
)

function onFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => (text.value = String(reader.result))
  reader.readAsText(file)
}

function submit() {
  try {
    emit('import', text.value)
  } catch (cause) {
    error.value = `Could not parse: ${(cause as Error).message}`
  }
}

/** Let the parent report a failure without closing the dialog. */
function fail(message: string) {
  error.value = message
}

defineExpose({ fail })
</script>

<template>
  <div ref="element" class="modal fade" tabindex="-1">
    <div class="modal-dialog modal-lg">
      <form class="modal-content" @submit.prevent="submit">
        <div class="modal-header">
          <h5 class="modal-title">Import annotations.yaml</h5>
          <button type="button" class="btn-close" @click="emit('update:open', false)"></button>
        </div>
        <div class="modal-body">
          <p class="small text-body-secondary">
            Paste the contents of an <code>annotations.yaml</code> file, or choose the file. This
            replaces everything currently saved in this browser.
          </p>
          <input type="file" class="form-control mb-2" accept=".yaml,.yml,text/yaml" @change="onFile" />
          <textarea v-model="text" class="form-control font-monospace yaml" rows="16" spellcheck="false"></textarea>
          <div v-if="error" class="alert alert-danger mt-2 mb-0 py-2">{{ error }}</div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" @click="emit('update:open', false)">Cancel</button>
          <button type="submit" class="btn btn-primary">Replace state</button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.yaml {
  font-size: 12px;
}
</style>
