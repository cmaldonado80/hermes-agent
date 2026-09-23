"""Backend-neutral Kanban worker progress signals and stall supervision."""

from __future__ import annotations

import sqlite3
import time
from typing import Optional


# Automatic worker activity is persisted at most once per minute.  A shorter
# watchdog cannot distinguish a healthy in-flight call from silence.
PROGRESS_HEARTBEAT_MIN_INTERVAL_SECONDS = 60
MIN_PROGRESS_STALL_TIMEOUT_SECONDS = PROGRESS_HEARTBEAT_MIN_INTERVAL_SECONDS * 2
DEFAULT_PROGRESS_STALL_TIMEOUT_SECONDS = 900
PROGRESS_PROTOCOL_VERSION = 1


def _kb():
    # Late-bound to keep the split modules' import cycle identical to the other
    # kanban_db siblings.
    from hermes_cli import kanban_db

    return kanban_db


def configured_progress_stall_timeout_seconds(kanban_cfg: Optional[dict] = None) -> int:
    """Resolve the progress-stall watchdog from ``config.yaml``.

    ``0`` disables the policy. Positive values are clamped to the activity
    heartbeat cadence; invalid or negative values use the documented default.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config_readonly

            kanban_cfg = (load_config_readonly() or {}).get("kanban", {})
        except Exception:
            kanban_cfg = {}
    raw = (kanban_cfg or {}).get(
        "progress_stall_timeout_seconds", DEFAULT_PROGRESS_STALL_TIMEOUT_SECONDS,
    )
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PROGRESS_STALL_TIMEOUT_SECONDS
    if parsed == 0:
        return 0
    if parsed < 0:
        return DEFAULT_PROGRESS_STALL_TIMEOUT_SECONDS
    return max(MIN_PROGRESS_STALL_TIMEOUT_SECONDS, parsed)


def _task_has_worker_comment_since(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int], since_ts: int,
) -> bool:
    """Whether this exact worker run emitted a comment since ``since_ts``."""
    if run_id is None:
        return False
    return conn.execute(
        "SELECT 1 FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind = 'commented' "
        "  AND created_at >= ? LIMIT 1",
        (task_id, int(run_id), int(since_ts)),
    ).fetchone() is not None


def _task_has_recent_progress_heartbeat(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int], since_ts: int,
) -> bool:
    """Whether this run explicitly heartbeated or has a call in flight."""
    if run_id is None:
        return False
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind = 'heartbeat' "
        "  AND created_at >= ? AND payload IS NOT NULL ORDER BY id DESC",
        (task_id, int(run_id), int(since_ts)),
    ).fetchall()
    for row in rows:
        payload = _kb()._json_dict(row["payload"])
        if payload.get("unbound") is True:
            continue
        # ``note`` recognizes explicit heartbeats written before the explicit
        # marker existed. Plain automatic liveness heartbeats have a null
        # payload; active model/tool calls carry ``in_flight``.
        if (
            payload.get("explicit") is True
            or bool(payload.get("note"))
            or payload.get("in_flight") is True
        ):
            return True
    return False


def task_has_recent_progress(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int], since_ts: int,
) -> bool:
    """Backend-neutral worker-authored progress for this exact run."""
    return _task_has_worker_comment_since(
        conn, task_id, run_id, since_ts,
    ) or _task_has_recent_progress_heartbeat(
        conn, task_id, run_id, since_ts,
    )


def run_supports_progress_protocol(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int],
) -> bool:
    """Whether this run's worker proved support for progress markers."""
    if run_id is None:
        return False
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND run_id = ? AND kind = 'heartbeat' "
        "  AND payload IS NOT NULL ORDER BY id DESC",
        (task_id, int(run_id)),
    ).fetchall()
    return any(
        isinstance(version := _kb()._json_dict(row["payload"]).get("progress_protocol"), int)
        and version >= PROGRESS_PROTOCOL_VERSION
        for row in rows
    )


def run_is_empty(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: Optional[int],
    run_started_at: int,
) -> bool:
    """Whether this attempt has no backend-neutral worker-authored progress."""
    if not run_supports_progress_protocol(conn, task_id, run_id):
        return False
    return not task_has_recent_progress(conn, task_id, run_id, int(run_started_at))


