"""Dispatch-time provider liveness gate contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.real_kanban_liveness_gate


@pytest.fixture()
def kanban_home():
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_dispatch_liveness as liveness

    liveness._probe_cache.clear()
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Liveness test")
    return kb, kbc, kbd, liveness


def _fake_spawn(*_args, **_kwargs):
    return 12345


def _create_task(kb, kbc, *, title="task", model="test-model", provider="test-provider"):
    with kbc.connect_closing() as conn:
        return kb.create_task(
            conn,
            title=title,
            assignee="default",
            model_override=model,
            provider_override=provider,
        )


def _dispatch(kbd, kbc):
    with kbc.connect_closing() as conn:
        return kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)


def _patch_gate(monkeypatch, liveness, probe):
    monkeypatch.setattr(liveness, "liveness_gate_enabled", lambda: True)
    monkeypatch.setattr(liveness, "probe_route", probe)


def test_dead_route_stays_ready_with_comment_event_and_telemetry(
    kanban_home, monkeypatch,
):
    kb, kbc, kbd, liveness = kanban_home
    task_id = _create_task(kb, kbc)
    outcome = liveness.ProbeOutcome(
        False, "test-provider", "test-model", "401 invalid api key",
    )
    _patch_gate(monkeypatch, liveness, lambda *_args: outcome)

    result = _dispatch(kbd, kbc)

    assert result.spawned == []
    assert result.liveness_blocked == [
        (task_id, "test-provider/test-model", "401 invalid api key"),
    ]
    assert "liveness_blocked=1" in kbd.describe_suppression([result])
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        comments = kb.list_comments(conn, task_id)
        events = [event for event in kb.list_events(conn, task_id) if event.kind == "liveness_blocked"]
    assert task is not None
    assert task.status == "ready"
    assert task.claim_lock is None
    assert len(comments) == 1
    assert comments[0].author == "system"
    assert "provider `test-provider` model `test-model`" in comments[0].body
    assert "401 invalid api key" in comments[0].body
    assert len(events) == 1
    assert events[0].payload == {
        "provider": "test-provider",
        "model": "test-model",
        "error": "401 invalid api key",
    }


def test_dead_route_does_not_block_next_card_same_tick(kanban_home, monkeypatch):
    kb, kbc, kbd, liveness = kanban_home
    dead_id = _create_task(kb, kbc, title="dead", model="dead-model")
    live_id = _create_task(kb, kbc, title="live", model="live-model")

    def _probe(_profile, model, provider):
        return liveness.ProbeOutcome(
            model != "dead-model",
            provider,
            model,
            "404 model not found" if model == "dead-model" else "",
        )

    _patch_gate(monkeypatch, liveness, _probe)
    result = _dispatch(kbd, kbc)

    assert [task_id for task_id, _assignee, _workspace in result.spawned] == [live_id]
    assert [entry[0] for entry in result.liveness_blocked] == [dead_id]
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, dead_id).status == "ready"
        assert kb.get_task(conn, live_id).status == "running"


def test_alive_route_spawns_and_claims_normally(kanban_home, monkeypatch):
    kb, kbc, kbd, liveness = kanban_home
    task_id = _create_task(kb, kbc)
    _patch_gate(
        monkeypatch,
        liveness,
        lambda *_args: liveness.ProbeOutcome(True, "test-provider", "test-model", ""),
    )

    result = _dispatch(kbd, kbc)

    assert [entry[0] for entry in result.spawned] == [task_id]
    assert result.liveness_blocked == []
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    assert task.status == "running"
    assert task.claim_lock is not None


def test_timeout_route_stays_ready(kanban_home, monkeypatch):
    kb, kbc, kbd, liveness = kanban_home
    task_id = _create_task(kb, kbc)
    _patch_gate(
        monkeypatch,
        liveness,
        lambda *_args: liveness.ProbeOutcome(
            False, "test-provider", "test-model", "request timed out after 8s",
        ),
    )

    result = _dispatch(kbd, kbc)

    assert result.spawned == []
    assert result.liveness_blocked[0][2] == "request timed out after 8s"
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, task_id).status == "ready"


def test_unexpected_probe_failure_fails_open(kanban_home, monkeypatch):
    kb, kbc, kbd, liveness = kanban_home
    task_id = _create_task(kb, kbc)

    def _raise(*_args):
        raise RuntimeError("probe implementation broke")

    _patch_gate(monkeypatch, liveness, _raise)
    result = _dispatch(kbd, kbc)

    assert [entry[0] for entry in result.spawned] == [task_id]


def test_gate_off_does_not_call_probe(kanban_home, monkeypatch):
    kb, kbc, kbd, liveness = kanban_home
    task_id = _create_task(kb, kbc)
    from hermes_constants import get_hermes_home

    (get_hermes_home() / "config.yaml").write_text(
        "kanban:\n  dispatch_liveness_gate: false\n",
        encoding="utf-8",
    )

    def _unexpected(*_args):
        pytest.fail("probe_route must not be called when the gate is disabled")

    monkeypatch.setattr(liveness, "probe_route", _unexpected)
    result = _dispatch(kbd, kbc)

    assert [entry[0] for entry in result.spawned] == [task_id]


def test_probe_cache_reuses_live_route_for_two_cards(kanban_home, monkeypatch):
    kb, kbc, kbd, liveness = kanban_home
    first_id = _create_task(kb, kbc, title="first")
    second_id = _create_task(kb, kbc, title="second")
    from agent import auxiliary_client
    from hermes_cli import runtime_provider

    monkeypatch.setattr(
        runtime_provider,
        "resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "test-provider",
            "base_url": "https://provider.invalid/v1",
            "api_key": "test-placeholder",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(
        auxiliary_client,
        "resolve_provider_client",
        lambda *_args, **_kwargs: (SimpleNamespace(), "test-model"),
    )
    calls = []
    monkeypatch.setattr(
        liveness,
        "_single_request",
        lambda _client, model, timeout: calls.append((model, timeout)),
    )

    result = _dispatch(kbd, kbc)

    assert {entry[0] for entry in result.spawned} == {first_id, second_id}
    assert calls == [("test-model", 8)]


def test_probe_uses_profile_default_route_without_card_override(
    kanban_home, monkeypatch,
):
    _kb, _kbc, _kbd, liveness = kanban_home
    from agent import auxiliary_client
    from hermes_cli import runtime_provider
    from hermes_constants import get_hermes_home

    (get_hermes_home() / "config.yaml").write_text(
        "model:\n  provider: profile-provider\n  default: profile-model\n",
        encoding="utf-8",
    )
    resolved = []

    def _resolve_runtime(**kwargs):
        resolved.append(kwargs)
        return {
            "provider": "profile-provider",
            "base_url": "https://provider.invalid/v1",
            "api_key": "test-placeholder",
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", _resolve_runtime)
    monkeypatch.setattr(
        auxiliary_client,
        "resolve_provider_client",
        lambda provider, **kwargs: (
            SimpleNamespace(),
            kwargs["model"] if provider == "profile-provider" else None,
        ),
    )
    requests = []
    monkeypatch.setattr(
        liveness,
        "_single_request",
        lambda _client, model, timeout: requests.append((model, timeout)),
    )

    outcome = liveness.probe_route("default", None, None)

    assert outcome == liveness.ProbeOutcome(
        True, "profile-provider", "profile-model", "",
    )
    assert resolved == [{"requested": None, "target_model": None}]
    assert requests == [("profile-model", 8)]


def test_identical_dead_episode_does_not_repeat_comment_or_event(
    kanban_home, monkeypatch,
):
    kb, kbc, kbd, liveness = kanban_home
    task_id = _create_task(kb, kbc)
    outcome = liveness.ProbeOutcome(
        False, "test-provider", "test-model", "429 quota exhausted",
    )
    _patch_gate(monkeypatch, liveness, lambda *_args: outcome)

    _dispatch(kbd, kbc)
    _dispatch(kbd, kbc)

    with kbc.connect_closing() as conn:
        comments = kb.list_comments(conn, task_id)
        events = [event for event in kb.list_events(conn, task_id) if event.kind == "liveness_blocked"]
    assert len(comments) == 1
    assert len(events) == 1


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("request timed out after 8s"), "request timed out after 8s"),
        (ConnectionError("connection refused"), "connection refused"),
        (RuntimeError("model abc not supported"), "model abc not supported"),
        (RuntimeError("internal probe bug"), None),
    ],
)
def test_classify_probe_error(exc, expected):
    from hermes_cli.kanban_dispatch_liveness import classify_probe_error

    assert classify_probe_error(exc) == expected


def test_classify_probe_error_includes_status_and_body_message():
    from hermes_cli.kanban_dispatch_liveness import classify_probe_error

    exc = RuntimeError("request failed")
    exc.status_code = 401
    exc.body = {"error": {"message": "invalid api key"}}

    assert classify_probe_error(exc) == "401 request failed: invalid api key"


def test_classify_missing_credentials_as_dead_route():
    from hermes_cli.auth import AuthError
    from hermes_cli.kanban_dispatch_liveness import classify_probe_error

    exc = AuthError(
        "No usable credentials found",
        provider="test-provider",
        code="missing_api_key",
    )

    assert classify_probe_error(exc) == "No usable credentials found"
