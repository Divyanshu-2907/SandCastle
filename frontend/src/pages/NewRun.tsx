import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api/client'

const EXAMPLES = [
  {
    repo: 'https://github.com/octocat/Hello-World.git',
    description: 'Add a build-status badge to the top of the README.',
  },
  {
    repo: 'https://github.com/octocat/Hello-World.git',
    description: 'Add a Usage section to the README explaining how to run the program.',
  },
]

export function NewRun() {
  const navigate = useNavigate()

  const [repoUrl, setRepoUrl] = useState('')
  const [description, setDescription] = useState('')
  const [model, setModel] = useState('')
  const [maxSteps, setMaxSteps] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      // A task is the reusable definition; a run is one attempt at it. Creating
      // both here keeps the common case to a single click.
      const task = await api.createTask(repoUrl.trim(), description.trim())
      const run = await api.createRun(task.id, {
        ...(model.trim() ? { agent_model: model.trim() } : {}),
        ...(maxSteps.trim() ? { max_steps: Number(maxSteps) } : {}),
      })
      navigate(`/runs/${run.id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  return (
    <div className="page">
      <header className="page__head">
        <h1>New run</h1>
        <p>Point the agent at a repository and describe what it should change.</p>
      </header>

      <div className="newrun">
        <form onSubmit={onSubmit} className="panel newrun__form">
          <div className="field">
            <label htmlFor="repo">Repository URL</label>
            <input
              id="repo"
              required
              value={repoUrl}
              onChange={(e) => setRepoUrl(e.target.value)}
              placeholder="https://github.com/owner/repo.git"
              spellCheck={false}
            />
            <p className="field__hint">
              Public https, ssh, or <code>git@host:path</code>. Cloned fresh for every run.
            </p>
          </div>

          <div className="field">
            <label htmlFor="description">Task</label>
            <textarea
              id="description"
              required
              rows={6}
              maxLength={4000}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Add a Usage section to the README explaining how to run the program."
            />
            <p className="field__hint">{description.length} / 4000</p>
          </div>

          <details className="newrun__advanced">
            <summary>Advanced</summary>
            <div className="newrun__grid">
              <div className="field">
                <label htmlFor="model">Model</label>
                <input
                  id="model"
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                  placeholder="server default"
                  spellCheck={false}
                />
                <p className="field__hint">
                  Free-tier quota is per model — switching gives a fresh allowance.
                </p>
              </div>
              <div className="field">
                <label htmlFor="steps">Max steps</label>
                <input
                  id="steps"
                  type="number"
                  min={1}
                  max={50}
                  value={maxSteps}
                  onChange={(e) => setMaxSteps(e.target.value)}
                  placeholder="15"
                />
                <p className="field__hint">Model turns, not tool calls.</p>
              </div>
            </div>
          </details>

          {error && <p className="form__error">{error}</p>}

          <button type="submit" className="btn btn--primary" disabled={busy}>
            {busy ? 'Queueing…' : 'Queue run'}
          </button>
        </form>

        <aside className="panel newrun__side">
          <h2>Examples</h2>
          <ul className="examples">
            {EXAMPLES.map((example) => (
              <li key={example.description}>
                <button
                  type="button"
                  onClick={() => {
                    setRepoUrl(example.repo)
                    setDescription(example.description)
                  }}
                >
                  <span className="examples__repo">{example.repo.replace(/^https:\/\//, '')}</span>
                  <span className="examples__desc">{example.description}</span>
                </button>
              </li>
            ))}
          </ul>

          <h2>What happens next</h2>
          <ol className="steps-explainer">
            <li>The repo is cloned onto the worker.</li>
            <li>A sandbox container starts with no network and hard resource caps.</li>
            <li>The agent reads, edits, and runs commands inside it.</li>
            <li>You watch every step stream in live.</li>
            <li>The container is destroyed; the diff is kept.</li>
          </ol>
        </aside>
      </div>
    </div>
  )
}
