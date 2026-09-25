import { useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

export function AuthPage({ mode }: { mode: 'login' | 'register' }) {
  const { user, login, register } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (user) {
    const from = (location.state as { from?: string } | null)?.from
    return <Navigate to={from ?? '/runs'} replace />
  }

  const registering = mode === 'register'

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await (registering ? register(email, password) : login(email, password))
      navigate('/runs', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth">
      <div className="auth__panel">
        <div className="auth__brand">
          <span className="auth__mark">▚</span>
          <div>
            <h1>Sandcastle</h1>
            <p>Evaluation harness for AI coding agents</p>
          </div>
        </div>

        <form onSubmit={onSubmit} className="auth__form">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            required
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="you@example.com"
          />

          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            required
            minLength={registering ? 8 : undefined}
            autoComplete={registering ? 'new-password' : 'current-password'}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={registering ? 'at least 8 characters' : ''}
          />

          {error && <p className="form__error">{error}</p>}

          <button type="submit" className="btn btn--primary" disabled={busy}>
            {busy ? 'Working…' : registering ? 'Create account' : 'Sign in'}
          </button>
        </form>

        <p className="auth__switch">
          {registering ? (
            <>
              Already have an account? <Link to="/login">Sign in</Link>
            </>
          ) : (
            <>
              No account yet? <Link to="/register">Create one</Link>
            </>
          )}
        </p>
      </div>
    </div>
  )
}
