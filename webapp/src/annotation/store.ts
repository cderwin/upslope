import { computed, reactive, readonly } from 'vue'
import { ApiError, AuthError, ConflictError } from '@/admin/api'
import * as api from './api'
import type { AnnotationDocument, AnnotationResult, DraftState, ImageAnnotation, MaptypeFit } from './types'

/**
 * The shared annotation draft, as last returned by the admin API.
 *
 * Every change is sent to the server, which answers with the whole new draft; the
 * store never edits the document itself. Destructive changes carry the draft
 * revision, so they fail (and the draft reloads) if someone else changed it first.
 */

const state = reactive({
  document: null as AnnotationDocument | null,
  publishedRevision: null as number | null,
  fit: {} as Record<string, MaptypeFit>,
  loading: false,
  busy: false,
  error: '',
  problems: [] as string[],
  notice: '',
})

/** Turn a display name into a point key, as the server does (for previews only). */
export function toKey(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '')
      .slice(0, 96) || 'point'
  )
}

let fitRequest = 0

async function refreshFit(): Promise<void> {
  const current = ++fitRequest
  try {
    const fit = await api.fit()
    if (current === fitRequest) state.fit = fit
  } catch {
    // The fit is advisory; the next change retries.
  }
}

function apply(draft: DraftState): void {
  state.document = draft.document
  state.publishedRevision = draft.published_revision
  void refreshFit()
}

function reportError(error: unknown): void {
  if (error instanceof AuthError) {
    state.error = ''
    return
  }
  state.error = error instanceof Error ? error.message : String(error)
  state.problems = error instanceof ApiError ? error.problems : []
}

async function load(): Promise<void> {
  state.loading = true
  try {
    apply(await api.draft())
    state.error = ''
  } catch (error) {
    reportError(error)
  } finally {
    state.loading = false
  }
}

/** Run a change, keeping `busy` set meanwhile. Returns its result, or `undefined` if it failed. */
async function run<T>(change: () => Promise<T>): Promise<T | undefined> {
  state.busy = true
  state.error = ''
  state.problems = []
  state.notice = ''
  try {
    return await change()
  } catch (error) {
    if (error instanceof ConflictError) {
      state.notice = 'Someone else changed the annotations meanwhile, so they were reloaded. Check and try again.'
      await load()
    } else {
      reportError(error)
    }
    return undefined
  } finally {
    state.busy = false
  }
}

function revision(): number {
  return state.document?.revision ?? 0
}

export function useAnnotationStore() {
  const maptypes = computed(() => Object.keys(state.document?.maptypes ?? {}).sort())

  function images(maptype: string): string[] {
    return Object.keys(state.document?.maptypes[maptype]?.images ?? {}).sort()
  }

  function image(maptype: string, imageId: string) {
    return state.document?.maptypes[maptype]?.images[imageId]
  }

  function annotationsFor(maptype: string, imageId: string): ImageAnnotation[] {
    const annotations = image(maptype, imageId)?.annotations ?? {}
    return Object.entries(annotations)
      .map(([point, a]) => ({ point, ...a }))
      .sort((a, b) => a.point.localeCompare(b.point))
  }

  const points = computed(() => state.document?.geographic_points ?? {})

  /** Points sorted by name, as `[key, point]` pairs. */
  const sortedPoints = computed(() =>
    Object.entries(points.value).sort(([, a], [, b]) => a.name.localeCompare(b.name)),
  )

  function usageCount(key: string): number {
    let count = 0
    for (const annotations of Object.values(state.document?.maptypes ?? {}))
      for (const img of Object.values(annotations.images)) if (key in img.annotations) count++
    return count
  }

  const hasUnpublishedChanges = computed(
    () => state.document !== null && state.document.revision !== state.publishedRevision,
  )

  /** Save the annotate dialog's result: create or update the point, then place it on the image. */
  async function saveAnnotation(maptype: string, imageId: string, result: AnnotationResult): Promise<boolean> {
    const done = await run(async () => {
      let key = result.pointKey
      if (key === null) {
        const created = await api.createPoint(result.point)
        apply(created)
        key = created.key
      } else {
        const current = points.value[key]
        if (
          !current ||
          current.name !== result.point.name ||
          current.latitude !== result.point.latitude ||
          current.longitude !== result.point.longitude
        )
          apply(await api.updatePoint(key, result.point))
      }
      const body = { pixel_x: result.pixel_x, pixel_y: result.pixel_y }
      const replaces = result.previousPoint && result.previousPoint !== key ? { replaces: result.previousPoint } : {}
      apply(await api.putAnnotation(maptype, imageId, key, { ...body, ...replaces }))
      return true
    })
    return done ?? false
  }

  async function removeAnnotation(maptype: string, imageId: string, point: string): Promise<void> {
    await run(async () => apply(await api.deleteAnnotation(maptype, imageId, point)))
  }

  async function removePoint(key: string): Promise<void> {
    await run(async () => apply(await api.deletePoint(key, revision())))
  }

  async function removeImage(maptype: string, imageId: string): Promise<void> {
    await run(async () => apply(await api.deleteImage(maptype, imageId, revision())))
  }

  async function setFitThreshold(maptype: string, meters: number | null): Promise<void> {
    await run(async () => apply(await api.setFitThreshold(maptype, meters)))
  }

  /** Add a forecast frame for annotation. Returns where it went, or `undefined` on failure. */
  async function promote(model: string, variable: string, hour: number) {
    return run(async () => {
      const result = await api.promote(model, variable, hour)
      apply(result)
      return { maptype: result.maptype, image: result.image }
    })
  }

  async function discard(): Promise<void> {
    await run(async () => apply(await api.discard(revision())))
  }

  async function restore(historyId: string): Promise<boolean> {
    return (await run(async () => (apply(await api.restore(historyId, revision())), true))) ?? false
  }

  /** Publish the draft as it is now. Returns warnings on success, or `undefined` on failure. */
  async function publish(note: string): Promise<string[] | undefined> {
    return run(async () => {
      const result = await api.publish(revision(), note)
      state.publishedRevision = result.history.revision
      return result.warnings
    })
  }

  function clearMessages(): void {
    state.error = ''
    state.problems = []
    state.notice = ''
  }

  return {
    state: readonly(state),
    maptypes,
    images,
    image,
    annotationsFor,
    points,
    sortedPoints,
    usageCount,
    hasUnpublishedChanges,
    load,
    saveAnnotation,
    removeAnnotation,
    removePoint,
    removeImage,
    setFitThreshold,
    promote,
    discard,
    restore,
    publish,
    clearMessages,
  }
}
