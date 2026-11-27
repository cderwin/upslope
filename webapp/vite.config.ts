import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// The upslope server the dev server proxies to: the local stack by default. VITE_PROXY_TARGET points it elsewhere,
// e.g. https://upslope.fly.dev to see production's forecasts (the server has no CORS, so the browser can't call it).
const proxyTarget = process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000'
const proxy = { target: proxyTarget, changeOrigin: true }

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  css: {
    preprocessorOptions: {
      // Bootstrap 5.3's Sass still uses @import and the global color functions; quiet what we can't change.
      scss: { quietDeps: true, silenceDeprecations: ['import', 'global-builtin', 'color-functions'] },
    },
  },
  server: {
    port: 5173,
    open: '/map',
    // Forecasts and the admin API come from a running upslope server, e.g. the local stack (`just up`).
    proxy: { '/fx': proxy, '/admin': proxy },
  },
})
