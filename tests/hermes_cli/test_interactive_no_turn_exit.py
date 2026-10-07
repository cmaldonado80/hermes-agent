"""Honest exit codes for no-query smoke runs (2026-09-18 incident).

A piped ``hermes chat`` with no ``-q``/``--query-file`` used to boot the REPL,
read EOF, print "Goodbye" and exit 0 having never called the model — a smoke
runner reading only the exit code saw "OK" against a dead provider and a full
QA card was wasted. The interactive REPL now exits ``1`` with a clear stderr
line when its stdin was never a TTY and no conversation turn ran in the
process; a real TTY session that runs zero or more turns keeps exit 0, and a
piped run whose turn DID run keeps the one-shot contract (tested in
test_single_query_exit_contract.py / test_oneshot_exit_contract.py).
"""

from types import SimpleNamespace

import pytest

import cli


class _FakeStdin:
    def __init__(self, isatty: bool):
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty


def _fake_cli(ran_turn: bool, stdin_usable: bool = True):
    return SimpleNamespace(
        _last_turn_result=({"final_response": "hi", "completed": True} if ran_turn else None),
        _tui_stdin_usable=lambda: stdin_usable,
    )


def test_piped_repl_that_never_ran_a_turn_reports_why(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin(isatty=False))
    reason = cli._interactive_run_never_turned(_fake_cli(ran_turn=False))
    assert reason == "stdin was not a TTY and no input arrived"


def test_piped_repl_with_unusable_stdin_reports_why(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin(isatty=False))
    reason = cli._interactive_run_never_turned(_fake_cli(ran_turn=False, stdin_usable=False))
    assert reason == "stdin is not usable"


def test_tty_session_without_a_turn_still_exits_clean(monkeypatch):
    """Someone opened chat on a real terminal and quit — exit 0 stays honest."""
    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin(isatty=True))
    assert cli._interactive_run_never_turned(_fake_cli(ran_turn=False)) is None


def test_piped_repl_whose_turn_ran_still_exits_clean(monkeypatch):
    """Non-TTY but a turn executed: the one-shot exit contract owns the code."""
    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin(isatty=False))
    assert cli._interactive_run_never_turned(_fake_cli(ran_turn=True)) is None


def test_unpollable_stdin_is_treated_as_non_interactive(monkeypatch):
    class _BrokenStdin:
        def isatty(self):
            raise OSError("fd 0 closed")

    monkeypatch.setattr(cli.sys, "stdin", _BrokenStdin())
    reason = cli._interactive_run_never_turned(_fake_cli(ran_turn=False))
    assert reason is not None


def test_main_after_run_exits_one_when_piped_and_turnless(monkeypatch, capsys):
    """End-to-end: ``cli.main`` with no query, piped stdin, and a ``run()`` that
    never turned must raise SystemExit(1) and print the guidance to stderr."""

    class _PipedCli(SimpleNamespace):
        def run(self):
            return None

    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin(isatty=False))
    monkeypatch.setattr(cli, "HermesCLI", lambda **_kwargs: _PipedCli(
        _last_turn_result=None, _tui_stdin_usable=lambda: True))
    monkeypatch.setattr(cli.atexit, "register", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_install_single_query_signal_handlers", lambda _cli: None)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(toolsets="terminal")

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "no conversation turn ran" in stderr
    assert "-q" in stderr  # the fix command is named


def test_main_after_run_exits_zero_on_tty_without_a_turn(monkeypatch):
    class _TtyCli(SimpleNamespace):
        def run(self):
            return None

    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin(isatty=True))
    monkeypatch.setattr(cli, "HermesCLI", lambda **_kwargs: _TtyCli(
        _last_turn_result=None, _tui_stdin_usable=lambda: True))
    monkeypatch.setattr(cli.atexit, "register", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_install_single_query_signal_handlers", lambda _cli: None)

    # No SystemExit: falls off the end of main() = implicit exit 0.
    assert cli.main(toolsets="terminal") is None
