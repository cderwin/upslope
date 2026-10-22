import { emptyDocument, type AnnotationDocument } from './types'

/**
 * Serialisation for annotations.yaml.
 *
 * `serialize` is byte-compatible with PyYAML's
 * `yaml.dump(doc, default_flow_style=False, sort_keys=False)`, so its output can
 * replace the file the Python tooling reads and writes.
 *
 * `parse` is a minimal indentation-based reader covering the subset that file
 * uses — nested maps, lists of maps, and scalars. It is not general YAML.
 */

/* -------------------------------------------------------------- serialise */

function scalar(value: string | number): string {
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : 'null'

  // Emit bare text only when it cannot be read back as a number, bool or null.
  const looksNumeric = value !== '' && !Number.isNaN(Number(value))
  const plain =
    /^[A-Za-z0-9][A-Za-z0-9 _.,'()/+-]*$/.test(value) &&
    !/^(true|false|null|yes|no|on|off)$/i.test(value) &&
    !looksNumeric

  return plain ? value : `'${value.replace(/'/g, "''")}'`
}

export function serialize(doc: AnnotationDocument): string {
  const lines: string[] = []

  const pointKeys = Object.keys(doc.geographic_points)
  lines.push(pointKeys.length ? 'geographic_points:' : 'geographic_points: {}')
  for (const key of pointKeys) {
    const point = doc.geographic_points[key]!
    lines.push(`  ${scalar(key)}:`)
    lines.push(`    name: ${scalar(point.name)}`)
    lines.push(`    latitude: ${scalar(point.latitude)}`)
    lines.push(`    longitude: ${scalar(point.longitude)}`)
  }

  const domainKeys = Object.keys(doc.domains)
  lines.push(domainKeys.length ? 'domains:' : 'domains: {}')
  for (const domain of domainKeys) {
    const images = doc.domains[domain]!
    const imageKeys = Object.keys(images)
    lines.push(imageKeys.length ? `  ${scalar(domain)}:` : `  ${scalar(domain)}: {}`)
    for (const image of imageKeys) {
      const list = images[image]!
      if (!list.length) {
        lines.push(`    ${scalar(image)}: []`)
        continue
      }
      lines.push(`    ${scalar(image)}:`)
      for (const annotation of list) {
        lines.push(`    - pixel_x: ${scalar(annotation.pixel_x)}`)
        lines.push(`      pixel_y: ${scalar(annotation.pixel_y)}`)
        lines.push(`      point: ${scalar(annotation.point)}`)
      }
    }
  }

  return lines.join('\n') + '\n'
}

/* ------------------------------------------------------------------ parse */

type YamlValue = string | number | boolean | null | YamlValue[] | { [key: string]: YamlValue }

interface Row {
  indent: number
  text: string
}

export function parse(text: string): YamlValue {
  const rows: Row[] = []
  for (const raw of text.split(/\r?\n/)) {
    const withoutComment = raw.replace(/\s+#.*$/, '')
    const trimmed = withoutComment.trim()
    if (!trimmed || trimmed.startsWith('#') || trimmed === '---') continue
    rows.push({ indent: withoutComment.match(/^ */)![0].length, text: trimmed })
  }

  let cursor = 0

  function readScalar(raw: string): YamlValue {
    if (raw === '' || raw === '~' || raw === 'null') return null
    if (raw === '[]') return []
    if (raw === '{}') return {}
    if (/^(true|yes)$/i.test(raw)) return true
    if (/^(false|no)$/i.test(raw)) return false
    if (/^'(.*)'$/.test(raw)) return raw.slice(1, -1).replace(/''/g, "'")
    if (/^"(.*)"$/.test(raw)) return raw.slice(1, -1).replace(/\\"/g, '"')
    if (!Number.isNaN(Number(raw))) return Number(raw)
    return raw
  }

  function splitKey(line: string): [string, string] {
    const match = line.match(/^((?:'[^']*'|"[^"]*"|[^:]+)):(?:\s+(.*))?$/)
    if (!match) throw new Error(`cannot parse line: ${line}`)
    return [String(readScalar(match[1]!.trim())), match[2] === undefined ? '' : match[2].trim()]
  }

  function parseBlock(indent: number): YamlValue {
    const row = rows[cursor]
    if (!row) return null
    return row.text.startsWith('- ') ? parseList(indent) : parseMap(indent)
  }

  function parseMap(indent: number): Record<string, YamlValue> {
    const map: Record<string, YamlValue> = {}
    while (cursor < rows.length) {
      const row = rows[cursor]!
      if (row.indent !== indent || row.text.startsWith('- ')) break

      const [key, inline] = splitKey(row.text)
      cursor++
      const next = rows[cursor]
      if (inline !== '') {
        map[key] = readScalar(inline)
      } else if (next && (next.indent > indent || (next.indent === indent && next.text.startsWith('- ')))) {
        map[key] = parseBlock(next.indent)
      } else {
        map[key] = null
      }
    }
    return map
  }

  function parseList(indent: number): YamlValue[] {
    const list: YamlValue[] = []
    while (cursor < rows.length) {
      const row = rows[cursor]!
      if (row.indent !== indent || !row.text.startsWith('- ')) break

      const inner = row.text.slice(2).trim()
      if (/^(?:'[^']*'|"[^"]*"|[^:]+):(?:\s|$)/.test(inner)) {
        // Re-indent the item's first line so it reads as the head of a map.
        rows[cursor] = { indent: indent + 2, text: inner }
        list.push(parseMap(indent + 2))
      } else {
        list.push(readScalar(inner))
        cursor++
      }
    }
    return list
  }

  return rows.length ? parseBlock(rows[0]!.indent) : {}
}

/* -------------------------------------------------------------- normalise */

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Coerce arbitrary parsed input into a well-formed document. */
export function normalize(value: unknown): AnnotationDocument {
  const doc = emptyDocument()
  if (!isRecord(value)) return doc

  const points = value.geographic_points
  if (isRecord(points)) {
    for (const [key, raw] of Object.entries(points)) {
      if (!isRecord(raw)) continue
      doc.geographic_points[key] = {
        name: raw.name == null ? key : String(raw.name),
        latitude: Number(raw.latitude),
        longitude: Number(raw.longitude),
      }
    }
  }

  const domains = value.domains
  if (isRecord(domains)) {
    for (const [domain, images] of Object.entries(domains)) {
      if (!isRecord(images)) continue
      doc.domains[domain] = {}
      for (const [image, list] of Object.entries(images)) {
        doc.domains[domain]![image] = (Array.isArray(list) ? list : [])
          .filter(isRecord)
          .map((entry) => ({
            pixel_x: Math.round(Number(entry.pixel_x)),
            pixel_y: Math.round(Number(entry.pixel_y)),
            point: entry.point == null ? '' : String(entry.point),
          }))
      }
    }
  }

  return doc
}

/** Parse YAML text straight into a document, throwing on malformed input. */
export function deserialize(text: string): AnnotationDocument {
  const parsed = parse(text)
  if (!isRecord(parsed)) throw new Error('expected a YAML mapping at the top level')
  if (!('geographic_points' in parsed) && !('domains' in parsed))
    throw new Error('no `geographic_points` or `domains` key found')
  return normalize(parsed)
}
