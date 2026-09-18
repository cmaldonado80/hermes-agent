"""Failure-injection coverage for the Anthropic refresh *commit* step.

Anthropic OAuth refresh tokens are single-use: the POST that returns a new
pair also invalidates the one that was sent.  The replacement therefore exists
only in memory until it reaches its authoritative on-disk store —
``~/.claude/.credentials.json`` for ``claude_code`` entries,
``~/.hermes/.anthropic_oauth.json`` for ``hermes_pkce`` ones.  Those singletons
are authoritative in the strict sense: ``_seed_from_singletons()`` re-reads
them on every ``load_pool()`` and writes what it finds over the pool row.

Before this coverage existed, both writers caught ``OSError``/``IOError``,
logged at debug level, and returned nothing, so no caller could tell a durable
commit from a failed one.  A refresh could therefore spend the only refresh
token, report success, and leave the consumed pre-rotation pair on disk to be
re-seeded — with the next refresh replaying a spent token and failing with
``invalid_grant`` / ``refresh_token_reused``.

Every test here forces the writer to fail and asserts the same invariant from
a different entry point: the rotation is never reported, marked, or persisted
as successful, and a subsequent ``load_pool()`` cannot bring the pre-refresh
pair back as a usable credential.

Companions: ``test_credential_pool_oauth_writethrough.py`` covers the
successful write-through, ``test_credential_pool_anthropic_refresh_race.py``
the contention between two refreshers.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from agent import anthropic_credentials as AA
from agent.anthropic_credentials import CredentialPersistError
from agent.credential_pool import (
    AUTH_TYPE_OAUTH,
    CREDENTIAL_PERSIST_FAILED_REASON,
    STATUS_DEAD,
    CredentialPool,
    PooledCredential,
    load_pool,
)

# Far enough in the past that every ``_entry_needs_refresh`` check fires.
_EXPIRED_MS = 1_000

# Synthetic, non-functional token material.
_STALE_ACCESS = "sk-ant-oat01-stale"
_STALE_REFRESH = "sk-ant-ort01-stale"
_ROTATED_ACCESS = "sk-ant-oat01-rotated"
_ROTATED_REFRESH = "sk-ant-ort01-rotated"


@pytest.fixture(autouse=True)
def _clean_spent_registry():
    """Isolate the process-global consumed-rotation registry between tests.

    Every failure injection here records the spent pair (see
    ``mark_rotation_consumed_uncommitted``), and those fingerprints would
    otherwise leak into unrelated tests that reuse the same token literals.
    """
    AA._SPENT_ROTATION_FINGERPRINTS.clear()
    yield
    AA._SPENT_ROTATION_FINGERPRINTS.clear()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Real on-disk HERMES_HOME so ``load_pool()`` re-reads what we persisted."""
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    (home / "auth.json").write_text(
        json.dumps({"version": 1, "providers": {}}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "hermes_cli.auth.is_provider_explicitly_configured", lambda pid: True
    )
    return home


@pytest.fixture
def claude_credentials(tmp_path, monkeypatch):
    """Point the ``claude_code`` singleton at a tmp file holding a stale pair."""
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
    # The Keychain reader shadows the file on macOS; keep the file the only
    # source so this suite behaves identically on every platform.
    monkeypatch.setattr(AA, "_read_claude_code_credentials_from_keychain", lambda: None)
    return cred_path


def _rotating_refresh(*_a, **_kw):
    """Stand-in for the token endpoint: always rotates the pair."""
    return {
        "access_token": _ROTATED_ACCESS,
        "refresh_token": _ROTATED_REFRESH,
        "expires_at_ms": int(time.time() * 1000) + 3_600_000,
    }


# Only the two authoritative Anthropic singletons are made unwritable.  The
# pool's own ``auth.json`` commit must keep working, otherwise the quarantine
# these tests assert on could never be persisted and the injection would be
# proving the wrong failure.
_SINGLETON_FILENAMES = frozenset({".credentials.json", ".anthropic_oauth.json"})


def _break_durable_write(monkeypatch):
    """Make the singleton atomic rename fail, i.e. the commit never lands."""
    real_replace = os.replace

    def _failing_replace(src, dst):
        if os.path.basename(os.fspath(dst)) in _SINGLETON_FILENAMES:
            raise OSError(13, "Permission denied")
        return real_replace(src, dst)

    monkeypatch.setattr(AA.os, "replace", _failing_replace)


def _entry(source: str) -> PooledCredential:
    return PooledCredential(
        provider="anthropic",
        id="anthropic-1",
        label="anthropic oauth",
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source=source,
        access_token=_STALE_ACCESS,
        refresh_token=_STALE_REFRESH,
        expires_at_ms=_EXPIRED_MS,
    )


def _read_claude_pair(cred_path):
    data = json.loads(cred_path.read_text(encoding="utf-8"))["claudeAiOauth"]
    return data["accessToken"], data["refreshToken"]


# ---------------------------------------------------------------------------
# The writers themselves
# ---------------------------------------------------------------------------


def test_claude_code_writer_raises_instead_of_swallowing(
    claude_credentials, monkeypatch
):
    """A failed durable write must be reported, not logged and dropped."""
    _break_durable_write(monkeypatch)

    with pytest.raises(CredentialPersistError):
        AA._write_claude_code_credentials(
            _ROTATED_ACCESS, _ROTATED_REFRESH, _EXPIRED_MS + 3_600_000
        )

    assert _read_claude_pair(claude_credentials) == (_STALE_ACCESS, _STALE_REFRESH), (
        "the failed commit must leave the previous file contents intact"
    )


def test_hermes_oauth_writer_raises_instead_of_swallowing(hermes_home, monkeypatch):
    oauth_file = hermes_home / ".anthropic_oauth.json"
    oauth_file.write_text(
        json.dumps(
            {
                "accessToken": _STALE_ACCESS,
                "refreshToken": _STALE_REFRESH,
                "expiresAt": _EXPIRED_MS,
            }
        ),
        encoding="utf-8",
    )
    _break_durable_write(monkeypatch)

    with pytest.raises(CredentialPersistError):
        AA._write_hermes_oauth_credentials(
            _ROTATED_ACCESS, _ROTATED_REFRESH, _EXPIRED_MS + 3_600_000
        )

    on_disk = json.loads(oauth_file.read_text(encoding="utf-8"))
    assert on_disk["refreshToken"] == _STALE_REFRESH


def test_failed_write_leaves_no_temp_file_behind(claude_credentials, monkeypatch):
    """The 0600 temp file must not survive a failed commit."""
    _break_durable_write(monkeypatch)

    with pytest.raises(CredentialPersistError):
        AA._write_claude_code_credentials(_ROTATED_ACCESS, _ROTATED_REFRESH, 0)

    leftovers = [
        p.name for p in claude_credentials.parent.iterdir() if ".tmp." in p.name
    ]
    assert leftovers == [], f"temp credential files left on disk: {leftovers}"


# ---------------------------------------------------------------------------
# Direct resolver path (resolve_anthropic_token -> _refresh_oauth_token)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pool path: claude_code
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pool path: hermes_pkce
# ---------------------------------------------------------------------------


def test_pool_hermes_pkce_fails_closed_and_reload_cannot_resurrect(
    hermes_home, monkeypatch
):
    oauth_file = hermes_home / ".anthropic_oauth.json"
    oauth_file.write_text(
        json.dumps(
            {
                "accessToken": _STALE_ACCESS,
                "refreshToken": _STALE_REFRESH,
                "expiresAt": _EXPIRED_MS,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(AA, "refresh_anthropic_oauth_pure", _rotating_refresh)
    monkeypatch.setattr(AA, "read_claude_code_credentials", lambda: None)
    _break_durable_write(monkeypatch)

    entry = _entry("hermes_pkce")
    pool = CredentialPool("anthropic", [entry])

    assert pool._refresh_entry(entry, force=True) is None

    quarantined = pool.entries()[0]
    assert quarantined.last_status == STATUS_DEAD
    assert quarantined.last_error_reason == CREDENTIAL_PERSIST_FAILED_REASON
    assert quarantined.refresh_token == _STALE_REFRESH

    on_disk = json.loads(oauth_file.read_text(encoding="utf-8"))
    assert on_disk["refreshToken"] == _STALE_REFRESH

    reloaded = [
        e for e in load_pool("anthropic").entries() if e.source == "hermes_pkce"
    ]
    assert reloaded
    assert reloaded[0].refresh_token == _STALE_REFRESH
    assert reloaded[0].last_status == STATUS_DEAD


# ---------------------------------------------------------------------------
# Pool path: the sync-and-retry-once recovery branch
# ---------------------------------------------------------------------------


