"""Behavior contracts for the per-profile credential-pool doctor."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agent.credential_persistence import fingerprint_secret_value
from hermes_cli.auth_check import (
    auth_check_command,
    collect_auth_check,
    render_reports,
    render_reports_json,
)


ZAI_SECRET_A = "zai-fixture-key-a"
OPENROUTER_MANUAL_SECRET = "sk-or-manual-fixture-a"
ZAI_MANUAL_SECRET_B = "zai-manual-fixture-b"


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "ZAI_API_KEY",
        "GLM_API_KEY",
        "Z_AI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


def _pool_entry(
    *,
    entry_id: str,
    label: str,
    source: str,
    priority: int,
    access_token: str | None = None,
    **extra,
) -> dict:
    entry = {
        "id": entry_id,
        "label": label,
        "priority": priority,
        "source": source,
        "auth_type": "api_key",
        **extra,
    }
    if access_token is not None:
        entry["access_token"] = access_token
    return entry


def _write_profile(home: Path, auth_store: dict, *, dotenv: str = "") -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(auth_store, indent=2), encoding="utf-8")
    (home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"provider": "zai"}}),
        encoding="utf-8",
    )
    if dotenv:
        (home / ".env").write_text(dotenv, encoding="utf-8")


@pytest.fixture
def credential_profiles(profile_env):
    now = time.time()
    profile_a = profile_env / "profiles" / "a"
    profile_b = profile_env / "profiles" / "b"
    openrouter_entries = [
        _pool_entry(
            entry_id="or-manual",
            label="openrouter-backup",
            source="manual",
            priority=0,
            access_token=OPENROUTER_MANUAL_SECRET,
            last_status="exhausted",
            last_status_at=now,
            last_error_code=429,
            last_error_reset_at=now + 3600,
        ),
        _pool_entry(
            entry_id="or-env",
            label="OPENROUTER_API_KEY",
            source="env:OPENROUTER_API_KEY",
            priority=1,
        ),
    ]
    _write_profile(
        profile_a,
        {"version": 1, "providers": {}, "credential_pool": {"openrouter": openrouter_entries}},
        dotenv=f"ZAI_API_KEY={ZAI_SECRET_A}\n",
    )
    _write_profile(
        profile_b,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "zai": [
                    _pool_entry(
                        entry_id="zai-manual",
                        label="zai-profile-b",
                        source="manual",
                        priority=0,
                        access_token=ZAI_MANUAL_SECRET_B,
                    )
                ]
            },
        },
    )
    return profile_a, profile_b


def _report(profile: str, provider: str, home: Path):
    reports = collect_auth_check([(profile, home)], provider=provider)
    assert len(reports) == 1
    return reports[0]


def test_de_facto_env_precedence_and_secret_safety(credential_profiles):
    profile_a, _profile_b = credential_profiles

    report = _report("a", "zai", profile_a)
    table = render_reports([report])
    json_output = render_reports_json([report])

    assert report.de_facto.kind == "env"
    assert report.de_facto.var_or_id == "ZAI_API_KEY"
    assert report.de_facto.fingerprint == fingerprint_secret_value(ZAI_SECRET_A)
    assert report.status == "ok"
    assert ZAI_SECRET_A not in table
    assert ZAI_SECRET_A not in json_output


def test_incident_case_reports_missing_env_and_exhausted_manual_entry(credential_profiles):
    profile_a, _profile_b = credential_profiles

    report = _report("a", "openrouter", profile_a)
    output = render_reports([report])

    assert report.status == "env-missing"
    assert report.de_facto.var_or_id == "OPENROUTER_API_KEY"
    assert any(entry.id == "or-env" and entry.env_missing for entry in report.entries)
    assert "openrouter-backup" in output
    assert "exhausted" in output
    assert "left" in output
    assert OPENROUTER_MANUAL_SECRET not in output


def test_malformed_env_does_not_shadow_runtime_pool_choice(profile_env):
    profile = profile_env / "profiles" / "malformed"
    _write_profile(
        profile,
        {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "openrouter": [
                    _pool_entry(
                        entry_id="or-valid",
                        label="valid-manual",
                        source="manual",
                        priority=0,
                        access_token="sk-or-v1-valid-fixture",
                    )
                ]
            },
        },
        dotenv="OPENROUTER_API_KEY=wrong-provider-fixture-key\n",
    )

    report = _report("malformed", "openrouter", profile)

    assert report.de_facto.kind == "pool"
    assert report.de_facto.var_or_id == "or-valid"
    assert report.status == "ok"


def test_profile_scope_is_restored_across_a_b_a(credential_profiles):
    profile_a, profile_b = credential_profiles

    first_a = _report("a", "zai", profile_a)
    report_b = _report("b", "zai", profile_b)
    second_a = _report("a", "zai", profile_a)

    assert first_a.de_facto.var_or_id == "ZAI_API_KEY"
    assert report_b.de_facto.kind == "pool"
    assert report_b.de_facto.var_or_id == "zai-manual"
    assert report_b.de_facto.label == "zai-profile-b"
    assert second_a.de_facto.var_or_id == "ZAI_API_KEY"
    assert second_a.de_facto.fingerprint == first_a.de_facto.fingerprint


def test_command_stdout_and_json_never_include_fixture_secrets(
    credential_profiles, capsys
):
    _profile_a, _profile_b = credential_profiles

    auth_check_command(SimpleNamespace(profile=["a,b"], provider=None, json=False))
    table = capsys.readouterr().out
    auth_check_command(SimpleNamespace(profile=["a,b"], provider=None, json=True))
    json_output = capsys.readouterr().out

    for secret in (ZAI_SECRET_A, OPENROUTER_MANUAL_SECRET, ZAI_MANUAL_SECRET_B):
        assert secret not in table
        assert secret not in json_output


def test_incident_check_is_byte_for_byte_read_only(credential_profiles):
    profile_a, _profile_b = credential_profiles
    auth_path = profile_a / "auth.json"
    before = auth_path.read_bytes()

    auth_check_command(SimpleNamespace(profile=["a"], provider="openrouter", json=False))

    assert auth_path.read_bytes() == before


def test_provider_filter_and_json_shape(credential_profiles, capsys):
    _profile_a, _profile_b = credential_profiles

    auth_check_command(SimpleNamespace(profile=["b"], provider="zai", json=True))
    payload = json.loads(capsys.readouterr().out)

    assert len(payload) == 1
    assert set(payload[0]) == {
        "profile",
        "provider",
        "de_facto",
        "status",
        "status_detail",
        "entries",
    }
    assert set(payload[0]["de_facto"]) == {
        "kind",
        "var_or_id",
        "label",
        "source",
        "fingerprint",
        "provenance",
    }
    assert set(payload[0]["entries"][0]) == {
        "id",
        "label",
        "source",
        "auth_type",
        "last_status",
        "fingerprint",
        "env_missing",
    }
    assert payload[0]["provider"] == "zai"
    assert payload[0]["status"] == "ok"


def test_unknown_profile_exits_with_available_profiles(credential_profiles):
    _profile_a, _profile_b = credential_profiles

    with pytest.raises(SystemExit) as exc_info:
        auth_check_command(SimpleNamespace(profile=["missing"], provider=None, json=False))

    message = str(exc_info.value)
    assert "Unknown profile(s): missing" in message
    assert "Available profiles: default, a, b" in message
