import { request, requestBlob } from '@/admin/api'
import type {
  DraftState,
  HistoryEntry,
  HistorySummary,
  MaptypeFit,
  PromotableLayer,
  PublishResult,
} from './types'

/** Client for `/admin/georegistration`. Every draft change returns the whole new draft. */

const BASE = '/admin/georegistration'

const segment = encodeURIComponent

function imagePath(maptype: string, image: string): string {
  return `${BASE}/draft/maptypes/${segment(maptype)}/images/${segment(image)}`
}

export const draft = () => request<DraftState>(`${BASE}/draft`)

export const fit = () => request<Record<string, MaptypeFit>>(`${BASE}/draft/fit`)

export const sources = () => request<PromotableLayer[]>(`${BASE}/sources`)

export const createPoint = (point: { name: string; latitude: number; longitude: number }) =>
  request<DraftState & { key: string }>(`${BASE}/draft/points`, { method: 'POST', body: point })

export const updatePoint = (key: string, point: { name: string; latitude: number; longitude: number }) =>
  request<DraftState>(`${BASE}/draft/points/${segment(key)}`, { method: 'PUT', body: point })

export const deletePoint = (key: string, revision: number) =>
  request<DraftState & { removed_annotations: number }>(`${BASE}/draft/points/${segment(key)}`, {
    method: 'DELETE',
    revision,
  })

export const putAnnotation = (
  maptype: string,
  image: string,
  point: string,
  body: { pixel_x: number; pixel_y: number; replaces?: string },
) =>
  request<DraftState>(`${imagePath(maptype, image)}/annotations/${segment(point)}`, { method: 'PUT', body })

export const deleteAnnotation = (maptype: string, image: string, point: string) =>
  request<DraftState>(`${imagePath(maptype, image)}/annotations/${segment(point)}`, { method: 'DELETE' })

export const deleteImage = (maptype: string, image: string, revision: number) =>
  request<DraftState>(imagePath(maptype, image), { method: 'DELETE', revision })

export const setFitThreshold = (maptype: string, meters: number | null) =>
  request<DraftState>(`${BASE}/draft/maptypes/${segment(maptype)}/fit-threshold`, {
    method: 'PUT',
    body: { meters },
  })

export const promote = (model: string, variable: string, hour: number) =>
  request<DraftState & { maptype: string; image: string }>(`${BASE}/draft/images`, {
    method: 'POST',
    body: { model, variable, hour },
  })

export const discard = (revision: number) =>
  request<DraftState>(`${BASE}/draft/discard`, { method: 'POST', revision })

export const publish = (revision: number, note: string) =>
  request<PublishResult>(`${BASE}/publish`, { method: 'POST', body: { revision, note } })

export const history = () => request<HistorySummary[]>(`${BASE}/history`)

export const historyEntry = (id: string) => request<HistoryEntry>(`${BASE}/history/${segment(id)}`)

export const restore = (id: string, revision: number) =>
  request<DraftState>(`${BASE}/history/${segment(id)}/restore`, { method: 'POST', revision })

/** Object URLs for images; images never change, so each is fetched once per page load. */
const imageUrls = new Map<string, Promise<string>>()

export function imageUrl(maptype: string, image: string): Promise<string> {
  const key = `${maptype}/${image}`
  let url = imageUrls.get(key)
  if (!url) {
    url = requestBlob(`${BASE}/images/${segment(maptype)}/${segment(image)}`).then((blob) => URL.createObjectURL(blob))
    url.catch(() => imageUrls.delete(key))
    imageUrls.set(key, url)
  }
  return url
}
