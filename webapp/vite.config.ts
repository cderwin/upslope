import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { dataMounts } from './plugins/dataMounts'

const repoPath = (relative: string) => fileURLToPath(new URL(`../${relative}`, import.meta.url))

export default defineConfig({
  plugins: [
    vue(),
    dataMounts([
      {
        // Georegistration GIFs for /annotation: listed at build time and copied into dist/.
        prefix: '/data/georegistration/images',
        dir: repoPath('data/georegistration/images'),
        extensions: ['.gif', '.png', '.jpg', '.jpeg'],
        copyOnBuild: true,
        manifestModule: 'virtual:georegistration-manifest',
      },
      {
        // `upslope export-web` output for /map. Served from disk only, never bundled:
        // it stands in for the forecast API (see VITE_FORECAST_BASE_URL).
        prefix: '/api/forecast',
        dir: repoPath('forecast/web_export'),
        extensions: ['.png', '.json'],
      },
    ]),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    open: '/map',
  },
})
