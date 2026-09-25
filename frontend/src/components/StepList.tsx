import { useEffect, useRef, useState } from 'react'
import type { Step } from '../api/types'

/** Inline JSON for tool input: one line when short, pretty-printed when not. */
function renderValue(value: unknown): string {
  const compact = JSON.stringify(value)
  if (compact !== undefined && compact.length <= 88) return compact
  return JSON.stringify(value, null, 2)
}

function ToolInput({ input }: { input: Record<string, unknown> }) {
  const entries = Object.entries(input)
  if (entries.length === 0) return null

  return (
    <dl className="step__args">
      {entries.map(([key, value]) => (
        <div key={key} className="step__arg">
          <dt>{key}</dt>
          <dd>
            <pre>{renderValue(value)}</pre>
          </dd>
        </div>
      ))}
    </dl>
  )
}

const COLLAPSE_OVER_LINES = 14

function Content({ text }: { text: string }) {
  const lines = text.split('\n')
  const long = lines.length > COLLAPSE_OVER_LINES
  const [expanded, setExpanded] = useState(false)

  const shown = long && !expanded ? lines.slice(0, COLLAPSE_OVER_LINES).join('\n') : text

  return (
    <div className="step__content">
      <pre>{shown}</pre>
      {long && (
        <button type="button" className="step__more" onClick={() => setExpanded((v) => !v)}>
          {expanded
            ? 'show less'
            : `show ${lines.length - COLLAPSE_OVER_LINES} more lines`}
        </button>
      )}
    </div>
  )
}

function StepRow({ step }: { step: Step }) {
  const exitCode = step.tool_output?.exit_code
  const failed =
    typeof exitCode === 'number' ? exitCode !== 0 : Boolean(step.tool_output?.error)

  return (
    <li className={`step step--${step.type}${failed ? ' step--failed' : ''}`}>
      <div className="step__gutter">
        <span className="step__number">{step.step_number}</span>
      </div>
      <div className="step__body">
        <div className="step__head">
          <span className="step__type">{step.type}</span>
          {step.tool_name && <span className="step__tool">{step.tool_name}</span>}
          {typeof exitCode === 'number' && (
            <span className={`step__exit${failed ? ' step__exit--bad' : ''}`}>
              exit {exitCode}
            </span>
          )}
          {step.tool_output?.timed_out === true && <span className="step__exit step__exit--bad">timed out</span>}
          <time className="step__time" dateTime={step.created_at}>
            {new Date(step.created_at).toLocaleTimeString()}
          </time>
        </div>

        {step.type === 'action' && step.tool_input && <ToolInput input={step.tool_input} />}
        {step.content && <Content text={step.content} />}
      </div>
    </li>
  )
}

export function StepList({ steps, autoScroll }: { steps: Step[]; autoScroll: boolean }) {
  const endRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (autoScroll) endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [steps.length, autoScroll])

  if (steps.length === 0) {
    return (
      <p className="empty">
        No steps yet. They appear here the moment the agent emits them.
      </p>
    )
  }

  return (
    <>
      <ol className="steps">
        {steps.map((step) => (
          <StepRow key={step.id || step.step_number} step={step} />
        ))}
      </ol>
      <div ref={endRef} />
    </>
  )
}
