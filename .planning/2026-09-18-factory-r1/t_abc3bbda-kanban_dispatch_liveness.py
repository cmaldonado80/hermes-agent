"""Dispatch-time liveness probe for a Kanban card's pinned model route."""

from __future__ import annotations

import contextvars
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


DEFAULT_TIMEOUT_SECONDS = 8
DEFAULT_ALIVE_TTL_SECONDS = 300
DEFAULT_DEAD_TTL_SECONDS = 60


@dataclass(frozen=True)
class ProbeOutcome:
    alive: bool
    provider: str
    model: str
    error: str


@dataclass(frozen=True)
class _LivenessSettings:
    timeout_seconds: int
    alive_ttl_seconds: int
    dead_ttl_seconds: int


class _ProbeRequestTimeout(TimeoutError):
    pass


_probe_cache: dict[tuple[str, str, str], tuple[float, ProbeOutcome]] = {}
_probe_cache_lock = threading.Lock()


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _kanban_config() -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly

    section = (load_config_readonly() or {}).get("kanban")
    return section if isinstance(section, dict) else {}


def liveness_gate_enabled() -> bool:
    """Whether dispatch probes are enabled; config failures fail open to enabled."""
    try:
        return bool(_kanban_config().get("dispatch_liveness_gate", True))
    except Exception:
        return True


def _settings() -> _LivenessSettings:
    try:
        config = _kanban_config()
    except Exception:
        config = {}
    return _LivenessSettings(
        timeout_seconds=_positive_int(
            config.get("dispatch_liveness_timeout_seconds"), DEFAULT_TIMEOUT_SECONDS,
        ),
        alive_ttl_seconds=_positive_int(
            config.get("dispatch_liveness_alive_ttl_seconds"), DEFAULT_ALIVE_TTL_SECONDS,
        ),
        dead_ttl_seconds=_positive_int(
            config.get("dispatch_liveness_dead_ttl_seconds"), DEFAULT_DEAD_TTL_SECONDS,
        ),
    )


def dead_ttl_seconds() -> int:
    """Configured dead-result TTL, shared with dispatcher comment deduplication."""
    return _settings().dead_ttl_seconds


def _configured_model(config: dict[str, Any]) -> str:
    raw = config.get("model")
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, dict):
        return ""
    selected = raw.get("default") or raw.get("model") or raw.get("name") or ""
    if isinstance(selected, dict):
        selected = selected.get("model") or selected.get("name") or selected.get("id") or ""
    return str(selected).strip()


def _configured_provider(config: dict[str, Any]) -> str:
    raw = config.get("model")
    if not isinstance(raw, dict):
        return ""
    selected = raw.get("provider") or ""
    if not selected and isinstance(raw.get("default"), dict):
        selected = raw["default"].get("provider") or ""
    return str(selected).strip()


def _status_code(exc: BaseException) -> Optional[int]:
    raw = getattr(exc, "status_code", None)
    if raw is None:
        raw = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _error_text(exc: BaseException) -> str:
    message = str(exc).strip() or type(exc).__name__
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        nested = body.get("error")
        detail = nested.get("message") if isinstance(nested, dict) else body.get("message")
        if detail and str(detail) not in message:
            message = f"{message}: {detail}"
    status = _status_code(exc)
    if status is not None and str(status) not in message:
        message = f"{status} {message}"
    try:
        from agent.redact import redact_for_egress

        return redact_for_egress(message)
    except Exception:
        return type(exc).__name__


def classify_probe_error(exc: BaseException) -> Optional[str]:
    """Return safe dead-route evidence, or ``None`` for an unexpected failure."""
    status = _status_code(exc)
    message = str(exc).lower()
    error_type = type(exc).__name__.lower()
    if status in {401, 402, 403, 404, 429}:
        return _error_text(exc)
    if "model" in message and "not" in message and (
        "found" in message or "support" in message
    ):
        return _error_text(exc)
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return _error_text(exc)
    if any(token in error_type for token in (
        "timeout", "connection", "connecterror", "networkerror", "protocolerror",
        "ratelimit", "sslerror", "dnserror", "authenticationerror",
        "permissiondenied", "unauthorized", "forbidden",
    )):
        return _error_text(exc)
    if any(token in message for token in (
        "timed out", "connection refused", "connection reset", "connection error",
        "name or service not known", "no route to host", "network is unreachable",
        "rate limit", "rate_limit", "quota exceeded", "quota exhausted",
        "payment required", "insufficient credits",
        "server disconnected", "peer closed connection", "unexpected eof",
    )):
        return _error_text(exc)
    try:
        from hermes_cli.auth import AuthError

        if isinstance(exc, AuthError):
            return _error_text(exc)
    except Exception:
        pass
    return None


