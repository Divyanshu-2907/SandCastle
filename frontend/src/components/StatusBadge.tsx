import type { RunStatus } from '../api/types'

const LABELS: Record<RunStatus, string> = {
  queued: 'queued',
  running: 'running',
  completed: 'completed',
  failed: 'failed',
}

export function StatusBadge({ status, live }: { status: RunStatus; live?: boolean }) {
  return (
    <span className={`badge badge--${status}`}>
      {status === 'running' && <span className="badge__pulse" aria-hidden />}
      {LABELS[status]}
      {live && status === 'running' ? ' · live' : ''}
    </span>
  )
}
