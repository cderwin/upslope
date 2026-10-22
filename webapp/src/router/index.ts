import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import AnnotationView from '@/views/AnnotationView.vue'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'home',
    component: () => import('@/views/HomeView.vue'),
  },
  {
    // The annotation tool lives under /annotation; the optional domain/image
    // segments make a selected image a shareable link.
    path: '/annotation',
    name: 'annotation',
    component: AnnotationView,
  },
  {
    path: '/annotation/:domain/:image',
    name: 'annotation-image',
    component: AnnotationView,
    props: true,
  },
  {
    // Forecast overlays on a Mapbox map; model/variable (plus ?hour=&opacity=)
    // make the current view a shareable link.
    path: '/map',
    name: 'map',
    component: () => import('@/views/MapView.vue'),
  },
  {
    path: '/map/:model/:variable',
    name: 'map-layer',
    component: () => import('@/views/MapView.vue'),
    props: true,
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
