import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { api, getToken, setAuthFailureHandler, setToken } from '../api/client'
import type { User } from '../api/types'

interface AuthState {
  user: User | null
  /** True until the stored token has been checked against the server. */
  loading: boolean
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
  }, [])

  // A token the server rejects mid-session should drop us to the login screen
  // rather than leave the UI showing stale data.
  useEffect(() => {
    setAuthFailureHandler(() => {
      setToken(null)
      setUser(null)
    })
  }, [])

  // A token in localStorage is a claim, not proof: verify it before trusting it.
  useEffect(() => {
    let cancelled = false

    if (!getToken()) {
      setLoading(false)
      return
    }
    api
      .me()
      .then((me) => !cancelled && setUser(me))
      .catch(() => !cancelled && setToken(null))
      .finally(() => !cancelled && setLoading(false))

    return () => {
      cancelled = true
    }
  }, [])

  const login = useCallback(async (email: string, password: string) => {
    const token = await api.login(email, password)
    setToken(token.access_token)
    setUser(token.user)
  }, [])

  const register = useCallback(async (email: string, password: string) => {
    const token = await api.register(email, password)
    setToken(token.access_token)
    setUser(token.user)
  }, [])

  const value = useMemo(
    () => ({ user, loading, login, register, logout }),
    [user, loading, login, register, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (context === null) throw new Error('useAuth must be used inside an AuthProvider')
  return context
}
