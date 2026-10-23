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
  /** Set while a hide we started is animating, so its `hidden` event isn't taken as a dismissal. */
  let hidingForState = false

  function handleHidden() {
    if (hidingForState) {
      hidingForState = false
      // `open` flipped back on mid-animation, when Bootstrap ignores show(); show it now.
      if (open.value) instance.value?.show()
      return
    }
    if (open.value) onHidden()
  }

  // Bootstrap ignores hide() while the show animation runs, so `open` can turn false
  // mid-animation (e.g. an action button clicked right away); close once it finishes.
  function handleShown() {
    if (open.value) return
    hidingForState = true
    instance.value?.hide()
  }

  onMounted(() => {
    if (!element.value) return
    instance.value = new Modal(element.value)
    element.value.addEventListener('hidden.bs.modal', handleHidden)
    element.value.addEventListener('shown.bs.modal', handleShown)
    if (open.value) instance.value.show()
  })

  onBeforeUnmount(() => {
    element.value?.removeEventListener('hidden.bs.modal', handleHidden)
    element.value?.removeEventListener('shown.bs.modal', handleShown)
    instance.value?.dispose()
    instance.value = null
  })

  watch(open, (isOpen) => {
    if (isOpen) instance.value?.show()
    else if (element.value?.classList.contains('show')) {
      hidingForState = true
      instance.value?.hide()
    }
  })

  return { hide: () => instance.value?.hide() }
}
