/**
 * Upper-air soundings (radiosonde launches) from the Iowa Environmental Mesonet, fetched straight from the
 * browser: IEM sends `Access-Control-Allow-Origin: *`, and the data needs no processing beyond unit conversion.
 */

const IEM = 'https://mesonet.agron.iastate.edu'

/** Stations are listed by their distance from here. */
const SEATTLE = { lat: 47.61, lon: -122.33 }

const EARTH_RADIUS_MI = 3958.8
const FEET_PER_METER = 3.28084
const MS_PER_KNOT = 0.514444
/** Balloons go up at 00Z and 12Z; how many of those to look back through for a station's latest sounding. */
const CYCLE_HOURS = 12
const CYCLES_TO_SEARCH = 4

export interface Station {
  /** IEM's id, e.g. `KUIL`; `_ABR`-style ids combine a site's successive identifiers. */
  id: string
  name: string
  /** State or province, if IEM has one. */
  region: string | null
  country: string
  elevationFt: number | null
  distanceMi: number
}

/** One level in the units skewt-plus takes (the "GSD sounding format"). */
export interface SoundingLevel {
  /** hPa */
  press: number
  /** m */
  hght: number
  /** °C */
  temp: number
  /** °C */
  dwpt: number
  /** Degrees; NaN when the level has no wind. */
  wdir: number
  /** m/s; NaN when the level has no wind. */
  wspd: number
}

export interface Sounding {
  /** The identifier the launch was reported under, e.g. `KABR` for station `_ABR`. */
  station: string
  /** ISO 8601. */
  valid: string
  /** Surface first (highest pressure). */
  levels: SoundingLevel[]
}

interface StationFeature {
  id: string
  properties: { sname: string; state: string | null; country: string; elevation: number | null; archive_end: string | null }
  geometry: { coordinates: [number, number] }
}

interface RaobLevel {
  pres: number | null
  hght: number | null
  tmpc: number | null
  dwpc: number | null
  drct: number | null
  sknt: number | null
}

interface RaobResponse {
  profiles: { station: string; valid: string; profile: RaobLevel[] }[]
}

async function getJson<T>(url: string, what: string, signal: AbortSignal | null): Promise<T> {
  const response = await fetch(url, { signal })
  if (!response.ok) throw new Error(`Could not load ${what} (${response.status} ${response.statusText})`)
  return (await response.json()) as T
}

/** Stations still launching (IEM's `online` flag isn't reliable; an open archive is), nearest Seattle first. */
export async function fetchStations(signal: AbortSignal | null = null): Promise<Station[]> {
  const data = await getJson<{ features: StationFeature[] }>(
    `${IEM}/geojson/network/RAOB.geojson`,
    'the sounding stations',
    signal,
  )
  return data.features
    .filter((feature) => feature.properties.archive_end === null)
    .map((feature) => {
      const [lon, lat] = feature.geometry.coordinates
      const { sname, state, country, elevation } = feature.properties
      // Most names end in their region (`Spokane  WA/US`, `Vernon,  BC/CA`), whose `state` is then null;
      // combined sites (`Aberdeen Area -- KHON KABR`) have a `state` instead.
      const suffixed = /^(.*?)[\s,]+([A-Z]{2})\/[A-Z]{2}$/.exec(sname)
      return {
        id: feature.id,
        name: (suffixed?.[1] ?? sname).trim(),
        region: suffixed?.[2] ?? (state?.trim() || null),
        country,
        elevationFt: elevation === null ? null : Math.round(elevation * FEET_PER_METER),
        distanceMi: distanceMi(SEATTLE, { lat, lon }),
      }
    })
    .sort((a, b) => a.distanceMi - b.distanceMi)
}

/** e.g. `KUIL · Quillayute State, WA, US`. */
export function stationLabel(station: Station): string {
  // `_ABR`-style ids combine a site's identifiers; the name lists them.
  const where = [station.region, station.country].filter(Boolean).join(', ')
  return `${station.id.replace(/^_/, '')} · ${station.name}${where ? `, ${where}` : ''}`
}

/** Great-circle distance. */
function distanceMi(a: { lat: number; lon: number }, b: { lat: number; lon: number }): number {
  const rad = Math.PI / 180
  const dLat = (b.lat - a.lat) * rad
  const dLon = (b.lon - a.lon) * rad
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2
  return 2 * EARTH_RADIUS_MI * Math.asin(Math.sqrt(h))
}

/** The 00Z or 12Z launch at or before `now`. */
function latestCycle(now: Date): Date {
  const cycle = new Date(now)
  cycle.setUTCHours(Math.floor(now.getUTCHours() / CYCLE_HOURS) * CYCLE_HOURS, 0, 0, 0)
  return cycle
}

/**
 * A station's most recent sounding, looking back through the last few launches (one can take a couple of hours
 * to reach IEM, and stations miss some). Null if there's none in that window.
 */
export async function fetchLatestSounding(
  station: string,
  signal: AbortSignal | null = null,
  now: Date = new Date(),
): Promise<Sounding | null> {
  const cycle = latestCycle(now)
  for (let i = 0; i < CYCLES_TO_SEARCH; i++) {
    const sounding = await fetchSounding(station, cycle, signal)
    if (sounding) return sounding
    cycle.setUTCHours(cycle.getUTCHours() - CYCLE_HOURS)
  }
  return null
}

async function fetchSounding(station: string, time: Date, signal: AbortSignal | null): Promise<Sounding | null> {
  const ts = time.toISOString().replace(/\.\d{3}Z$/, 'Z')
  const params = new URLSearchParams({ station, ts })
  const data = await getJson<RaobResponse>(`${IEM}/json/raob.py?${params}`, `the ${station} sounding`, signal)
  const profile = data.profiles[0]
  if (!profile) return null
  const levels = profile.profile.flatMap(toLevel).sort((a, b) => b.press - a.press)
  return levels.length ? { station: profile.station, valid: profile.valid, levels } : null
}

/** IEM's level in skewt-plus's units; levels missing a temperature or dewpoint can't be plotted. */
function toLevel(level: RaobLevel): SoundingLevel[] {
  const { pres, hght, tmpc, dwpc, drct, sknt } = level
  if (pres === null || hght === null || tmpc === null || dwpc === null) return []
  const windy = drct !== null && sknt !== null
  return [{ press: pres, hght, temp: tmpc, dwpt: dwpc, wdir: windy ? drct : NaN, wspd: windy ? sknt * MS_PER_KNOT : NaN }]
}
