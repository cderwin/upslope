<script setup lang="ts">
/**
 * Upslope's mark: a glaciated volcano under a stacked lenticular cap cloud, on a navy tile. The same shapes as the
 * favicon (public/favicon.svg), colored from the --up-mark-* tokens. Decorative: put the name beside it.
 */
withDefaults(defineProps<{ size?: number }>(), { size: 28 })
</script>

<template>
  <svg class="upslope-mark" viewBox="0 0 64 64" :width="size" :height="size" aria-hidden="true" focusable="false">
    <rect class="bg" width="64" height="64" />
    <path
      class="rock"
      d="M0 58 L4 56 C12 52 20 42 26 35 Q32 31 38 35 C44 42 52 52 60 56 L64 58 L64 64 L0 64 Z"
    />
    <path
      class="snow"
      d="M20 42 C22 40 24 37 26 35 Q32 31 38 35 C40 37 42 40 44 42 L41 40.5 L38.5 43 L35.5 40.5 L32.5 43.5
        L29.5 40.5 L26.5 43 L23.5 40.5 Z"
    />
    <path class="snow" d="M12 30 C18 25.5 46 25.5 52 30 C44 32 20 32 12 30 Z" />
    <path class="shade" d="M12 30 C20 29.8 44 29.8 52 30 C44 32 20 32 12 30 Z" />
    <path class="snow" d="M16 24.5 C22 20 42 20 48 24.5 C41 26 23 26 16 24.5 Z" />
    <path class="shade" d="M16 24.5 C23 24.3 41 24.3 48 24.5 C41 26 23 26 16 24.5 Z" />
    <path class="snow" d="M20 19.5 C25 15.5 39 15.5 44 19.5 C38 20.8 26 20.8 20 19.5 Z" />
    <path class="shade" d="M20 19.5 C26 19.3 38 19.3 44 19.5 C38 20.8 26 20.8 20 19.5 Z" />
    <path class="snow" d="M25 14.8 C29 11.8 35 11.8 39 14.8 C35 15.8 29 15.8 25 14.8 Z" />
    <path class="shade" d="M25 14.8 C29 14.6 35 14.6 39 14.8 C35 15.8 29 15.8 25 14.8 Z" />
  </svg>
</template>

<style scoped>
.upslope-mark {
  display: block;
  flex: none;
  /* The favicon's rounded tile (rx 14 of 64); a clip, so the mountain's base is cut off at the corners. */
  clip-path: inset(0 round 22%);
}

.bg {
  fill: var(--up-mark-bg);
}

.rock {
  fill: var(--up-mark-rock);
}

.snow {
  fill: var(--up-mark-snow);
}

.shade {
  fill: var(--up-mark-shade);
}
</style>
