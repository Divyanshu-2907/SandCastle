/**
 * Route protection and session handling. The failure that matters here is
 * failing *open* -- showing protected data to someone without a valid token.
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import { AuthProvider } from '../auth/AuthContext'

const USER = { id: 'u1', email: 'owner@example.com', created_at: new Date().toISOString() }

interface Handler {
  status?: number
  body: unknown
}

let routes: Record<string, Handler>

function mockFetch() {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const match = Object.keys(routes).find((path) => url.includes(path))
    const handler = match ? routes[match] : { status: 404, body: { detail: 'Not Found' } }
    const status = handler.status ?? 200
    return {
      ok: status < 400,
      status,
      statusText: '',
      json: async () => handler.body,
    }
  }) as unknown as typeof fetch
}

function renderApp(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  routes = {
    '/auth/me': { body: USER },
    '/auth/login': { body: { access_token: 'tok', token_type: 'bearer', expires_in: 3600, user: USER } },
    '/auth/register': { body: { access_token: 'tok', token_type: 'bearer', expires_in: 3600, user: USER } },
    '/runs': { body: [] },
  }
  vi.stubGlobal('fetch', mockFetch())
  vi.stubGlobal('WebSocket', class {} as unknown as typeof WebSocket)
  localStorage.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('route protection', () => {
  it('sends an anonymous visitor to the login page', async () => {
    renderApp('/runs')
    expect(await screen.findByRole('button', { name: /sign in/i })).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('lets a visitor with a valid token through', async () => {
    localStorage.setItem('sandcastle.token', 'tok')
    renderApp('/runs')
    expect(await screen.findByRole('heading', { name: 'Runs' })).toBeInTheDocument()
  })

  it('rejects a stored token the server no longer accepts', async () => {
    // The token exists locally but /auth/me 401s: it must not grant access.
    localStorage.setItem('sandcastle.token', 'stale')
    routes['/auth/me'] = { status: 401, body: { detail: 'Not authenticated' } }

    renderApp('/runs')
    expect(await screen.findByRole('button', { name: /sign in/i })).toBeInTheDocument()
    await waitFor(() => expect(localStorage.getItem('sandcastle.token')).toBeNull())
  })

  it('does not flash the login page while the token is being verified', async () => {
    localStorage.setItem('sandcastle.token', 'tok')
    let release: (value: unknown) => void = () => {}
    const pending = new Promise((resolve) => {
      release = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        await pending
        return { ok: true, status: 200, json: async () => USER }
      }) as unknown as typeof fetch,
    )

    renderApp('/runs')
    // Mid-verification: neither the protected page nor the login form.
    expect(screen.queryByRole('button', { name: /sign in/i })).not.toBeInTheDocument()

    release(null)
  })
})

describe('sign in', () => {
  it('stores the token and lands on the run list', async () => {
    const user = userEvent.setup()
    renderApp('/login')

    await user.type(await screen.findByLabelText(/email/i), 'owner@example.com')
    await user.type(screen.getByLabelText(/password/i), 'hunter2hunter2')
    await user.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByRole('heading', { name: 'Runs' })).toBeInTheDocument()
    expect(localStorage.getItem('sandcastle.token')).toBe('tok')
  })

  it('surfaces the server error instead of silently failing', async () => {
    routes['/auth/login'] = { status: 401, body: { detail: 'Incorrect email or password' } }
    const user = userEvent.setup()
    renderApp('/login')

    await user.type(await screen.findByLabelText(/email/i), 'owner@example.com')
    await user.type(screen.getByLabelText(/password/i), 'wrong')
    await user.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByText(/incorrect email or password/i)).toBeInTheDocument()
    expect(localStorage.getItem('sandcastle.token')).toBeNull()
  })

  it('renders a 422 validation error readably', async () => {
    routes['/auth/register'] = {
      status: 422,
      body: { detail: [{ loc: ['body', 'password'], msg: 'String should have at least 8 characters' }] },
    }
    const user = userEvent.setup()
    renderApp('/register')

    await user.type(await screen.findByLabelText(/email/i), 'new@example.com')
    await user.type(screen.getByLabelText(/password/i), 'shortpw')
    await user.click(screen.getByRole('button', { name: /create account/i }))

    expect(await screen.findByText(/password: String should have at least 8/i)).toBeInTheDocument()
  })
})

describe('sign out', () => {
  it('clears the token and returns to the login page', async () => {
    localStorage.setItem('sandcastle.token', 'tok')
    const user = userEvent.setup()
    renderApp('/runs')

    await screen.findByRole('heading', { name: 'Runs' })
    await user.click(screen.getByRole('button', { name: /sign out/i }))

    expect(await screen.findByRole('button', { name: /sign in/i })).toBeInTheDocument()
    expect(localStorage.getItem('sandcastle.token')).toBeNull()
  })
})
