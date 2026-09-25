import { useMemo, useState } from 'react'
import ReactDiffViewer, { DiffMethod } from 'react-diff-viewer-continued'
import type { Diff } from '../api/types'

/**
 * A unified diff carries only the changed hunks, not whole files, so the two
 * sides have to be rebuilt from it: the old side is context + removed lines, the
 * new side is context + added lines.
 *
 * `startLine` comes from the first `@@` header so numbering matches the real
 * file. With multiple hunks the sides are concatenated, so numbering after the
 * first hunk is approximate -- the raw view below is the exact record.
 */
interface Sides {
  before: string
  after: string
  startLine: number
  additions: number
  deletions: number
  hunks: number
}

const HUNK_HEADER = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/

export function splitUnifiedDiff(diffText: string): Sides {
  const before: string[] = []
  const after: string[] = []
  let startLine = 1
  let additions = 0
  let deletions = 0
  let hunks = 0
  let seenHunk = false

  const lines = diffText.split('\n')
  // A diff ends with a newline, so the split leaves one empty element behind.
  // Treating it as a context line appends a phantom blank line to both sides.
  // A genuinely added blank line is a "+" line, which survives this.
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop()

  for (const line of lines) {
    const header = HUNK_HEADER.exec(line)
    if (header) {
      hunks += 1
      if (!seenHunk) {
        startLine = Number(header[1]) || 1
        seenHunk = true
      } else {
        // Mark the boundary so concatenated hunks don't read as contiguous.
        before.push('')
        after.push('')
      }
      continue
    }
    if (!seenHunk) continue // file headers, index lines, mode changes

    if (line.startsWith('+')) {
      after.push(line.slice(1))
      additions += 1
    } else if (line.startsWith('-')) {
      before.push(line.slice(1))
      deletions += 1
    } else if (line.startsWith('\\')) {
      continue // "\ No newline at end of file"
    } else {
      const context = line.startsWith(' ') ? line.slice(1) : line
      before.push(context)
      after.push(context)
    }
  }

  return {
    before: before.join('\n'),
    after: after.join('\n'),
    startLine,
    additions,
    deletions,
    hunks,
  }
}

// Matches the app's palette so the viewer doesn't look bolted on.
const VIEWER_STYLES = {
  variables: {
    dark: {
      diffViewerBackground: '#0d1117',
      diffViewerColor: '#e6edf3',
      addedBackground: '#12261e',
      addedColor: '#aff5b4',
      removedBackground: '#25171c',
      removedColor: '#ffdcd7',
      wordAddedBackground: '#1f6f34',
      wordRemovedBackground: '#8d2c33',
      addedGutterBackground: '#16301f',
      removedGutterBackground: '#2d1a1f',
      gutterBackground: '#0d1117',
      gutterColor: '#6e7681',
      codeFoldBackground: '#161b22',
      codeFoldGutterBackground: '#161b22',
      emptyLineBackground: '#0d1117',
    },
  },
  line: { fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace', fontSize: '12px' },
}

function FileDiff({ diff }: { diff: Diff }) {
  const sides = useMemo(() => splitUnifiedDiff(diff.diff_text), [diff.diff_text])
  const [raw, setRaw] = useState(false)

  return (
    <section className="filediff">
      <header className="filediff__head">
        <span className="filediff__path">{diff.file_path}</span>
        <span className="filediff__stat filediff__stat--add">+{sides.additions}</span>
        <span className="filediff__stat filediff__stat--del">−{sides.deletions}</span>
        <button type="button" className="filediff__toggle" onClick={() => setRaw((v) => !v)}>
          {raw ? 'split view' : 'raw diff'}
        </button>
      </header>

      {raw ? (
        <pre className="filediff__raw">{diff.diff_text}</pre>
      ) : (
        <div className="filediff__viewer">
          <ReactDiffViewer
            oldValue={sides.before}
            newValue={sides.after}
            splitView
            useDarkTheme
            compareMethod={DiffMethod.WORDS}
            linesOffset={sides.startLine - 1}
            styles={VIEWER_STYLES}
          />
          {sides.hunks > 1 && (
            <p className="filediff__note">
              {sides.hunks} hunks shown together — line numbers after the first are
              approximate. Use “raw diff” for the exact record.
            </p>
          )}
        </div>
      )}
    </section>
  )
}

export function DiffView({ diffs }: { diffs: Diff[] }) {
  if (diffs.length === 0) {
    return <p className="empty">This run did not change any files.</p>
  }
  return (
    <div className="diffs">
      {diffs.map((diff) => (
        <FileDiff key={diff.id || diff.file_path} diff={diff} />
      ))}
    </div>
  )
}
