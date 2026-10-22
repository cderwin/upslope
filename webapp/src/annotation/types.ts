/** A named point on the earth, keyed under `geographic_points` in annotations.yaml. */
export interface GeographicPoint {
  name: string
  latitude: number
  longitude: number
}

/** A pixel in an image tied to a `GeographicPoint` by its key. */
export interface PixelAnnotation {
  pixel_x: number
  pixel_y: number
  point: string
}

/** The shape of annotations.yaml: `domains[domain][image]` holds the pixel list. */
export interface AnnotationDocument {
  geographic_points: Record<string, GeographicPoint>
  domains: Record<string, Record<string, PixelAnnotation[]>>
}

export function emptyDocument(): AnnotationDocument {
  return { geographic_points: {}, domains: {} }
}

/** What the annotate dialog is working on. */
export interface AnnotationDraft {
  /** Index into the image's annotation list, or `null` when adding a new one. */
  index: number | null
  pixel_x: number
  pixel_y: number
}

/** What the annotate dialog hands back: the pixel, plus the point it refers to. */
export interface AnnotationResult {
  index: number | null
  annotation: PixelAnnotation
  /** Key of an existing point to update, or `null` to create one. */
  existingKey: string | null
  point: GeographicPoint
}
