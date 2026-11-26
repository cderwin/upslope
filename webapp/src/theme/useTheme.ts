import { readonly, ref } from 'vue'

/**
 * The color theme: `data-bs-theme` on <html>, which Bootstrap and the design tokens (src/styles/tokens.css) restyle
 * from. The viewer's choice is kept in localStorage; until they choose, the OS preference applies (and is followed
 * when it changes), defaulting to dark.
 */

export type Theme = 'dark' | 'light'

const STORAGE_KEY = 'upslopeTheme'

function stored(): Theme | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    return value === 'dark' || value === 'light' ? value : null
  } catch {
    return null // blocked storage: the OS preference applies
  }
}

const prefersLight = typeof window === 'undefined' ? null : window.matchMedia('(prefers-color-scheme: light)')

function system(): Theme {
  return prefersLight?.matches ? 'light' : 'dark'
}

const theme = ref<Theme>(stored() ?? system())

function apply(value: Theme) {
  theme.value = value
  document.documentElement.dataset.bsTheme = value
}

/** Set the theme before the app mounts, so the first paint is already in it. */
export function initTheme(): void {
  apply(theme.value)
  prefersLight?.addEventListener('change', () => {
    if (!stored()) apply(system())
  })
}

function setTheme(value: Theme): void {
  try {
    localStorage.setItem(STORAGE_KEY, value)
  } catch {
    // Not persisted; it still applies to this page.
  }
  apply(value)
}

/** The current theme (reactive), and ways to change it. Charts watch `theme` to re-read their colors. */
export function useTheme() {
  return {
    theme: readonly(theme),
    setTheme,
    toggle: () => setTheme(theme.value === 'dark' ? 'light' : 'dark'),
  }
}
