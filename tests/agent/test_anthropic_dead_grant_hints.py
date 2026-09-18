"""Hermes credential hints and the Anthropic token-endpoint error shape (#113023).

A dead Hermes login must be repaired with ``hermes auth add <provider>``; hints that send the user to
an external CLI's login command do not touch Hermes' own credentials. The token endpoint's
``invalid_grant`` body is surfaced as a structured, classifiable error so the pool can quarantine
instead of benching the dead grant as transient.
"""
from __future__ import annotations

import io
import logging
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent import anthropic_credentials as ac


def test_dead_grant_is_classified_and_not_replayed_at_other_endpoints(monkeypatch):
    calls: list = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(b'{"error":"invalid_grant","error_description":"revoked"}'))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ac, "_OAUTH_TOKEN_URLS", ["https://a.example/oauth/token", "https://b.example/oauth/token"])

    with pytest.raises(ac.AnthropicOAuthError) as info:
        ac.refresh_anthropic_oauth_pure("sk-ant-ort-dead")

    assert ac.is_terminal_anthropic_refresh_error(info.value)
    assert info.value.code == "invalid_grant" and "revoked" in str(info.value)
    assert calls == ["https://a.example/oauth/token"]  # a dead grant is not replayed at the fallback endpoint
    assert not ac.is_terminal_anthropic_refresh_error(TimeoutError("timed out"))


def test_claude_code_refresher_never_spends_the_borrowed_grant(monkeypatch, caplog):
    """Claude Code owns its grant; the refresher must neither POST it nor warn about
    endpoint verdicts it never asked for. It only adopts a token Claude Code already
    refreshed, and a dead grant is reported by the pool's own quarantine path."""
    monkeypatch.setattr(ac, "read_claude_code_credentials", lambda: {"accessToken": "old", "refreshToken": "rt", "expiresAt": 1})

    posts: list = []

    def must_not_post(refresh_token, *, use_json=False):
        posts.append(refresh_token)
        raise AssertionError("the borrowed Claude Code grant must never be POSTed")

    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure", must_not_post)
    with caplog.at_level(logging.DEBUG, logger=ac.logger.name):
        assert ac._refresh_oauth_token({"accessToken": "old", "refreshToken": "rt"}) is None
    assert posts == []
    assert not any(r.levelno == logging.WARNING for r in caplog.records)
    assert not any("claude setup-token" in r.getMessage() for r in caplog.records)


def test_claude_code_refresher_adopts_only_a_real_future_rotation(monkeypatch, caplog):
    """A DIFFERENT token with a real future expiry is adopted; a re-read that still shows
    the same expired token (Claude Code has not refreshed yet) is left for Claude Code's
    next run — never spent by Hermes."""
    monkeypatch.setattr(ac, "_DEAD_REFRESH_TOKEN_FINGERPRINTS", set())
    monkeypatch.setattr(ac, "refresh_anthropic_oauth_pure",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not POST")))
    creds = {"accessToken": "old", "refreshToken": "rt-dead", "expiresAt": 1}
    monkeypatch.setattr(ac, "read_claude_code_credentials", lambda: creds)
    with caplog.at_level(logging.DEBUG, logger=ac.logger.name):
        assert ac._refresh_oauth_token(creds) is None  # same expired token: nothing to adopt
    rotated = {"accessToken": "new", "refreshToken": "rt-new", "expiresAt": 4102444800000}
    monkeypatch.setattr(ac, "read_claude_code_credentials", lambda: rotated)
    assert ac._refresh_oauth_token(creds) == "new"  # Claude Code rotated: adopt
    # A re-read that is valid but NOT a different token (managed key / unknown expiry) is not adopted.
    same_but_valid = {"accessToken": "old", "expiresAt": 4102444800000}
    monkeypatch.setattr(ac, "read_claude_code_credentials", lambda: same_but_valid)
    assert ac._refresh_oauth_token(creds) is None


def test_claude_code_credentials_path_honours_claude_config_dir(monkeypatch, tmp_path):
    """The documented opt-out: CLAUDE_CONFIG_DIR relocates the borrowed file exactly as the Claude CLI does."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    assert ac.claude_code_credentials_path() == tmp_path / "cc" / ".credentials.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "  ")
    assert ac.claude_code_credentials_path() == Path.home() / ".claude" / ".credentials.json"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert ac.claude_code_credentials_path() == Path.home() / ".claude" / ".credentials.json"


def test_anthropic_401_troubleshooting_points_at_hermes_auth(capsys):
    from agent.turn_recovery import _print_anthropic_401_diagnostics

    class _Agent:
        log_prefix = ""

    _print_anthropic_401_diagnostics(_Agent(), "sk-ant-oat01-xxxxxxxxxxxx")
    out = capsys.readouterr().out
    assert "hermes auth add anthropic" in out and "hermes auth list anthropic" in out
    assert "/login" not in out


def test_no_anthropic_credentials_message_points_at_hermes_auth():
    from hermes_cli.runtime_provider import _NO_ANTHROPIC_CREDENTIALS_MSG

    assert "hermes auth add anthropic" in _NO_ANTHROPIC_CREDENTIALS_MSG
    assert "/login" not in _NO_ANTHROPIC_CREDENTIALS_MSG
