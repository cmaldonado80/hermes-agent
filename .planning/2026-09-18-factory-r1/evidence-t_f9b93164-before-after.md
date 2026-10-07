# t_f9b93164 — Honest exit codes in smoke runs: before/after reproduction

Repository: /Users/cmaldonado/.hermes/hermes-agent @ 0a8d4caef4650320b31f9c17a906293e680be5df
Worktree (attempt t_f9b93164-run388-a): /Users/cmaldonado/.hermes/hermes-agent/.worktrees/t_f9b93164-run388-a
Date: 2026-09-18

## Root cause (verified against the 2026-09-18 incident)

Incident evidence (kanban.db task_events, t_bf800ee4 / t_8a1770ec):
- 2026-09-18 05:28:30 the QA card's model was re-pinned to `gpt-5.6-terra` /
  `copilot` (dead: HTTP 400 model_not_supported).
- The dispatcher-spawned workers (`chat -q`) DID exit 1 — the one-shot paths
  (`chat -q`, `-Q`, `-z`, `--format stream-json`) already map turn outcome to
  exit code on this base (commits be9d4369a7, 23863ccbaf, 391a7007fb all in
  0a8d4ca). The wasted QA card came from the *manual smoke pattern*
  `hermes chat -p X -m "ping"` used before pinning a model.
- In this CLI, `-m` is `--model` (NOT --message): the command carries NO query.
  On a pipe (non-TTY stdin) that boots the interactive REPL, which reads EOF
  immediately, prints "Goodbye! ☤", and **exits 0 having never called the
  model** — indistinguishable from a successful smoke against a live model.

Two more silent-0 bail-outs in the same class:
- classic REPL: `_tui_stdin_usable()` False (fd 0 broken) → silent return → 0.
- `--tui` on a pipe: ui-tui/src/entry.tsx printed "hermes-tui: no TTY" and
  `process.exit(0)`; `_launch_tui` propagates the child code.

## Before (base 0a8d4ca, main checkout, dead provider via HERMES_HOME fixture)

Fixture: HERMES_HOME=/tmp/hermes-dead-provider (zai + invalid key sk-totally-invalid...,
HTTP 401 on every call). Command shapes probed with stdin </dev/null:

| command | exit | note |
|---|---|---|
| `hermes chat -m "ping"` | **0** | THE INCIDENT: REPL read EOF, "Goodbye! ☤", model never called (log: /tmp/out3.txt, /tmp/out8.txt) |
| `hermes chat -q "ping" --tui` | **0** | "hermes-tui: no TTY", exit 0 (log: /tmp/out11.txt) |
| `hermes chat -q "ping"` | 1 | already honest (turn failed: 401) |
| `hermes chat -Q -q "ping"` | 1 | already honest |
| `hermes -z "ping"` | 2 | already honest |
| `hermes chat --format stream-json -q "ping"` | 1 | already honest; result record exit_code:1 |

Live-model control on the same base: `hermes chat -Q -q "ping"` (fallback
answered) → exit 0. Correct then and now.

## After (worktree, same fixtures)

| command | exit | stderr / last line |
|---|---|---|
| `hermes chat -m "ping"` | **1** | `hermes: no conversation turn ran in this process (stdin was not a TTY and no input arrived); non-interactive chat requires a query (-q "..." / --query-file - / -z "...").` |
| `hermes chat -q "ping" --tui` | **1** | `hermes-tui: no TTY` |
| `hermes chat -q "ping"` (dead provider) | 1 | unchanged (provider error copy) |
| `hermes -z "ping"` (dead provider) | 2 | unchanged |
| `hermes -z "reply with exactly: PONG"` (LIVE model, engineer profile) | **0** | `PONG` — no regression |

## Change summary

1. cli.py — `_interactive_run_never_turned(cli)` + call after `cli.run()` in
   `main()`: non-TTY stdin + zero turns → sys.exit(1) with guidance on stderr.
   TTY sessions (zero or more turns) and piped runs whose turn ran keep their
   existing codes. Reuses `_last_turn_result` — the SAME signal the one-shot
   exit contract reads; no new heuristics.
2. ui-tui/src/entry.tsx — no-TTY bail-out exits 1 instead of 0.
3. Docs: website/docs/reference/cli-commands.md "Exit codes" section.

## Tests

- tests/hermes_cli/test_interactive_no_turn_exit.py (new, 7 tests): piped
  turnless → reason; unusable stdin → reason; TTY turnless → None (exit 0);
  piped with turn → None; unpollable stdin → non-interactive; main() end-to-end
  SystemExit(1) + stderr; main() TTY end-to-end implicit 0.
- ui-tui/src/__tests__/noTtyExitCode.test.ts (new, 2 tests): guard present,
  exits 1 not 0.
- Regression: existing exit-contract suites all green
  (test_single_query_exit_contract, test_oneshot_exit_contract,
  test_chat_q_exit_clear, test_single_query_session_finalize,
  test_quiet_single_query, test_cli_quiet_stdout_leak — 33 passed via
  scripts/run_tests.sh).
