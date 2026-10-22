import { reactive, readonly } from 'vue'

/**
 * The admin API's bearer token, kept for this browser tab only (sessionStorage).
 *
 * When a request is refused, `request()` opens the token prompt with the reason;
 * `TokenDialog` reads this state and calls `setToken` with what the user enters.
 */

const STORAGE_KEY = 'upslope-admin-token'

export type PromptReason = 'missing' | 'rejected'

function readStored(): string {
  try {
    return sessionStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

const state = reactive({
  token: readStored(),
  prompt: null as PromptReason | null,
  /** Increments whenever a new token is saved, so pages can reload. */
  version: 0,
})

export const adminToken = readonly(state)

export function setToken(token: string): void {
  state.token = token.trim()
  state.prompt = null
  state.version++
  try {
    sessionStorage.setItem(STORAGE_KEY, state.token)
  } catch {
    // Not persisted; the token still works for this page.
  }
}

export function clearToken(): void {
  state.token = ''
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing stored.
  }
}

export function requestToken(reason: PromptReason): void {
  state.prompt = reason
}

export function dismissPrompt(): void {
  state.prompt = null
}
