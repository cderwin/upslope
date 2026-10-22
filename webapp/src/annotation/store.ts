import { computed, reactive, watch } from 'vue'
import seedYaml from '../../../data/georegistration/annotations.yaml?raw'
import { emptyDocument, type AnnotationDocument, type GeographicPoint, type PixelAnnotation } from './types'
import { deserialize, normalize, serialize } from './yaml'

const STORAGE_KEY = 'wrf-georegistration-v1'

/** The repo's annotations.yaml, inlined at build time, used on first load. */
function seedDocument(): AnnotationDocument {
  try {
    return deserialize(seedYaml)
  } catch (error) {
    console.warn('Could not parse the seed annotations.yaml', error)
    return emptyDocument()
  }
}

function loadDocument(): AnnotationDocument {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) return normalize(JSON.parse(raw))
  } catch (error) {
    console.warn('Could not read saved annotations', error)
  }
  return seedDocument()
}

/** Turn a display name into a YAML-safe key, matching the Python tool's style. */
export function toKey(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '') || 'point'
  )
}

const doc = reactive<AnnotationDocument>(loadDocument())

watch(
  doc,
  (value) => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(value))
    } catch (error) {
      console.warn('Could not persist annotations', error)
    }
  },
  { deep: true },
)

function replaceContents(next: AnnotationDocument): void {
  for (const key of Object.keys(doc.geographic_points)) delete doc.geographic_points[key]
  for (const key of Object.keys(doc.domains)) delete doc.domains[key]
  Object.assign(doc.geographic_points, next.geographic_points)
  Object.assign(doc.domains, next.domains)
}

export function useAnnotationStore() {
  const points = computed(() => doc.geographic_points)

  /** Points sorted by name, as `[key, point]` pairs, for dropdowns and tables. */
  const sortedPoints = computed(() =>
    Object.entries(doc.geographic_points).sort(([, a], [, b]) => a.name.localeCompare(b.name)),
  )

  function annotationsFor(domain: string, image: string): PixelAnnotation[] {
    if (!domain || !image) return []
    return doc.domains[domain]?.[image] ?? []
  }

  function listFor(domain: string, image: string): PixelAnnotation[] {
    const images = (doc.domains[domain] ??= {})
    return (images[image] ??= [])
  }

  function addAnnotation(domain: string, image: string, annotation: PixelAnnotation): void {
    listFor(domain, image).push(annotation)
  }

  function updateAnnotation(
    domain: string,
    image: string,
    index: number,
    annotation: PixelAnnotation,
  ): void {
    const list = listFor(domain, image)
    if (index >= 0 && index < list.length) list[index] = annotation
  }

  function removeAnnotation(domain: string, image: string, index: number): void {
    const list = listFor(domain, image)
    if (index >= 0 && index < list.length) list.splice(index, 1)
  }

  /** Create or overwrite a geographic point; returns the key actually used. */
  function savePoint(key: string, point: GeographicPoint): string {
    doc.geographic_points[key] = point
    return key
  }

  /** A key based on `name` (or `preferred`) that is not already taken. */
  function uniqueKey(preferred: string): string {
    const base = toKey(preferred)
    let key = base
    let suffix = 2
    while (key in doc.geographic_points) key = `${base}_${suffix++}`
    return key
  }

  function usageCount(key: string): number {
    let count = 0
    for (const images of Object.values(doc.domains))
      for (const list of Object.values(images)) for (const a of list) if (a.point === key) count++
    return count
  }

  /** Delete a point and every pixel annotation referencing it. */
  function removePoint(key: string): void {
    delete doc.geographic_points[key]
    for (const images of Object.values(doc.domains))
      for (const [image, list] of Object.entries(images))
        images[image] = list.filter((a) => a.point !== key)
  }

  function toYaml(): string {
    return serialize(doc)
  }

  function importYaml(text: string): void {
    replaceContents(deserialize(text))
  }

  function reset(): void {
    try {
      localStorage.removeItem(STORAGE_KEY)
    } catch {
      // Nothing to clear.
    }
    replaceContents(seedDocument())
  }

  return {
    document: doc,
    points,
    sortedPoints,
    annotationsFor,
    addAnnotation,
    updateAnnotation,
    removeAnnotation,
    savePoint,
    uniqueKey,
    usageCount,
    removePoint,
    toYaml,
    importYaml,
    reset,
  }
}
