import { Navigate, NavLink, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { useAuth } from './auth/AuthContext'
import { AuthPage } from './pages/Login'
import { NewRun } from './pages/NewRun'
import { RunHistory } from './pages/RunHistory'
import { RunView } from './pages/RunView'

function RequireAuth() {
  const { user, loading } = useAuth()
  const location = useLocation()

  // Wait for the stored token to be verified, or a refresh bounces the user to
  // the login screen before the check finishes.
  if (loading) {
    return (
      <div className="boot">
        <span className="boot__mark">▚</span>
      </div>
    )
  }
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <Shell />
}

function Shell() {
  const { user, logout } = useAuth()

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="sidebar__brand">
          <span className="sidebar__mark">▚</span>
          <span className="sidebar__name">Sandcastle</span>
        </div>

        <nav className="sidebar__nav">
          <NavLink to="/runs/new" className={({ isActive }) => (isActive ? 'navlink navlink--active' : 'navlink')}>
            New run
          </NavLink>
          <NavLink
            to="/runs"
            end
            className={({ isActive }) => (isActive ? 'navlink navlink--active' : 'navlink')}
          >
            Runs
          </NavLink>
        </nav>

        <div className="sidebar__foot">
          <span className="sidebar__user" title={user?.email}>
            {user?.email}
          </span>
          <button type="button" className="btn btn--ghost" onClick={logout}>
            Sign out
          </button>
        </div>
      </aside>

      <main className="main">
        <Outlet />
      </main>
    </div>
  )
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<AuthPage mode="login" />} />
      <Route path="/register" element={<AuthPage mode="register" />} />

      <Route element={<RequireAuth />}>
        <Route path="/runs" element={<RunHistory />} />
        <Route path="/runs/new" element={<NewRun />} />
        <Route path="/runs/:runId" element={<RunView />} />
      </Route>

      <Route path="*" element={<Navigate to="/runs" replace />} />
    </Routes>
  )
}
