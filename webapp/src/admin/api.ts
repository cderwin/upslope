import { adminToken, clearToken, requestToken } from './token'

/** An error response from the admin API. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly problems: string[] = [],
  ) {
    super(message)
  }
}

/** 401 or 403: the token is missing or wrong; the token prompt has been opened. */
export class AuthError extends ApiError {}

/** 412: the draft changed since the revision the request was based on. */
export class ConflictError extends ApiError {
  constructor(
    message: string,
    readonly currentRevision: number,
  ) {
    super(412, message)
  }
}

export interface RequestOptions {
  method?: string
  body?: unknown
  /** Sent as `If-Match`, so the change applies only to this draft revision. */
  revision?: number
}

async function send(path: string, { method = 'GET', body, revision }: RequestOptions): Promise<Response> {
  if (!adminToken.token) {
    requestToken('missing')
    throw new AuthError(401, 'Enter the admin token to continue.')
  }
  const headers: Record<string, string> = { Authorization: `Bearer ${adminToken.token}` }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (revision !== undefined) headers['If-Match'] = `"${revision}"`

  const response = await fetch(path, {
    method,
    headers,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  if (response.ok) return response

  const detail = (await response.json().catch(() => null)) as {
    detail?: string
    problems?: string[]
    current_revision?: number
  } | null
  const message = detail?.detail ?? `${response.status} ${response.statusText}`
  if (response.status === 401) {
    requestToken('missing')
    throw new AuthError(401, message)
  }
  if (response.status === 403) {
    clearToken()
    requestToken('rejected')
    throw new AuthError(403, message)
  }
  if (response.status === 412) throw new ConflictError(message, detail?.current_revision ?? -1)
  throw new ApiError(response.status, message, detail?.problems ?? [])
}

/** Call the admin API and parse its JSON response. */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return (await (await send(path, options)).json()) as T
}

/** Fetch a binary resource from the admin API. */
export async function requestBlob(path: string): Promise<Blob> {
  return (await send(path, {})).blob()
}