def enforce_max_runtime(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """Terminate host-local workers whose per-attempt runtime cap elapsed."""
    from hermes_cli.kanban_db_dispatch import UNVERIFIED_WORKER_FINGERPRINT

    kb = _kb()
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = kb._host_prefix()
    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock, t.current_run_id "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        elapsed = now - int(row["active_started_at"])
        limit = int(row["max_runtime_seconds"])
        if elapsed < limit:
            continue
        tid = row["id"]

        pid = int(row["worker_pid"])
        started_at = kb._row_get(row, "worker_started_at")
        if started_at == UNVERIFIED_WORKER_FINGERPRINT and kb._pid_alive(pid):
            kb._log.warning(
                "kanban: task %s worker pid %s exceeded max runtime but has no verified "
                "identity; not signalled", tid, pid,
            )
            continue
        worker_was_live = kb._worker_alive(pid, started_at)
        termination = kb._terminate_reclaimed_worker(
            pid, lock, signal_fn=signal_fn, started_at=started_at,
        )
        if kb._worker_survived_termination(termination):
            kb._defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="max_runtime_worker_alive",
            )
            continue

        error = f"elapsed {int(elapsed)}s > limit {limit}s"
        review_run = kb._retry_status_for_run(conn, tid) == "review"
        empty = worker_was_live and (not review_run) and run_is_empty(
            conn, tid, row["current_run_id"], int(row["active_started_at"]),
        )
        with kb.write_txn(conn):
            retry_status = kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, tid, pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": limit,
                    "retry_status": retry_status,
                    "empty_run": empty,
                }
                payload.update(termination)
                run_id = kb._end_run(
                    conn, tid, outcome="timed_out", status="timed_out",
                    error=error, metadata=payload,
                )
                # Empty timeouts emit only the force-trip event below, avoiding
                # two user notifications for the same terminal transition.
                if not empty:
                    kb._append_event(conn, tid, "timed_out", payload, run_id=run_id)
                timed_out.append(tid)
        if cur.rowcount == 1:
            extra = {
                "pid": pid,
                "elapsed_seconds": int(elapsed),
                "limit_seconds": limit,
                "retry_status": retry_status,
                "empty_run": empty,
                **termination,
            }
            if empty:
                extra["empty_timeout"] = True
            kb._record_task_failure(
                conn, tid,
                error=error if not empty else error + " (empty run: no worker-authored progress)",
                outcome="timed_out",
                force_trip=empty,
                release_claim=False,
                end_run=False,
                event_payload_extra=extra,
            )
    return timed_out


def detect_progress_stall(
    conn: sqlite3.Connection,
    *,
    stall_timeout_seconds: int = DEFAULT_PROGRESS_STALL_TIMEOUT_SECONDS,
    signal_fn=None,
) -> list[str]:
    """Block live host-local workers that stopped reporting progress."""
    if stall_timeout_seconds <= 0:
        return []

    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli.kanban_db_dispatch import UNVERIFIED_WORKER_FINGERPRINT

    kb = _kb()
    stalled: list[str] = []
    now = int(time.time())
    host_prefix = kb._host_prefix()
    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.current_run_id "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        started = int(row["active_started_at"])
        elapsed = now - started
        if elapsed < stall_timeout_seconds:
            continue
        tid = row["id"]
        pid = int(row["worker_pid"])
        started_at = kb._row_get(row, "worker_started_at")
        if not run_supports_progress_protocol(conn, tid, row["current_run_id"]):
            continue
        if started_at == UNVERIFIED_WORKER_FINGERPRINT and kb._pid_alive(pid):
            continue
        if not kb._worker_alive(pid, started_at):
            continue
        if task_has_recent_progress(
            conn, tid, row["current_run_id"], now - stall_timeout_seconds,
        ):
            continue
        # A worker can report progress between the optimistic read above and
        # termination. BEGIN IMMEDIATE serializes this final predicate with the
        # SIGTERM request, so any progress committed before the signal wins.
        with kb.write_txn(conn):
            fresh = conn.execute(
                "SELECT current_run_id, worker_pid, worker_started_at, claim_lock "
                "FROM tasks WHERE id = ? AND status = 'running'",
                (tid,),
            ).fetchone()
            if (
                fresh is None
                or fresh["worker_pid"] != pid
                or fresh["claim_lock"] != row["claim_lock"]
                or fresh["current_run_id"] != row["current_run_id"]
                or task_has_recent_progress(
                    conn, tid, fresh["current_run_id"], now - stall_timeout_seconds,
                )
            ):
                continue
            started_at = kb._row_get(fresh, "worker_started_at")
            termination, kill = kbd._request_reclaimed_worker_termination(
                pid, lock, signal_fn=signal_fn, started_at=started_at,
            )
        termination = kbd._finish_reclaimed_worker_termination(
            pid, started_at, termination, kill,
        )
        if kb._worker_survived_termination(termination):
            kb._defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="progress_stall_worker_alive",
            )
            continue
        error = (
            "no worker comment, explicit heartbeat, or in-flight call for "
            f"{stall_timeout_seconds}s after {int(elapsed)}s running "
            f"(progress stall {stall_timeout_seconds}s)"
        )
        with kb.write_txn(conn):
            retry_status = kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, tid, pid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue
            payload = {
                "pid": pid,
                "elapsed_seconds": int(elapsed),
                "stall_seconds": stall_timeout_seconds,
                "retry_status": retry_status,
                "empty_run": True,
            }
            payload.update(termination)
            run_id = kb._end_run(
                conn, tid, outcome="stalled", status="stalled",
                error=error, metadata=payload,
            )
            kb._append_event(conn, tid, "progress_stalled", payload, run_id=run_id)
            stalled.append(tid)
        kb._record_task_failure(
            conn, tid,
            error=error,
            outcome="stalled",
            force_trip=True,
            release_claim=False,
            end_run=False,
            event_payload_extra={
                "pid": pid,
                "empty_run": True,
                "stall_seconds": stall_timeout_seconds,
                **termination,
            },
        )
    return stalled
