import { useCallback, useEffect, useRef, useState } from 'react'
import { api, runSocketUrl } from '../api/client'
import type { Diff, RunDetail, RunEvent, RunStatus, Step } from '../api/types'
import { isTerminal } from '../api/types'

export type ConnectionState = 'connecting' | 'live' | 'reconnecting' | 'closed' | 'error'

interface RunStream {
  run: RunDetail | null
  steps: Step[]
  diffs: Diff[]
  status: RunStatus | null
  connection: ConnectionState
  error: string | null
  reload: () => void
}

/** Backoff for reconnects, capped so a long outage doesn't stall forever. */
const RETRY_DELAYS_MS = [500, 1000, 2000, 4000, 8000]

/**
 * Streams one run.
 *
 * The REST fetch gives the full record (task, diffs, metadata); the socket
 * supplies live steps on top. Steps are merged by `step_number` so a reconnect
 * replaying the backlog cannot produce duplicates.
 */
export function useRunStream(runId: string | undefined): RunStream {
  const [run, setRun] = useState<RunDetail | null>(null)
  const [steps, setSteps] = useState<Step[]>([])
  const [diffs, setDiffs] = useState<Diff[]>([])
  const [status, setStatus] = useState<RunStatus | null>(null)
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [error, setError] = useState<string | null>(null)

  const socketRef = useRef<WebSocket | null>(null)
  const retryRef = useRef(0)
  const timerRef = useRef<number | null>(null)
  const closedByUs = useRef(false)

  const mergeStep = useCallback((incoming: Step) => {
    setSteps((current) => {
      if (current.some((s) => s.step_number === incoming.step_number)) return current
      return [...current, incoming].sort((a, b) => a.step_number - b.step_number)
    })
  }, [])

  const loadRun = useCallback(async () => {
    if (!runId) return
    try {
      const detail = await api.getRun(runId)
      setRun(detail)
      setStatus(detail.status)
      setDiffs(detail.diffs)
      detail.steps.forEach(mergeStep)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setConnection('error')
    }
  }, [runId, mergeStep])

  // Initial load, and a reset whenever the run being watched changes.
  useEffect(() => {
    setRun(null)
    setSteps([])
    setDiffs([])
    setStatus(null)
    setError(null)
    setConnection('connecting')
    retryRef.current = 0
    void loadRun()
  }, [runId, loadRun])

  useEffect(() => {
    if (!runId) return
    closedByUs.current = false

    const connect = () => {
      const socket = new WebSocket(runSocketUrl(runId))
      socketRef.current = socket

      socket.onopen = () => {
        retryRef.current = 0
        setConnection('live')
      }

      socket.onmessage = (event) => {
        let message: RunEvent
        try {
          message = JSON.parse(event.data as string)
        } catch {
          return // a malformed frame is not worth tearing the view down for
        }

        if (message.event === 'step') {
          mergeStep(message.data)
        } else if (message.event === 'status') {
          setStatus(message.data.status)
          if (isTerminal(message.data.status)) {
            // Diffs and final metadata are only written once the run ends, and
            // they come over REST rather than the stream.
            closedByUs.current = true
            void loadRun()
          }
        }
        // 'ping' is a keepalive; nothing to do.
      }

      socket.onerror = () => {
        // onclose always follows, which is where reconnect is handled.
      }

      socket.onclose = (event) => {
        socketRef.current = null
        if (closedByUs.current) {
          setConnection('closed')
          return
        }
        // 1008 is our "not authorized" close: retrying cannot help.
        if (event.code === 1008) {
          setError('Not authorized to watch this run.')
          setConnection('error')
          return
        }

        const attempt = retryRef.current
        if (attempt >= RETRY_DELAYS_MS.length) {
          setConnection('closed')
          // Fall back to a plain refetch so the view is at least accurate.
          void loadRun()
          return
        }
        retryRef.current = attempt + 1
        setConnection('reconnecting')
        timerRef.current = window.setTimeout(connect, RETRY_DELAYS_MS[attempt])
      }
    }

    connect()

    return () => {
      closedByUs.current = true
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [runId, mergeStep, loadRun])

  return { run, steps, diffs, status, connection, error, reload: loadRun }
}