def _cache_get(key: tuple[str, str, str]) -> Optional[ProbeOutcome]:
    now = time.monotonic()
    with _probe_cache_lock:
        entry = _probe_cache.get(key)
        if entry is None:
            return None
        expires_at, outcome = entry
        if expires_at > now:
            return outcome
        _probe_cache.pop(key, None)
    return None


def _cache_put(
    key: tuple[str, str, str], outcome: ProbeOutcome, settings: _LivenessSettings,
) -> ProbeOutcome:
    ttl = settings.alive_ttl_seconds if outcome.alive else settings.dead_ttl_seconds
    with _probe_cache_lock:
        _probe_cache[key] = (time.monotonic() + ttl, outcome)
    return outcome


def _single_request(client: Any, model: str, timeout_seconds: int) -> None:
    """Issue one request and bound adapters that do not forward per-call timeouts."""
    results: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    context = contextvars.copy_context()

    def _request() -> None:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                timeout=timeout_seconds,
            )
            results.put((True, response))
        except Exception as exc:
            results.put((False, exc))

    thread = threading.Thread(
        target=lambda: context.run(_request),
        name="kanban-route-liveness",
        daemon=True,
    )
    thread.start()
    try:
        ok, value = results.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise _ProbeRequestTimeout(
            f"request timed out after {timeout_seconds}s"
        ) from exc
    if not ok:
        raise value


def _profile_scope(profile: str) -> tuple[str, Optional[str]]:
    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    profile_name = normalize_profile_name(profile)
    try:
        profile_home = resolve_profile_env(profile_name)
    except FileNotFoundError:
        profile_home = None
    return profile_name, profile_home


def probe_route(
    profile: str,
    model_override: Optional[str],
    provider_override: Optional[str],
) -> ProbeOutcome:
    """Probe exactly the route a dispatched worker would use.

    Recognized route failures are cached and returned dead. Any unexpected
    probe-infrastructure failure is recorded but fails open.
    """
    settings = _settings()
    provider_label = (provider_override or "auto").strip() or "auto"
    model_label = (model_override or "").strip()
    cache_key: Optional[tuple[str, str, str]] = None
    home_token = secret_token = None
    try:
        profile_name, profile_home = _profile_scope(profile)
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        if profile_home:
            home_token = set_hermes_home_override(profile_home)
            from agent.secret_scope import (
                build_profile_secret_scope,
                reset_secret_scope,
                set_secret_scope,
            )

            secret_token = set_secret_scope(build_profile_secret_scope(Path(profile_home)))
        from hermes_cli.config import load_config_readonly
        from hermes_cli.runtime_provider import resolve_runtime_provider

        config = load_config_readonly() or {}
        model_label = model_label or _configured_model(config)
        if not provider_override:
            provider_label = _configured_provider(config) or provider_label
        runtime = resolve_runtime_provider(
            requested=provider_override or None,
            target_model=model_override or None,
        )
        provider_label = str(runtime.get("provider") or provider_label)
        cache_key = (profile_name, provider_label, model_label)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        from agent.auxiliary_client import resolve_provider_client

        client, resolved_model = resolve_provider_client(
            provider_label,
            model=model_label,
            explicit_base_url=str(runtime.get("base_url") or ""),
            explicit_api_key=runtime.get("api_key") or None,
            api_mode=str(runtime.get("api_mode") or ""),
            main_runtime={**runtime, "model": model_label},
        )
        model_label = str(resolved_model or model_label or "(default)")
        if client is None or not resolved_model:
            raise RuntimeError(
                "provider client unavailable (credentials or model route not configured)"
            )
        _single_request(client, model_label, settings.timeout_seconds)
        return _cache_put(
            cache_key,
            ProbeOutcome(True, provider_label, model_label, ""),
            settings,
        )
    except Exception as exc:
        error = classify_probe_error(exc)
        outcome = ProbeOutcome(
            error is None,
            provider_label,
            model_label or "(default)",
            error or _error_text(exc),
        )
        if cache_key is None:
            cache_key = (str(profile), provider_label, model_label)
        return _cache_put(cache_key, outcome, settings)
    finally:
        if secret_token is not None:
            reset_secret_scope(secret_token)
        if home_token is not None:
            reset_hermes_home_override(home_token)
