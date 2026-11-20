export type RunStatus = 'queued' | 'running' | 'completed' | 'failed'
export type StepType = 'thought' | 'action' | 'observation'

export interface User {
  id: string
  email: string
  created_at: string
}

export interface Token {
  access_token: string
  token_type: string
  expires_in: number
  user: User
}

export interface Task {
  id: string
  repo_url: string
  description: string
  created_at: string
}

export interface Step {
  id: string
  step_number: number
  type: StepType
  content: string | null
  tool_name: string | null
  tool_input: Record<string, unknown> | null
  tool_output: Record<string, unknown> | null
  created_at: string
}

export interface Diff {
  id: string
  file_path: string
  diff_text: string
  created_at: string
}

export interface RunSummary {
  id: string
  task_id: string
  status: RunStatus
  agent_model: string
  max_steps: number
  created_at: string
  started_at: string | null
  completed_at: string | null
  error_message: string | null
  stop_reason: string | null
  task: Task
}

export interface RunDetail extends RunSummary {
  steps: Step[]
  diffs: Diff[]
}

export interface RunCreated {
  id: string
  task_id: string
  status: RunStatus
}

/** Frames pushed over /ws/runs/{id}. `ping` is a keepalive and can be ignored. */
export type RunEvent =
  | { event: 'step'; data: Step }
  | {
      event: 'status'
      data: { status: RunStatus; error_message?: string | null; stop_reason?: string | null }
    }
  | { event: 'ping'; data: Record<string, never> }

export const TERMINAL_STATUSES: RunStatus[] = ['completed', 'failed']

export function isTerminal(status: RunStatus): boolean {
  return TERMINAL_STATUSES.includes(status)
}
