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
    // The admin API comes from a running upslope server, e.g. the local stack (`just up`).
    proxy: { '/admin': 'http://localhost:8000' },
  },
})
