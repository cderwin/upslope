import manifest from 'virtual:georegistration-manifest'

/**
 * The image tree under `data/georegistration`, discovered at build/dev time by
 * the `wrf-georegistration` Vite plugin so the dropdowns stay in sync with disk.
 */
export const IMAGE_MANIFEST: Record<string, string[]> = manifest

export const IMAGE_BASE_URL = '/data/georegistration/images'

export function listDomains(extra: Iterable<string> = []): string[] {
  return [...new Set([...Object.keys(IMAGE_MANIFEST), ...extra])].sort()
}

export function listImages(domain: string, extra: Iterable<string> = []): string[] {
  return [...new Set([...(IMAGE_MANIFEST[domain] ?? []), ...extra])].sort()
}

export function imageUrl(domain: string, image: string): string {
  return `${IMAGE_BASE_URL}/${encodeURIComponent(domain)}/${encodeURIComponent(image)}`
}
