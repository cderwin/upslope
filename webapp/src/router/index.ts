import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import AnnotationView from '@/views/AnnotationView.vue'

const routes: RouteRecordRaw[] = [
  {
    // The map is the app; there's no separate landing page.
    path: '/',
    name: 'home',
    redirect: { name: 'map' },
  },
  {
    // The annotation tool lives under /annotation; the optional maptype/image
    // segments make a selected image a shareable link.
    path: '/annotation',
    name: 'annotation',
    component: AnnotationView,
  },
  {
    path: '/annotation/:maptype/:image',
    name: 'annotation-image',
    component: AnnotationView,
    props: true,
  },
  {
    // Forecast overlays on a Mapbox map; model/layer (plus ?hour=&opacity=&terrain=,
    // and ?run=2026-10-29T00Z for a run other than the latest) make the view a shareable link.
    path: '/map',
    name: 'map',
    component: () => import('@/views/MapView.vue'),
  },
  {
    path: '/map/:model/:layer',
    name: 'map-layer',
    component: () => import('@/views/MapView.vue'),
    props: true,
  },
  {
    path: '/about',
    name: 'about',
    component: () => import('@/views/AboutView.vue'),
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
  },
]

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

export default router
