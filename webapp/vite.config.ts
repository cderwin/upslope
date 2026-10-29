import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    open: '/map',
    // Forecasts and the admin API come from a running upslope server, e.g. the local stack (`just up`).
    proxy: { '/fx': 'http://localhost:8000', '/admin': 'http://localhost:8000' },
  },
})
