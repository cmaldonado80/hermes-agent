"""Tests for kb.revive_task — the triage exit (loop-detector / respawn-guard
reset). LLM-free by design; covers the full triage → revive → ready cycle,
the non-triage refusal, parent re-gating, and loop-state clearing."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _triage_via_loop_breaker(conn, title="looped"):
    """Drive a real card through the unblock-loop breaker into ``triage``.

    Mirrors test_kanban_block_kinds: block → unblock → re-block for the same
    cause routes the card to triage at BLOCK_RECURRENCE_LIMIT with a
    ``block_loop_detected`` event.
    """
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    kb.block_task(conn, tid, reason="x", kind="capability")
    assert kb.unblock_task(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    kb.block_task(conn, tid, reason="x", kind="capability")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def test_loop_breaker_then_revive_lands_ready(kanban_home):
    """Fixture provenance: a genuine loop-detector triage card revives to ready."""
    with kbc.connect_closing() as conn:
        tid = _triage_via_loop_breaker(conn)
        ok, err = kb.revive_task(conn, tid, actor="operator", reason="fixed the model")
        assert (ok, err) == (True, None)
        task = kb.get_task(conn, tid)
    assert task.status == "ready"
    assert task.block_kind is None
    assert int(task.block_recurrences or 0) == 0
    assert int(task.consecutive_failures or 0) == 0


def test_revive_records_event_with_provenance(kanban_home):
    """The revived event carries who/when/from-where plus the cleared loop-state."""
    with kbc.connect_closing() as conn:
        tid = _triage_via_loop_breaker(conn)
        before = [e.kind for e in kb.list_events(conn, tid)]
        assert "block_loop_detected" in before
        kb.revive_task(conn, tid, actor="alice", reason="model fixed")
        events = kb.list_events(conn, tid)
    revived = [e for e in events if e.kind == "revived"]
    assert len(revived) == 1
    payload = revived[0].payload or {}
    assert payload["from_status"] == "triage"
    assert payload["status"] == "ready"
    assert payload["actor"] == "alice"
    assert payload["reason"] == "model fixed"
    # The audit snapshot preserves what triage had caught (breaker state).
    assert payload["cleared"]["block_kind"] == "capability"
    assert payload["cleared"]["block_recurrences"] >= kb.BLOCK_RECURRENCE_LIMIT


def test_revive_rejects_non_triage_statuses(kanban_home):
    """Normal-state cards reject revive with a clear error, and nothing changes."""
    with kbc.connect_closing() as conn:
        ready_tid = kb.create_task(conn, title="ready card", assignee="w")
        ok, err = kb.revive_task(conn, ready_tid, actor="op")
        assert ok is False
        assert "ready" in (err or "")
        assert "triage" in (err or "")
        assert kb.get_task(conn, ready_tid).status == "ready"
        # Unknown id is also a clear refusal, not an exception.
        ok2, err2 = kb.revive_task(conn, "t_missing0")
        assert ok2 is False
        assert "not found" in (err2 or "")
        # A done card refuses too (revive must not resurrect completed work).
        done_tid = kb.create_task(conn, title="done card", assignee="w")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (done_tid,))
        kb.claim_task(conn, done_tid, claimer="w")
        kb.complete_task(conn, done_tid, result="done")
        ok3, err3 = kb.revive_task(conn, done_tid)
        assert ok3 is False
        assert "done" in (err3 or "")


def test_revive_respects_parent_gates(kanban_home):
    """Revive must not promote a child past an unfinished parent: it lands in
    todo and recompute_ready flips it once the parent completes."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(conn, title="child", assignee="worker", parents=(parent,))
        assert kb.get_task(conn, child).status == "todo"
        # Park the child in triage the way the loop breaker would.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (child,))
        ok, err = kb.revive_task(conn, child, actor="op")
        assert (ok, err) == (True, None)
        assert kb.get_task(conn, child).status == "todo", "parent gate must hold"
        # Parent completes -> recompute_ready promotes the child.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


def test_revive_clears_respawn_guard_inputs(kanban_home):
    """A dead-model card carries last_failure_error that check_respawn_guard
    reads as blocker_auth; revive clears it so the re-dispatch can proceed
    once the model is fixed (combined with the dispatch liveness gate, no
    spiral: the card only re-spawns when the probe passes)."""
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        tid = _triage_via_loop_breaker(conn)
        # Simulate the dead-model failure bookkeeping that precedes triage.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET last_failure_error = ?, consecutive_failures = 3 "
                "WHERE id = ?",
                ("spawn failed: auth error 401 invalid api key", tid),
            )
        # The guard holds the card before revive...
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"
        ok, _ = kb.revive_task(conn, tid, actor="op")
        assert ok
        # ...and no longer does after: the guard inputs were cleared.
        assert kbd.check_respawn_guard(conn, tid) is None
