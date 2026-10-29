/// <reference types="vite/client" />

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

interface ImportMetaEnv {
  /** Mapbox access token for /map; set it in `.env.local`. */
  readonly VITE_MAPBOX_TOKEN?: string
  /** The upslope server's forecast API (`/status`, `/data/...`). Defaults to `/fx`. */
  readonly VITE_FORECAST_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
