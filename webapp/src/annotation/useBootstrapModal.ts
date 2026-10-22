import { onBeforeUnmount, onMounted, ref, watch, type Ref } from 'vue'
import { Modal } from 'bootstrap'

/**
 * Drive a Bootstrap modal from a reactive `open` flag, and report dismissals
 * (backdrop click, Escape, close button) back to the caller.
 */
export function useBootstrapModal(
  element: Ref<HTMLElement | null>,
  open: Ref<boolean>,
  onHidden: () => void,
) {
  const instance = ref<Modal | null>(null)

  function handleHidden() {
    if (open.value) onHidden()
  }

  onMounted(() => {
    if (!element.value) return
    instance.value = new Modal(element.value)
    element.value.addEventListener('hidden.bs.modal', handleHidden)
    if (open.value) instance.value.show()
  })

  onBeforeUnmount(() => {
    element.value?.removeEventListener('hidden.bs.modal', handleHidden)
    instance.value?.dispose()
    instance.value = null
  })

  watch(open, (isOpen) => {
    if (isOpen) instance.value?.show()
    else instance.value?.hide()
  })

  return { hide: () => instance.value?.hide() }
}
