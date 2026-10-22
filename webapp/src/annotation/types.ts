/** Shapes of the admin georegistration API (python `upslope.models.uw_wrf.georegistration_document`). */

/** A landmark on the earth, keyed by point key. */
export interface GeographicPoint {
  name: string
  latitude: number
  longitude: number
}

/** Where a landmark appears in an image (keyed by point key in `AnnotatedImage.annotations`). */
export interface PixelAnnotation {
  pixel_x: number
  pixel_y: number
}

/** The forecast frame an image was promoted from. */
export interface ImageSource {
  model: string
  variable: string
  hour: number
  initialized: string
  promoted_at: string
}

export interface AnnotatedImage {
  width: number
  height: number
  /** `null` for images carried over from the original repo data. */
  source: ImageSource | null
  annotations: Record<string, PixelAnnotation>
}

export interface MaptypeAnnotations {
  images: Record<string, AnnotatedImage>
  /** Mean fit error (meters) above which publishing warns. */
  fit_threshold_m: number | null
}

export interface AnnotationDocument {
  revision: number
  geographic_points: Record<string, GeographicPoint>
  maptypes: Record<string, MaptypeAnnotations>
}

export interface DraftState {
  document: AnnotationDocument
  published_revision: number | null
}

/** How well a maptype's annotations fit its pixel-to-map transform. */
export interface MaptypeFit {
  control_points: number
  mean_error_m: number | null
  fit_threshold_m: number | null
  over_threshold: boolean
  image_errors: Record<string, number>
  point_errors: Record<string, number>
  error: string | null
}

/** A configured forecast layer whose frames can be promoted for annotation. */
export interface PromotableLayer {
  model: string
  variable: string
  maptype: string
  hours: number[]
}

export interface HistorySummary {
  id: string
  published_at: string
  revision: number
}

export interface HistoryEntry {
  id: string
  published_at: string
  note: string | null
  document: AnnotationDocument
}

export interface PublishResult {
  history: HistorySummary
  warnings: string[]
}

/** A pixel annotation together with its point key, for lists and markers. */
export interface ImageAnnotation extends PixelAnnotation {
  point: string
}

/** What the annotate dialog is working on. */
export interface AnnotationDraft {
  /** Point of the annotation being edited, or `null` when adding a new one. */
  point: string | null
  pixel_x: number
  pixel_y: number
}

/** What the annotate dialog hands back. */
export interface AnnotationResult {
  /** The annotation's point before the edit, if it was an existing annotation. */
  previousPoint: string | null
  /** Key of an existing point, or `null` to create a new one. */
  pointKey: string | null
  point: GeographicPoint
  pixel_x: number
  pixel_y: number
}
