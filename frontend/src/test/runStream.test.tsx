/**
 * The run stream is the part that can silently go wrong: a reconnect replays the
 * backlog, so the merge must be idempotent, and a run that ends must stop the
 * view waiting forever.
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useRunStream } from '../hooks/useRunStream'
import type { RunDetail, Step } from '../api/types'

function makeStep(n: number, overrides: Partial<Step> = {}): Step {
  return {
    id: `step-${n}`,
    step_number: n,
    type: 'action',
    content: null,
    tool_name: 'list_files',
    tool_input: { directory: '.' },
    tool_output: null,
    created_at: new Date().toISOString(),
    ...overrides,
  }
}

const RUN: RunDetail = {
  id: 'run-1',
  task_id: 'task-1',
  status: 'running',
  agent_model: 'gemini-2.5-flash',
  max_steps: 15,
  created_at: new Date().toISOString(),
  started_at: new Date().toISOString(),
  completed_at: null,
  error_message: null,
  stop_reason: null,
  task: {
    id: 'task-1',
    repo_url: 'https://github.com/octocat/Hello-World.git',
    description: 'Add a badge',
    created_at: new Date().toISOString(),
  },
  steps: [],
  diffs: [],
}

/** Minimal stand-in for the browser WebSocket, drivable from the test. */
class FakeSocket {
  static instances: FakeSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  closed = false

  constructor(public url: string) {
    FakeSocket.instances.push(this)
    // Mirror the browser: onopen fires after the handler is attached.
    queueMicrotask(() => this.onopen?.())
  }

  send() {}

  close() {
    this.closed = true
  }

  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }

  drop(code = 1006) {
    this.onclose?.({ code })
  }
}

let runPayload: RunDetail

beforeEach(() => {
  runPayload = { ...RUN, steps: [], diffs: [] }
  FakeSocket.instances = []
  vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket)
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => runPayload,
    })) as unknown as typeof fetch,
  )
  localStorage.setItem('sandcastle.token', 'test-token')
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('useRunStream', () => {
  it('loads the run over REST and then goes live', async () => {
    const { result } = renderHook(() => useRunStream('run-1'))

    await waitFor(() => expect(result.current.run).not.toBeNull())
    expect(result.current.status).toBe('running')
    await waitFor(() => expect(result.current.connection).toBe('live'))
  })

  it('appends live steps in step_number order', async () => {
    const { result } = renderHook(() => useRunStream('run-1'))
    await waitFor(() => expect(result.current.connection).toBe('live'))

    const socket = FakeSocket.instances[0]
    // Deliberately out of order: ordering must not depend on arrival.
    act(() => socket.emit({ event: 'step', data: makeStep(2) }))
    act(() => socket.emit({ event: 'step', data: makeStep(1) }))

    await waitFor(() => expect(result.current.steps).toHaveLength(2))
    expect(result.current.steps.map((s) => s.step_number)).toEqual([1, 2])
  })

  it('does not duplicate a step the backlog already delivered', async () => {
    runPayload = { ...RUN, steps: [makeStep(1), makeStep(2)], diffs: [] }
    const { result } = renderHook(() => useRunStream('run-1'))

    await waitFor(() => expect(result.current.steps).toHaveLength(2))
    const socket = FakeSocket.instances[0]

    // A reconnect replays the backlog; the merge must absorb it.
    act(() => socket.emit({ event: 'step', data: makeStep(1) }))
    act(() => socket.emit({ event: 'step', data: makeStep(2) }))
    act(() => socket.emit({ event: 'step', data: makeStep(3) }))

    await waitFor(() => expect(result.current.steps).toHaveLength(3))
    expect(result.current.steps.map((s) => s.step_number)).toEqual([1, 2, 3])
  })

  it('ignores keepalive frames', async () => {
    const { result } = renderHook(() => useRunStream('run-1'))
    await waitFor(() => expect(result.current.connection).toBe('live'))

    act(() => FakeSocket.instances[0].emit({ event: 'ping', data: {} }))
    expect(result.current.steps).toHaveLength(0)
    expect(result.current.status).toBe('running')
  })

  it('survives a malformed frame', async () => {
    const { result } = renderHook(() => useRunStream('run-1'))
    await waitFor(() => expect(result.current.connection).toBe('live'))

    act(() => FakeSocket.instances[0].onmessage?.({ data: '{not json' }))
    expect(result.current.connection).toBe('live')
  })

  it('refetches on a terminal status so diffs appear', async () => {
    const { result } = renderHook(() => useRunStream('run-1'))
    await waitFor(() => expect(result.current.connection).toBe('live'))

    runPayload = {
      ...RUN,
      status: 'completed',
      completed_at: new Date().toISOString(),
      diffs: [
        {
          id: 'd1',
          file_path: 'README.md',
          diff_text: '@@ -1 +1 @@\n-a\n+b\n',
          created_at: new Date().toISOString(),
        },
      ],
    }
    act(() => FakeSocket.instances[0].emit({ event: 'status', data: { status: 'completed' } }))

    await waitFor(() => expect(result.current.diffs).toHaveLength(1))
    expect(result.current.status).toBe('completed')
  })

  it('reconnects after an unexpected drop', async () => {
    const { result } = renderHook(() => useRunStream('run-1'))
    await waitFor(() => expect(result.current.connection).toBe('live'))

    act(() => FakeSocket.instances[0].drop(1006))
    expect(result.current.connection).toBe('reconnecting')

    await waitFor(() => expect(FakeSocket.instances.length).toBeGreaterThan(1), {
      timeout: 3000,
    })
    await waitFor(() => expect(result.current.connection).toBe('live'))
  })

  it('does not retry when the server says unauthorized', async () => {
    const { result } = renderHook(() => useRunStream('run-1'))
    await waitFor(() => expect(result.current.connection).toBe('live'))

    // 1008 is the backend's "not authorized for this run"; retrying cannot help.
    act(() => FakeSocket.instances[0].drop(1008))

    await waitFor(() => expect(result.current.connection).toBe('error'))
    expect(result.current.error).toMatch(/not authorized/i)
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it('puts the token in the socket url', async () => {
    renderHook(() => useRunStream('run-1'))
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1))
    expect(FakeSocket.instances[0].url).toContain('token=test-token')
    expect(FakeSocket.instances[0].url).toContain('/ws/runs/run-1')
  })
})
