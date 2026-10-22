import fs from 'node:fs'
import type { IncomingMessage, ServerResponse } from 'node:http'
import path from 'node:path'
import type { Connect, Plugin, ViteDevServer } from 'vite'

/**
 * The webapp's data lives outside the package (in the repo's `data/` and
 * `forecast/` trees), so it is neither in `public/` nor importable as modules.
 *
 * This plugin mounts such directories under URL prefixes:
 *
 *  - every mount is served by `vite dev` and `vite preview` (with a
 *    path-traversal guard), so data can stay out of the bundle entirely;
 *  - `copyOnBuild` mounts are also copied into the build output;
 *  - `manifestModule` mounts expose their `{ dir: [file, ...] }` listing as a
 *    virtual module, so dropdowns never have to be hand-maintained. The dev
 *    server reloads when files are added or removed.
 */

const MIME_TYPES: Record<string, string> = {
  '.gif': 'image/gif',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.json': 'application/json',
}

export interface DataMount {
  /** URL prefix the files are served under, e.g. `/api/forecast`. */
  prefix: string
  /** Absolute path of the directory to serve. */
  dir: string
  /** File extensions (lowercase, with the dot) to serve, copy and list. */
  extensions: string[]
  /** Copy the tree into the build output. Defaults to `false`. */
  copyOnBuild?: boolean
  /** Virtual module id exporting `{ subdirectory: [file, ...] }` for files one level down. */
  manifestModule?: string
}

interface ResolvedMount extends Required<Omit<DataMount, 'manifestModule'>> {
  extensionSet: Set<string>
  manifestModule?: string
}

function hasExtension(mount: ResolvedMount, file: string): boolean {
  return mount.extensionSet.has(path.extname(file).toLowerCase())
}

/** `{ subdirectory: [file, ...] }` for every matching file directly under `<dir>/<subdirectory>`. */
export function readManifest(dir: string, extensions: Iterable<string>): Record<string, string[]> {
  const extensionSet = new Set(extensions)
  const manifest: Record<string, string[]> = {}
  if (!fs.existsSync(dir)) return manifest

  for (const group of fs.readdirSync(dir).sort()) {
    if (group.startsWith('.')) continue
    const groupDir = path.join(dir, group)
    if (!fs.statSync(groupDir).isDirectory()) continue

    const files = fs
      .readdirSync(groupDir)
      .filter((name) => !name.startsWith('.'))
      .filter((name) => extensionSet.has(path.extname(name).toLowerCase()))
      .filter((name) => fs.statSync(path.join(groupDir, name)).isFile())
      .sort()

    if (files.length) manifest[group] = files
  }
  return manifest
}

function copyTree(mount: ResolvedMount, from: string, to: string): void {
  if (!fs.existsSync(from)) return
  fs.mkdirSync(to, { recursive: true })
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    if (entry.name.startsWith('.')) continue
    const src = path.join(from, entry.name)
    const dest = path.join(to, entry.name)
    if (entry.isDirectory()) copyTree(mount, src, dest)
    else if (hasExtension(mount, entry.name)) fs.copyFileSync(src, dest)
  }
}

function serveMounts(mounts: ResolvedMount[]): Connect.NextHandleFunction {
  return (req: IncomingMessage, res: ServerResponse, next: Connect.NextFunction) => {
    const url = (req.url ?? '').split('?')[0]
    const mount = mounts.find((m) => url.startsWith(m.prefix + '/'))
    if (!mount) return next()

    let relative: string
    try {
      relative = decodeURIComponent(url.slice(mount.prefix.length + 1))
    } catch {
      res.statusCode = 400
      return res.end('Bad request')
    }

    const file = path.resolve(mount.dir, relative)
    if (!file.startsWith(mount.dir + path.sep)) {
      res.statusCode = 403
      return res.end('Forbidden')
    }
    if (!hasExtension(mount, file) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      res.statusCode = 404
      return res.end('Not found')
    }

    res.setHeader('Content-Type', MIME_TYPES[path.extname(file).toLowerCase()] ?? 'application/octet-stream')
    res.setHeader('Cache-Control', 'no-cache')
    fs.createReadStream(file).pipe(res)
  }
}

export function dataMounts(mounts: DataMount[]): Plugin {
  const resolved: ResolvedMount[] = mounts.map((mount) => ({
    ...mount,
    prefix: mount.prefix.replace(/\/+$/, ''),
    dir: path.resolve(mount.dir),
    copyOnBuild: mount.copyOnBuild ?? false,
    extensionSet: new Set(mount.extensions.map((ext) => ext.toLowerCase())),
  }))
  const byModuleId = new Map(
    resolved.filter((m) => m.manifestModule).map((m) => ['\0' + m.manifestModule, m] as const),
  )
  let outDir = 'dist'

  return {
    name: 'upslope-data-mounts',

    configResolved(config) {
      outDir = path.resolve(config.root, config.build.outDir)
    },

    resolveId(id) {
      return byModuleId.has('\0' + id) ? '\0' + id : null
    },

    load(id) {
      const mount = byModuleId.get(id)
      if (!mount) return null
      return `export default ${JSON.stringify(readManifest(mount.dir, mount.extensionSet), null, 2)}\n`
    },

    configureServer(server: ViteDevServer) {
      // Reload a mount's manifest when its files are added or removed.
      for (const [moduleId, mount] of byModuleId) {
        server.watcher.add(mount.dir)
        const invalidate = (file: string) => {
          if (!file.startsWith(mount.dir + path.sep) || !hasExtension(mount, file)) return
          const mod = server.moduleGraph.getModuleById(moduleId)
          if (mod) server.moduleGraph.invalidateModule(mod)
          server.ws.send({ type: 'full-reload' })
        }
        server.watcher.on('add', invalidate)
        server.watcher.on('unlink', invalidate)
      }

      server.middlewares.use(serveMounts(resolved))
    },

    configurePreviewServer(server) {
      server.middlewares.use(serveMounts(resolved))
    },

    closeBundle() {
      for (const mount of resolved.filter((m) => m.copyOnBuild)) {
        copyTree(mount, mount.dir, path.join(outDir, mount.prefix.replace(/^\//, '')))
      }
    },
  }
}
