import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { StatusBadge } from '../components/StatusBadge'
import type { RunStatus, RunSummary } from '../api/types'
import { isTerminal } from '../api/types'

const FILTERS: Array<{ label: string; value: RunStatus | 'all' }> = [
  { label: 'All', value: 'all' },
  { label: 'Running', value: 'running' },
  { label: 'Completed', value: 'completed' },
  { label: 'Failed', value: 'failed' },
]

function duration(run: RunSummary): string {
  if (!run.started_at) return '—'
  const end = run.completed_at ? new Date(run.completed_at).getTime() : Date.now()
  const seconds = Math.max(0, Math.round((end - new Date(run.started_at).getTime()) / 1000))
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

export function RunHistory() {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [filter, setFilter] = useState<RunStatus | 'all'>('all')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const data = await api.listRuns(filter === 'all' ? undefined : filter)
      setRuns(data)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [filter])

  useEffect(() => {
    setLoading(true)
    void load()
  }, [load])

  // Poll only while something is actually in flight; the run view itself uses
  // the WebSocket, so this list does not need to be push-driven.
  useEffect(() => {
    const active = runs.some((run) => !isTerminal(run.status))
    if (!active) return
    const id = window.setInterval(() => void load(), 4000)
    return () => window.clearInterval(id)
  }, [runs, load])

  return (
    <div className="page">
      <header className="page__head page__head--row">
        <div>
          <h1>Runs</h1>
          <p>Every agent run on your account, newest first.</p>
        </div>
        <Link to="/runs/new" className="btn btn--primary">
          New run
        </Link>
      </header>

      <div className="filters">
        {FILTERS.map((option) => (
          <button
            key={option.value}
            type="button"
            className={filter === option.value ? 'filter filter--active' : 'filter'}
            onClick={() => setFilter(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>

      {error && <p className="form__error">{error}</p>}

      {loading ? (
        <p className="muted">Loading…</p>
      ) : runs.length === 0 ? (
        <div className="empty empty--block">
          <p>No runs yet.</p>
          <Link to="/runs/new" className="btn btn--primary">
            Queue your first run
          </Link>
        </div>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Status</th>
              <th>Task</th>
              <th>Repository</th>
              <th>Model</th>
              <th className="table__num">Duration</th>
              <th>Started</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id}>
                <td>
                  <StatusBadge status={run.status} stopReason={run.stop_reason} />
                </td>
                <td className="table__main">
                  <Link to={`/runs/${run.id}`}>{run.task.description}</Link>
                </td>
                <td className="table__mono">
                  {run.task.repo_url.replace(/^https:\/\/(www\.)?/, '')}
                </td>
                <td className="table__mono">{run.agent_model}</td>
                <td className="table__num">{duration(run)}</td>
                <td className="table__mono">
                  {run.started_at ? new Date(run.started_at).toLocaleString() : 'not started'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
