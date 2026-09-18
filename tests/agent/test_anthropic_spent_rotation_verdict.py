"""A rotation that was consumed but never committed must stay unusable.

``_refresh_oauth_token()`` already refuses to return an access token whose
refresh half was lost to a failed write.  That verdict was local: the caller
above it (``resolve_anthropic_token()``) simply continued to the next source,
and source 5 (``_resolve_anthropic_pool_token``) enumerates read-only
(``clear_expired=False, refresh=False``) over a pool that ``load_pool()`` has
just re-seeded from the *unchanged* singleton file.  So the very pair whose
refresh token the POST had already spent came back as a healthy token, and
``_refresh_provider_credentials("anthropic")`` reported the refresh as a
success and evicted its cached clients.

That is the same silent-transition failure the fail-closed path exists to
prevent, one layer up: no ``invalid_grant`` is raised until the *next* refresh,
by which point the provenance of the failure is gone.

These tests take the full resolver path, not just the writer: successful POST +
failed commit must make ``resolve_anthropic_token()`` return ``None`` (or a
genuinely independent credential), must make
``_refresh_provider_credentials("anthropic")`` return ``False`` when the spent
family is the only credential, and must keep the spent fingerprint out of every
lease.

Companion: ``test_anthropic_credential_persist_failure.py`` covers the writers
and the pool quarantine; this file covers what resolution does afterwards.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from agent import anthropic_credentials as AA
from agent.auxiliary_client import _refresh_provider_credentials
from agent.credential_pool import AUTH_TYPE_OAUTH, load_pool

_EXPIRED_MS = 1_000

_STALE_ACCESS = "sk-ant-oat01-spent-stale"
_STALE_REFRESH = "sk-ant-ort01-spent-stale"
_ROTATED_ACCESS = "sk-ant-oat01-spent-rotated"
_ROTATED_REFRESH = "sk-ant-ort01-spent-rotated"
_INDEPENDENT_ACCESS = "sk-ant-oat01-independent"
_INDEPENDENT_REFRESH = "sk-ant-ort01-independent"

_SINGLETON_FILENAMES = frozenset({".credentials.json", ".anthropic_oauth.json"})


@pytest.fixture(autouse=True)
def _clean_spent_registry():
    """The consumed-rotation registry is process-global; isolate each test."""
    AA._SPENT_ROTATION_FINGERPRINTS.clear()
    yield
    AA._SPENT_ROTATION_FINGERPRINTS.clear()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    (home / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {}}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "hermes_cli.auth.is_provider_explicitly_configured", lambda pid: True
    )
    return home


@pytest.fixture
def claude_credentials(tmp_path, monkeypatch):
    cred_path = tmp_path / "claude" / ".credentials.json"
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    cred_path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": _STALE_ACCESS,
                    "refreshToken": _STALE_REFRESH,
                    "expiresAt": _EXPIRED_MS,
                    "scopes": ["user:inference", "user:profile"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(AA, "claude_code_credentials_path", lambda: cred_path)
    monkeypatch.setattr(AA, "_read_claude_code_credentials_from_keychain", lambda: None)
    return cred_path


def _rotating_refresh(*_a, **_kw):
    """Stand-in for the token endpoint: the POST always succeeds and rotates."""
    return {
        "access_token": _ROTATED_ACCESS,
        "refresh_token": _ROTATED_REFRESH,
        "expires_at_ms": int(time.time() * 1000) + 3_600_000,
    }


def _break_durable_write(monkeypatch):
    """Only the authoritative singletons fail; auth.json must stay writable."""
    real_replace = os.replace

    def _failing_replace(src, dst):
        if os.path.basename(os.fspath(dst)) in _SINGLETON_FILENAMES:
            raise OSError(13, "Permission denied")
        return real_replace(src, dst)

    monkeypatch.setattr(AA.os, "replace", _failing_replace)


def _add_independent_pool_entry(home):
    """Persist a second, unrelated Anthropic OAuth credential in the pool."""
    path = home / "auth.json"
    store = json.loads(path.read_text(encoding="utf-8"))
    pool = store.setdefault("credential_pool", {})
    pool.setdefault("anthropic", []).append(
        {
            "id": "anthropic-independent",
            "label": "second subscription",
            "auth_type": AUTH_TYPE_OAUTH,
            "priority": 10,
            "source": "manual",
            "access_token": _INDEPENDENT_ACCESS,
            "refresh_token": _INDEPENDENT_REFRESH,
            "expires_at_ms": int(time.time() * 1000) + 3_600_000,
        }
    )
    path.write_text(json.dumps(store), encoding="utf-8")


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------


def test_registry_matches_only_the_recorded_secret():
    AA.mark_rotation_consumed_uncommitted(_STALE_REFRESH, "", None)

    assert AA.is_rotation_consumed_uncommitted(_STALE_REFRESH)
    assert not AA.is_rotation_consumed_uncommitted(_INDEPENDENT_REFRESH)
    assert not AA.is_rotation_consumed_uncommitted("")
    assert not AA.is_rotation_consumed_uncommitted(None)


def test_registry_stays_bounded():
    for i in range(AA._SPENT_ROTATION_MAX_TRACKED * 2):
        AA.mark_rotation_consumed_uncommitted(f"sk-ant-ort01-{i}")

    assert len(AA._SPENT_ROTATION_FINGERPRINTS) == AA._SPENT_ROTATION_MAX_TRACKED
    assert AA.is_rotation_consumed_uncommitted(
        f"sk-ant-ort01-{AA._SPENT_ROTATION_MAX_TRACKED * 2 - 1}"
    ), "the most recent rotation must survive eviction"


# ---------------------------------------------------------------------------
# Full resolution
# ---------------------------------------------------------------------------


def test_independent_pool_credential_stays_eligible(
    hermes_home, claude_credentials, monkeypatch
):
    """Failing closed is scoped to the spent family, not to Anthropic as a whole."""
    _add_independent_pool_entry(hermes_home)
    monkeypatch.setattr(AA, "refresh_anthropic_oauth_pure", _rotating_refresh)
    _break_durable_write(monkeypatch)

    resolved = AA.resolve_anthropic_token()

    assert resolved == _INDEPENDENT_ACCESS, (
        "an unrelated credential must still be selectable after the quarantine"
    )


# ---------------------------------------------------------------------------
# Cross-process durability of the verdict (sidecar registry)
# ---------------------------------------------------------------------------


def test_control_second_process_without_sidecar_still_resolves(
    hermes_home, claude_credentials, monkeypatch
):
    """Independent-credential control: no verdict, no quarantine.

    With no failed rotation recorded anywhere, the shared file's (valid)
    credential resolves normally in this process — proving the sidecar gate
    only fires on a recorded verdict, not on every read.
    """
    fresh = dict(
        json.loads(claude_credentials.read_text(encoding="utf-8"))
    )
    fresh["claudeAiOauth"]["expiresAt"] = int(time.time() * 1000) + 3_600_000
    claude_credentials.write_text(json.dumps(fresh), encoding="utf-8")

    assert not AA._spent_rotation_sidecar_path(claude_credentials).exists()
    creds = AA.read_claude_code_credentials()
    assert AA._resolve_claude_code_token_from_credentials(creds) == _STALE_ACCESS
