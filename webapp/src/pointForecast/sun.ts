/**
 * Sunrise and sunset, for shading nights on the point forecast's charts. NOAA's simplified solar position
 * (https://gml.noaa.gov/grad/solcalc/solareqns.PDF): good to a minute or two, plenty for shading.
 */

const RAD = Math.PI / 180
const DAY_MS = 86_400_000

/** Sunrise and sunset (ms) on the UTC day containing `dayMs`; null when the sun doesn't rise or set that day. */
function sunTimes(dayMs: number, lat: number, lon: number): { rise: number; set: number } | null {
  const midnight = Math.floor(dayMs / DAY_MS) * DAY_MS
  const start = Date.UTC(new Date(midnight).getUTCFullYear(), 0, 1)
  const dayOfYear = Math.round((midnight - start) / DAY_MS)
  const gamma = ((2 * Math.PI) / 365) * dayOfYear // fractional year, at noon
  const equationOfTime =
    229.18 *
    (0.000075 +
      0.001868 * Math.cos(gamma) -
      0.032077 * Math.sin(gamma) -
      0.014615 * Math.cos(2 * gamma) -
      0.040849 * Math.sin(2 * gamma))
  const declination =
    0.006918 -
    0.399912 * Math.cos(gamma) +
    0.070257 * Math.sin(gamma) -
    0.006758 * Math.cos(2 * gamma) +
    0.000907 * Math.sin(2 * gamma) -
    0.002697 * Math.cos(3 * gamma) +
    0.00148 * Math.sin(3 * gamma)
  // the sun's centre 0.833° below the horizon: refraction and its radius
  const cosHourAngle =
    Math.cos(90.833 * RAD) / (Math.cos(lat * RAD) * Math.cos(declination)) - Math.tan(lat * RAD) * Math.tan(declination)
  if (cosHourAngle < -1 || cosHourAngle > 1) return null
  const hourAngle = Math.acos(cosHourAngle) / RAD
  const noonMinutes = 720 - 4 * lon - equationOfTime
  return {
    rise: midnight + (noonMinutes - 4 * hourAngle) * 60_000,
    set: midnight + (noonMinutes + 4 * hourAngle) * 60_000,
  }
}

/** The nights (sunset to the next sunrise, ms) overlapping [start, end]. */
export function nights(start: number, end: number, lat: number, lon: number): [number, number][] {
  const result: [number, number][] = []
  for (let day = start - DAY_MS; day <= end + DAY_MS; day += DAY_MS) {
    const today = sunTimes(day, lat, lon)
    const tomorrow = sunTimes(day + DAY_MS, lat, lon)
    if (!today || !tomorrow) continue
    if (tomorrow.rise > start && today.set < end) result.push([Math.max(today.set, start), Math.min(tomorrow.rise, end)])
  }
  return result
}
