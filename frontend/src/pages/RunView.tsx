import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { DiffView } from '../components/DiffView'
import { StatusBadge } from '../components/StatusBadge'
import { StepList } from '../components/StepList'
import { useRunStream, type ConnectionState } from '../hooks/useRunStream'
import { isTerminal } from '../api/types'

const CONNECTION_LABEL: Record<ConnectionState, string> = {
  connecting: 'connecting…',
  live: 'streaming',
  reconnecting: 'reconnecting…',
  closed: 'stream closed',
  error: 'stream unavailable',
}

function elapsed(startedAt: string | null, completedAt: string | null, now: number): string {
  if (!startedAt) return '—'
  const start = new Date(startedAt).getTime()
  const end = completedAt ? new Date(completedAt).getTime() : now
  const seconds = Math.max(0, Math.round((end - start) / 1000))
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

export function RunView() {
  const { runId } = useParams<{ runId: string }>()
  const { run, steps, diffs, status, connection, error } = useRunStream(runId)

  // Drives the live duration counter while a run is in flight.
  const [now, setNow] = useState(() => Date.now())
  const running = status !== null && !isTerminal(status)

  useEffect(() => {
    if (!running) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [running])

  const [tab, setTab] = useState<'steps' | 'diff'>('steps')
  // Once a run finishes with changes, the diff is the interesting part.
  useEffect(() => {
    if (status && isTerminal(status) && diffs.length > 0) setTab('diff')
  }, [status, diffs.length])

  const toolCalls = useMemo(() => steps.filter((s) => s.type === 'action').length, [steps])

  if (error && !run) {
    return (
      <div className="page">
        <p className="form__error">{error}</p>
        <Link to="/runs" className="btn">
          Back to runs
        </Link>
      </div>
    )
  }

  if (!run || !status) {
    return (
      <div className="page">
        <p className="muted">Loading run…</p>
      </div>
    )
  }

  return (
    <div className="page runview">
      <header className="runview__head">
        <div className="runview__title">
          <StatusBadge status={status} live={connection === 'live'} />
          <h1>{run.task.description}</h1>
        </div>
        <a className="runview__repo" href={run.task.repo_url} target="_blank" rel="noreferrer">
          {run.task.repo_url.replace(/^https:\/\//, '')}
        </a>
      </header>

      <dl className="metrics">
        <div className="metric">
          <dt>Steps</dt>
          <dd>{steps.length}</dd>
        </div>
        <div className="metric">
          <dt>Tool calls</dt>
          <dd>{toolCalls}</dd>
        </div>
        <div className="metric">
          <dt>Duration</dt>
          <dd>{elapsed(run.started_at, run.completed_at, now)}</dd>
        </div>
        <div className="metric">
          <dt>Model</dt>
          <dd className="metric__mono">{run.agent_model}</dd>
        </div>
        <div className="metric">
          <dt>Step limit</dt>
          <dd>{run.max_steps}</dd>
        </div>
        <div className="metric">
          <dt>Files changed</dt>
          <dd>{diffs.length}</dd>
        </div>
      </dl>

      {run.error_message && (
        <div className="alert alert--error">
          <strong>Run failed.</strong>
          <p>{run.error_message}</p>
        </div>
      )}

      {connection === 'reconnecting' && (
        <div className="alert alert--warn">
          Lost the live connection — retrying. Nothing is lost; the stream replays
          on reconnect.
        </div>
      )}

      <div className="tabs">
        <button
          type="button"
          className={tab === 'steps' ? 'tab tab--active' : 'tab'}
          onClick={() => setTab('steps')}
        >
          Steps <span className="tab__count">{steps.length}</span>
        </button>
        <button
          type="button"
          className={tab === 'diff' ? 'tab tab--active' : 'tab'}
          onClick={() => setTab('diff')}
        >
          Diff <span className="tab__count">{diffs.length}</span>
        </button>
        <span className={`stream stream--${connection}`}>{CONNECTION_LABEL[connection]}</span>
      </div>

      {tab === 'steps' ? (
        <StepList steps={steps} autoScroll={running && connection === 'live'} />
      ) : (
        <DiffView diffs={diffs} />
      )}
    </div>
  )
}
