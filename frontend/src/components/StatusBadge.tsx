import type { RunStatus } from '../api/types'

const LABELS: Record<RunStatus, string> = {
  queued: 'queued',
  running: 'running',
  completed: 'completed',
  failed: 'failed',
}

/**
 * How a run ended, in the reader's words rather than the loop's.
 *
 * Only shown where it adds something the status does not already say: a run
 * that finished of its own accord is just "completed", but one that merely ran
 * out of turns is also "completed" and that is misleading on its own.
 */
const STOP_REASONS: Record<string, string> = {
  max_steps: 'step limit',
  sandbox_timeout: 'sandbox timeout',
  task_timeout: 'task timeout',
  malformed_tool_call: 'bad tool call',
  refusal: 'declined',
}

export function StatusBadge({
  status,
  stopReason,
  live,
}: {
  status: RunStatus
  stopReason?: string | null
  live?: boolean
}) {
  const detail = stopReason ? STOP_REASONS[stopReason] : undefined
  return (
    <span className={`badge badge--${status}`}>
      {status === 'running' && <span className="badge__pulse" aria-hidden />}
      {LABELS[status]}
      {live && status === 'running' ? ' · live' : ''}
      {detail ? ` · ${detail}` : ''}
    </span>
  )
}
