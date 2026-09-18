import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

/**
 * Honest exit codes for no-TTY smoke runs (2026-09-18 incident).
 *
 * `hermes chat --tui` on a pipe reached entry.tsx with stdin.isTTY === false
 * and exited 0 before any model turn ran; `_launch_tui` propagates that code,
 * so a smoke runner saw "OK" against a dead provider. The bail-out must exit
 * non-zero. Asserting on the source (not spawning the TUI) matches the
 * bundle-shape test precedent and keeps the test TTY-free.
 */

const entrySource = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '..', 'entry.tsx'),
  'utf8',
)

const noTtyBlock = /if \(!process\.stdin\.isTTY\) \{[\s\S]*?\}/.exec(entrySource)

describe('no-TTY bail-out exits non-zero', () => {
  it('has the guard at module top (before the gateway client starts)', () => {
    expect(noTtyBlock).not.toBeNull()
  })

  it('exits 1, not 0', () => {
    expect(noTtyBlock![0]).toContain('process.exit(1)')
    expect(noTtyBlock![0]).not.toContain('process.exit(0)')
  })
})
