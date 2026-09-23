"""Dispatcher invariants for backend-neutral Kanban progress detection."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_progress as kbp
from hermes_cli import kanban_diagnostics as kd
from tools import kanban_tools


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(
    conn, *, pid: int, max_runtime: int = 3600, review: bool = False,
) -> tuple[str, int]:
    tid = kb.create_task(
        conn,
        title="job",
        assignee="worker",
        max_runtime_seconds=max_runtime,
        # A remote backend may never write into this host path. Progress
        # classification must therefore depend only on board events.
        workspace_kind="worktree",
        workspace_path="/host/path/not-mounted-in-worker",
    )
    task = kb.claim_task(conn, tid)
    if review:
        assert task is not None
        assert kb.request_review(
            conn, tid, summary="implementation ready", reviewer="worker",
        )
        task = kb.claim_review_task(conn, tid)
    assert task is not None and task.current_run_id is not None
    kbd._set_worker_pid(conn, tid, pid)
    # The first heartbeat is the worker's protocol negotiation.  Merely
    # spawning a PID must not opt old/custom workers into stall supervision.
    assert kbd.heartbeat_worker(conn, tid, expected_run_id=task.current_run_id)
    old = int(time.time()) - 90
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old, tid))
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (old, task.current_run_id),
        )
    return tid, task.current_run_id


def _sleeping_worker() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _cleanup_worker(worker: subprocess.Popen[bytes]) -> None:
    if worker.poll() is None:
        worker.terminate()
    try:
        worker.wait(timeout=5)
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.wait(timeout=5)


def _dispatch_reclaims(conn) -> kbd.DispatchResult:
    result = kbd.DispatchResult()
    kbd._run_reclaim_phase(
        conn,
        result,
        stale_timeout_seconds=3600,
        progress_stall_timeout_seconds=1,
        failure_limit=2,
        reconcile_orphans=False,
    )
    return result


def test_dispatch_stalls_auto_heartbeat_without_worker_authored_progress(
    kanban_home,
    monkeypatch,
):
    """Review runs cannot be kept alive by automatic or outside-task signals."""
    worker = _sleeping_worker()
    legacy_worker = _sleeping_worker()
    try:
        with kbc.connect() as conn:
            tid, run_id = _running_task(conn, pid=worker.pid, review=True)
            legacy_tid, _ = _running_task(conn, pid=legacy_worker.pid)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_events SET payload = NULL "
                    "WHERE task_id = ? AND kind = 'heartbeat'",
                    (legacy_tid,),
                )
            legacy_spawn = next(
                event for event in kb.list_events(conn, legacy_tid)
                if event.kind == "spawned"
            )
            assert "progress_protocol" not in (legacy_spawn.payload or {})
            assert kbd.heartbeat_worker(conn, tid, expected_run_id=run_id)
            monkeypatch.setenv("HERMES_KANBAN_TASK", "t_other")
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "999")
            args = SimpleNamespace(
                task_id=tid,
                text=["cross-task", "handoff"],
                max_len=None,
                author="worker",
            )
            assert kanban_cli._cmd_comment(args) == 0

            result = _dispatch_reclaims(conn)

            assert result.progress_stalled == [tid]
            assert result.timed_out == []
            stalled_task = kb.get_task(conn, tid)
            assert stalled_task.status == "blocked"
            assert "progress_stalled" in {e.kind for e in kb.list_events(conn, tid)}
            diagnostics = kd.compute_task_diagnostics(
                stalled_task,
                kb.list_events(conn, tid),
                kb.list_runs(conn, tid),
            )
            repeated = next(d for d in diagnostics if d.kind == "repeated_failures")
            assert repeated.data["most_recent_outcome"] == "stalled"
            assert kb.get_task(conn, legacy_tid).status == "running"
    finally:
        _cleanup_worker(worker)
        _cleanup_worker(legacy_worker)


def test_dispatch_honors_authored_progress_and_timeout_precedence(
    kanban_home,
    monkeypatch,
):
    """Explicit heartbeat/CLI comment protect runs; configured timeout wins otherwise."""
    timed_worker = _sleeping_worker()
    try:
        with kbc.connect() as conn:
            in_flight_tid, in_flight_run = _running_task(conn, pid=os.getpid())
            in_flight_task = kb.get_task(conn, in_flight_tid)
            assert in_flight_task is not None
            monkeypatch.setenv("HERMES_KANBAN_TASK", in_flight_tid)
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(in_flight_run))
            monkeypatch.setenv(
                "HERMES_KANBAN_CLAIM_LOCK", str(in_flight_task.claim_lock),
            )
            monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_attempt", 0.0)
            monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_in_flight", False)
            assert kanban_tools.heartbeat_current_worker_from_env()
            # An in-flight transition must upgrade the recent liveness-only
            # heartbeat even though the ordinary 60-second limiter is active.
            assert kanban_tools.heartbeat_current_worker_from_env(in_flight=True)

            heartbeat_tid, heartbeat_run = _running_task(conn, pid=os.getpid())
            assert kbd.heartbeat_worker(
                conn,
                heartbeat_tid,
                note="long remote build",
                expected_run_id=heartbeat_run,
                explicit=True,
            )

            comment_tid, comment_run = _running_task(conn, pid=os.getpid())
            monkeypatch.setenv("HERMES_KANBAN_TASK", comment_tid)
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(comment_run))
            args = SimpleNamespace(
                task_id=comment_tid,
                text=["remote", "artifact", "ready"],
                max_len=None,
                author="worker",
            )
            assert kanban_cli._cmd_comment(args) == 0

            timeout_tid, _ = _running_task(
                conn,
                pid=timed_worker.pid,
                max_runtime=1,
            )
            result = _dispatch_reclaims(conn)

            assert result.progress_stalled == []
            assert result.timed_out == [timeout_tid]
            assert kb.get_task(conn, in_flight_tid).status == "running"
            assert kb.get_task(conn, heartbeat_tid).status == "running"
            assert kb.get_task(conn, comment_tid).status == "running"
            timeout_task = kb.get_task(conn, timeout_tid)
            assert timeout_task.status == "blocked"
            timeout_events = kb.list_events(conn, timeout_tid)
            terminal_kinds = [
                event.kind for event in timeout_events
                if event.kind in {"timed_out", "gave_up"}
            ]
            assert terminal_kinds == ["gave_up"]
            assert kb.list_runs(conn, timeout_tid)[-1].outcome == "timed_out"
            diagnostics = kd.compute_task_diagnostics(
                timeout_task,
                timeout_events,
                kb.list_runs(conn, timeout_tid),
            )
            repeated = next(d for d in diagnostics if d.kind == "repeated_failures")
            assert repeated.data["most_recent_outcome"] == "timed_out"
    finally:
        _cleanup_worker(timed_worker)


def test_recovered_stall_does_not_diagnose_a_later_manual_block(kanban_home):
    """A historical stalled run is not the active reason for a newer block."""
    worker = _sleeping_worker()
    try:
        with kbc.connect() as conn:
            tid, _ = _running_task(conn, pid=worker.pid)
            assert _dispatch_reclaims(conn).progress_stalled == [tid]
            assert kb.unblock_task(conn, tid)
            assert kb.block_task(
                conn, tid, kind="needs_input", reason="waiting for operator choice",
            )

            task = kb.get_task(conn, tid)
            diagnostics = kd.compute_task_diagnostics(
                task,
                kb.list_events(conn, tid),
                kb.list_runs(conn, tid),
            )

            assert all(d.kind != "repeated_failures" for d in diagnostics)
    finally:
        _cleanup_worker(worker)


def test_progress_committed_before_signal_wins_the_stall_race(
    kanban_home,
    monkeypatch,
):
    worker = _sleeping_worker()
    try:
        with kbc.connect() as conn:
            tid, _ = _running_task(conn, pid=worker.pid)
            checks = 0
            real_check = kbp.task_has_recent_progress

            def progress_arrives_at_fence(*args, **kwargs):
                nonlocal checks
                checks += 1
                return checks == 2 or real_check(*args, **kwargs)

            signals = []
            monkeypatch.setattr(kbp, "task_has_recent_progress", progress_arrives_at_fence)
            monkeypatch.setattr(kbd, "_kill_fn", lambda _signal_fn: signals.append)

            result = _dispatch_reclaims(conn)

            assert checks == 2
            assert signals == []
            assert result.progress_stalled == []
            assert kb.get_task(conn, tid).status == "running"
    finally:
        _cleanup_worker(worker)


def test_unbound_heartbeat_is_liveness_not_worker_progress(kanban_home):
    with kbc.connect() as conn:
        tid, run_id = _running_task(conn, pid=os.getpid())

        assert kbd.heartbeat_worker(
            conn,
            tid,
            note="operator checked the card",
            explicit=True,
        )

        assert not kbp.task_has_recent_progress(
            conn, tid, run_id, int(time.time()) - 5,
        )


def test_recent_progress_does_not_extend_hard_max_runtime(kanban_home):
    worker = _sleeping_worker()
    try:
        with kbc.connect() as conn:
            tid, run_id = _running_task(conn, pid=worker.pid, max_runtime=1)
            assert kbd.heartbeat_worker(
                conn,
                tid,
                expected_run_id=run_id,
                in_flight=True,
            )

            timed_out = kbp.enforce_max_runtime(conn)

            assert timed_out == [tid]
            # Progress changes retry classification, never the hard cap itself.
            assert kb.get_task(conn, tid).status == "ready"
            assert kb.list_runs(conn, tid)[-1].outcome == "timed_out"
    finally:
        _cleanup_worker(worker)


def test_progress_stall_timeout_is_configurable_and_can_be_disabled(kanban_home):
    default = kbp.DEFAULT_PROGRESS_STALL_TIMEOUT_SECONDS
    assert kbp.configured_progress_stall_timeout_seconds({}) == default
    assert kbp.configured_progress_stall_timeout_seconds(
        {"progress_stall_timeout_seconds": "1200"},
    ) == 1200
    assert kbp.configured_progress_stall_timeout_seconds(
        {"progress_stall_timeout_seconds": 0},
    ) == 0
    assert kbp.configured_progress_stall_timeout_seconds(
        {"progress_stall_timeout_seconds": 15},
    ) == kbp.MIN_PROGRESS_STALL_TIMEOUT_SECONDS
    assert kbp.configured_progress_stall_timeout_seconds(
        {"progress_stall_timeout_seconds": "invalid"},
    ) == default

    with kbc.connect() as conn:
        result = kbd.DispatchResult()
        kbd._run_reclaim_phase(
            conn,
            result,
            stale_timeout_seconds=3600,
            progress_stall_timeout_seconds=0,
            failure_limit=2,
            reconcile_orphans=False,
        )
        assert result.progress_stalled == []
