"""Kanban dispatch honors the global emergency stop on every dispatch path.

`hermes pause` must hold kanban dispatch whether the tick comes from the gateway
watcher, `hermes kanban dispatch`, or the dashboard. Only the gateway watcher
checked the sentinel, so CLI dispatches kept spawning workers while paused
(54 runs across two days on one machine). The guard lives in ``dispatch_once``
so every caller inherits it; a dry run spawns nothing and stays allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import estop
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    estop._logged_components.clear()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _spy():
    calls: list = []

    def spawn(*args, **kwargs):
        calls.append(args[0] if args else kwargs)
        return 4242

    return spawn, calls


def test_engaged_estop_spawns_nothing(conn, all_assignees_spawnable):
    kb.create_task(conn, title="t", assignee="alice")
    estop.engage(reason="test")
    spawn, calls = _spy()

    result = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert calls == [], "dispatch_once spawned a worker while the emergency stop was engaged"
    assert result.spawned == []
    assert result.paused is True


def test_dry_run_is_not_blocked_by_estop(conn, all_assignees_spawnable):
    kb.create_task(conn, title="t", assignee="alice")
    estop.engage()
    spawn, _ = _spy()

    result = kbd.dispatch_once(conn, spawn_fn=spawn, dry_run=True)

    assert result.paused is False


def test_resume_lets_dispatch_spawn_again(conn, all_assignees_spawnable):
    tid = kb.create_task(conn, title="t", assignee="alice")
    estop.engage()
    spawn, calls = _spy()

    kbd.dispatch_once(conn, spawn_fn=spawn)
    assert calls == [], "dispatch_once spawned a worker while the emergency stop was engaged"

    estop.disengage()
    result = kbd.dispatch_once(conn, spawn_fn=spawn)

    assert result.paused is False
    assert any(row[0] == tid for row in result.spawned)
