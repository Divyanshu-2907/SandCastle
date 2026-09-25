import { describe, expect, it } from 'vitest'
import { splitUnifiedDiff } from '../components/DiffView'

// A real diff produced by the agent, copied out of the database.
const REAL_DIFF = `diff --git a/README b/README
index 980a0d5..0fa17d8 100644
--- a/README
+++ b/README
@@ -1 +1,5 @@
 Hello World!
+============
+
+A simple Hello World repository.
+
`

describe('splitUnifiedDiff', () => {
  it('separates the two sides of a change', () => {
    const sides = splitUnifiedDiff(REAL_DIFF)

    // Context appears on both sides; additions only on the new side.
    expect(sides.before).toBe('Hello World!')
    expect(sides.after).toBe(
      'Hello World!\n============\n\nA simple Hello World repository.\n',
    )
  })

  it('counts additions and deletions', () => {
    const sides = splitUnifiedDiff(REAL_DIFF)
    expect(sides.additions).toBe(4)
    expect(sides.deletions).toBe(0)
  })

  it('reads the starting line from the hunk header', () => {
    const sides = splitUnifiedDiff(`--- a/x
+++ b/x
@@ -42,3 +42,4 @@
 context
+added
`)
    expect(sides.startLine).toBe(42)
  })

  it('handles a modification, not just an append', () => {
    const sides = splitUnifiedDiff(`--- a/x
+++ b/x
@@ -1,3 +1,3 @@
 keep
-old line
+new line
 tail
`)
    expect(sides.before).toBe('keep\nold line\ntail')
    expect(sides.after).toBe('keep\nnew line\ntail')
    expect(sides.additions).toBe(1)
    expect(sides.deletions).toBe(1)
  })

  it('ignores file headers so they do not leak into the content', () => {
    const sides = splitUnifiedDiff(REAL_DIFF)
    expect(sides.before).not.toContain('diff --git')
    expect(sides.before).not.toContain('index 980a0d5')
    expect(sides.after).not.toContain('+++')
  })

  it('drops the no-newline marker rather than rendering it as content', () => {
    const sides = splitUnifiedDiff(`--- a/x
+++ b/x
@@ -1 +1 @@
-a
+b
\\ No newline at end of file
`)
    expect(sides.after).not.toContain('No newline')
    expect(sides.after.trim()).toBe('b')
  })

  it('reports multiple hunks so the view can flag approximate numbering', () => {
    const sides = splitUnifiedDiff(`--- a/x
+++ b/x
@@ -1,2 +1,2 @@
 one
-two
+TWO
@@ -50,2 +50,2 @@
 fifty
-51
+FIFTY-ONE
`)
    expect(sides.hunks).toBe(2)
    expect(sides.startLine).toBe(1)
    expect(sides.additions).toBe(2)
    expect(sides.deletions).toBe(2)
  })

  it('survives an empty diff without throwing', () => {
    const sides = splitUnifiedDiff('')
    expect(sides.before).toBe('')
    expect(sides.after).toBe('')
    expect(sides.hunks).toBe(0)
  })
})
