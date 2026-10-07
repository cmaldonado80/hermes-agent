"""CLI surface tests for `hermes kanban revive` — the triage exit verb.

Exercises the same entry point the interactive CLI and gateway use
(``run_slash``): the happy path out of triage, the clear refusal on
non-triage cards, and bulk ids.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


def _triage_card(conn, title="stuck in triage"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    return tid


def test_revive_cli_moves_triage_to_ready(kanban_home):
    with kbc.connect() as conn:
        tid = _triage_card(conn)
    out = kc.run_slash(f"revive {tid}")
    assert "ready" in out
    assert "Revived" in out
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "revived" in kinds


def test_revive_cli_rejects_non_triage_with_error(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="normal card", assignee="worker")
    out = kc.run_slash(f"revive {tid}")
    assert "cannot revive" in out
    assert "triage" in out
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "ready"


def test_revive_cli_bulk_and_reason(kanban_home):
    with kbc.connect() as conn:
        t1 = _triage_card(conn, title="one")
        t2 = _triage_card(conn, title="two")
    out = kc.run_slash(f"revive {t1} {t2} --reason 'model fixed'")
    assert out.count("Revived") == 2
    with kbc.connect() as conn:
        for tid in (t1, t2):
            task = kb.get_task(conn, tid)
            assert task is not None and task.status == "ready"
        ev = [e for e in kb.list_events(conn, t1) if e.kind == "revived"]
        assert ev and (ev[0].payload or {}).get("reason") == "model fixed"


def test_revive_cli_requires_ids(kanban_home):
    out = kc.run_slash("revive")
    assert "usage error" in out
    assert "required" in out
