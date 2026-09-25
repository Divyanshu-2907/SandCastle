import type { RunCreated, RunDetail, RunSummary, Task, Token, User } from './types'

export const API_BASE = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

/** ws:// for http://, wss:// for https://. */
export const WS_BASE = API_BASE.replace(/^http/, 'ws')

const TOKEN_KEY = 'sandcastle.token'

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    // Private browsing and some embedded webviews throw on access.
    return null
  }
}

export function setToken(token: string | null): void {
  try {
    if (token === null) localStorage.removeItem(TOKEN_KEY)
    else localStorage.setItem(TOKEN_KEY, token)
  } catch {
    /* non-fatal: the session just won't survive a reload */
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  get isAuthError(): boolean {
    return this.status === 401
  }
}

/** Pull something readable out of FastAPI's error shapes. */
async function describeError(response: Response): Promise<string> {
  let body: unknown
  try {
    body = await response.json()
  } catch {
    return response.statusText || `Request failed (${response.status})`
  }

  const detail = (body as { detail?: unknown })?.detail
  if (typeof detail === 'string') return detail

  // 422 validation errors arrive as a list of {loc, msg}.
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => {
        const field = Array.isArray(item?.loc) ? item.loc[item.loc.length - 1] : null
        return field ? `${field}: ${item.msg}` : item.msg
      })
      .filter(Boolean)
    if (messages.length) return messages.join('; ')
  }
  return response.statusText || `Request failed (${response.status})`
}

type OnAuthFailure = () => void
let onAuthFailure: OnAuthFailure = () => {}

/** Let the auth layer react to a token the server no longer accepts. */
export function setAuthFailureHandler(handler: OnAuthFailure): void {
  onAuthFailure = handler
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers })
  } catch {
    // Status 0 means the request never reached the server -- backend down, DNS,
    // CORS. Worth distinguishing from a real HTTP error in the UI.
    throw new ApiError(0, `Cannot reach the backend at ${API_BASE}`)
  }

  if (response.status === 401) {
    // Only a request we *authenticated* can have an expired session. A 401 on
    // login means bad credentials, and the server's message is the useful one.
    if (token) {
      onAuthFailure()
      throw new ApiError(401, 'Your session has expired. Please sign in again.')
    }
    throw new ApiError(401, await describeError(response))
  }
  if (!response.ok) {
    throw new ApiError(response.status, await describeError(response))
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const api = {
  register: (email: string, password: string) =>
    request<Token>('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  login: (email: string, password: string) =>
    request<Token>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  me: () => request<User>('/auth/me'),

  createTask: (repo_url: string, description: string) =>
    request<Task>('/tasks', {
      method: 'POST',
      body: JSON.stringify({ repo_url, description }),
    }),

  createRun: (taskId: string, options: { agent_model?: string; max_steps?: number } = {}) =>
    request<RunCreated>(`/tasks/${taskId}/runs`, {
      method: 'POST',
      body: JSON.stringify(options),
    }),

  listRuns: (status?: string) =>
    request<RunSummary[]>(`/runs${status ? `?status=${encodeURIComponent(status)}` : ''}`),

  getRun: (runId: string) => request<RunDetail>(`/runs/${runId}`),
}

export function runSocketUrl(runId: string): string {
  const token = getToken() ?? ''
  // Browsers cannot set headers on a WebSocket handshake, so the token rides in
  // the query string. The backend accepts a bearer header too, for CLI clients.
  return `${WS_BASE}/ws/runs/${runId}?token=${encodeURIComponent(token)}`
}
