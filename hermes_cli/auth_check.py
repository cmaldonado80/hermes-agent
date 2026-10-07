"""Read-oriented credential-pool diagnostics across Hermes profiles."""

from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from agent.credential_persistence import fingerprint_secret_value
from agent.credential_pool import (
    AUTH_TYPE_API_KEY,
    STATUS_DEAD,
    STATUS_EXHAUSTED,
    PooledCredential,
    _env_key_var_candidates,
    load_pool,
)
from agent.secret_scope import (
    build_profile_secret_scope,
    reset_secret_scope,
    set_secret_scope,
)
from hermes_cli.auth import PROVIDER_REGISTRY, read_credential_pool
from hermes_constants import (
    get_default_hermes_root,
    reset_hermes_home_override,
    set_hermes_home_override,
)


@dataclass(frozen=True)
class DeFactoCredential:
    kind: str
    var_or_id: str | None
    label: str | None
    source: str | None
    fingerprint: str | None
    provenance: str | None


@dataclass(frozen=True)
class EntryReport:
    id: str
    label: str
    source: str
    auth_type: str
    last_status: str
    fingerprint: str | None
    env_missing: bool


@dataclass(frozen=True)
class AuthCheckReport:
    profile: str
    provider: str
    de_facto: DeFactoCredential
    status: str
    status_detail: str | None
    entries: tuple[EntryReport, ...]


def _base_env_vars(provider: str) -> list[str]:
    if provider == "openrouter":
        return ["OPENROUTER_API_KEY"]
    if provider == "anthropic":
        return ["ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]
    pconfig = PROVIDER_REGISTRY.get(provider)
    return list(pconfig.api_key_env_vars) if pconfig is not None else []


def _model_level_env(provider: str) -> str:
    from hermes_cli.config import load_config_readonly

    try:
        config = load_config_readonly() or {}
    except Exception:
        return ""
    model = config.get("model")
    if not isinstance(model, dict):
        return ""
    if str(model.get("provider") or "").strip().lower() != provider:
        return ""
    return str(model.get("key_env") or model.get("api_key_env") or "").strip()


def _entry_sources(provider: str, raw_entries: list[dict]) -> list[PooledCredential]:
    return [
        PooledCredential.from_dict(
            provider,
            {
                "id": str(entry.get("id") or "source-only"),
                "label": str(entry.get("label") or "source-only"),
                "priority": index,
                "source": str(entry.get("source") or ""),
                "auth_type": AUTH_TYPE_API_KEY,
                "access_token": "",
            },
        )
        for index, entry in enumerate(raw_entries)
    ]


def _env_candidates(provider: str, raw_entries: list[dict]) -> list[str]:
    base_vars = _base_env_vars(provider)
    model_var = _model_level_env(provider)
    if model_var:
        base_vars = [model_var, *base_vars]
    candidates = _env_key_var_candidates(base_vars, _entry_sources(provider, raw_entries))
    return list(dict.fromkeys(candidates))


def _resolved_env(
    provider: str,
    raw_entries: list[dict],
) -> tuple[str, str, str] | None:
    from hermes_cli.auth import _usable_declared_secret
    from hermes_cli.config import get_env_value_prefer_dotenv, load_env

    dotenv = load_env()
    for env_var in _env_candidates(provider, raw_entries):
        value = _usable_declared_secret(
            provider,
            get_env_value_prefer_dotenv(env_var),
            env_var,
        )
        if value:
            provenance = ".env" if env_var in dotenv else "shell"
            return env_var, value, provenance
    return None


def _is_env_first(provider: str) -> bool:
    pconfig = PROVIDER_REGISTRY.get(provider)
    return provider == "openrouter" or (
        pconfig is not None and pconfig.auth_type == AUTH_TYPE_API_KEY
    )


def _dict_rows(value: object) -> list[dict]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _has_resolved_env(provider: str, raw_entries: list[dict]) -> bool:
    try:
        return _resolved_env(provider, raw_entries) is not None
    except Exception:
        return False


def _providers_for_profile(raw_pool: dict[str, object]) -> list[str]:
    providers = {
        str(provider).strip().lower()
        for provider, entries in raw_pool.items()
        if isinstance(entries, list) and entries
    }
    for provider, pconfig in PROVIDER_REGISTRY.items():
        if pconfig.auth_type != AUTH_TYPE_API_KEY:
            continue
        rows = _dict_rows(raw_pool.get(provider))
        if _has_resolved_env(provider, rows):
            providers.add(provider)
    rows = _dict_rows(raw_pool.get("openrouter"))
    if rows or _has_resolved_env("openrouter", rows):
        providers.add("openrouter")
    return sorted(providers)


def _entry_fingerprint(entry: PooledCredential) -> str | None:
    return fingerprint_secret_value(entry.runtime_api_key or entry.access_token)


def _entry_status(entry: PooledCredential) -> str:
    status = entry.last_status or "ok"
    if status != STATUS_EXHAUSTED:
        return status
    from hermes_cli.auth_commands import _format_exhausted_status

    detail = _format_exhausted_status(entry).strip()
    return f"exhausted; {detail}" if detail else "exhausted"


def _entry_report(entry: PooledCredential) -> EntryReport:
    from hermes_cli.auth_commands import _display_source
    from hermes_cli.config import get_env_value_prefer_dotenv

    env_var = entry.source.split(":", 1)[1].strip() if entry.source.startswith("env:") else ""
    env_missing = bool(env_var and not get_env_value_prefer_dotenv(env_var))
    return EntryReport(
        id=str(entry.id),
        label=str(entry.label),
        source=_display_source(str(entry.source)),
        auth_type=str(entry.auth_type),
        last_status=_entry_status(entry),
        fingerprint=_entry_fingerprint(entry),
        env_missing=env_missing,
    )


def _retry_detail(next_available_at: float | None) -> str:
    if next_available_at is None:
        return "no retry time available"
    remaining = max(0, int(math.ceil(next_available_at - time.time())))
    if remaining <= 0:
        return "ready to retry"
    minutes = max(1, int(math.ceil(remaining / 60)))
    retry_at = datetime.fromtimestamp(next_available_at, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    return f"{minutes}m left (retry at {retry_at})"


def _raw_secret_values(raw_entries: list[dict]) -> list[str]:
    secret_fields = ("access_token", "refresh_token", "agent_key", "api_key", "token")
    return [
        value
        for entry in raw_entries
        if isinstance(entry, dict)
        for field in secret_fields
        for value in (entry.get(field),)
        if isinstance(value, str) and value
    ]


def _safe_error_detail(exc: Exception, secret_values: Sequence[str]) -> str:
    from agent.redact import redact_sensitive_text

    detail = str(exc)
    ordered_values = sorted(set(secret_values), key=lambda value: len(value), reverse=True)
    for value in ordered_values:
        detail = detail.replace(value, "***")
    return redact_sensitive_text(
        detail,
        force=True,
        redact_url_credentials=True,
    )[:200]


def _error_entry_reports(provider: str, raw_entries: list[dict]) -> tuple[EntryReport, ...]:
    reports: list[EntryReport] = []
    for payload in raw_entries:
        if not isinstance(payload, dict):
            continue
        try:
            reports.append(_entry_report(PooledCredential.from_dict(provider, payload)))
        except Exception:
            continue
    return tuple(reports)


def _collect_provider(profile: str, provider: str, raw_entries: list[dict]) -> AuthCheckReport:
    known_secrets = _raw_secret_values(raw_entries)
    try:
        resolved_env = _resolved_env(provider, raw_entries)
        if resolved_env is not None:
            known_secrets.append(resolved_env[1])

        pool = load_pool(provider)
        entries = pool.entries()
        entry_reports = tuple(_entry_report(entry) for entry in entries)

        if _is_env_first(provider) and resolved_env is not None:
            env_var, value, provenance = resolved_env
            return AuthCheckReport(
                profile=profile,
                provider=provider,
                de_facto=DeFactoCredential(
                    kind="env",
                    var_or_id=env_var,
                    label=env_var,
                    source=f"env:{env_var}",
                    fingerprint=fingerprint_secret_value(value),
                    provenance=provenance,
                ),
                status="ok",
                status_detail=None,
                entries=entry_reports,
            )

        entry = pool.peek()
        if entry is not None:
            from hermes_cli.auth_commands import _display_source, _format_exhausted_status

            status = entry.last_status or "ok"
            status_detail = (
                _format_exhausted_status(entry).strip()
                if status == STATUS_EXHAUSTED
                else None
            )
            return AuthCheckReport(
                profile=profile,
                provider=provider,
                de_facto=DeFactoCredential(
                    kind="pool",
                    var_or_id=str(entry.id),
                    label=str(entry.label),
                    source=_display_source(str(entry.source)),
                    fingerprint=_entry_fingerprint(entry),
                    provenance=None,
                ),
                status=status if status in {"ok", STATUS_EXHAUSTED, STATUS_DEAD} else str(status),
                status_detail=status_detail,
                entries=entry_reports,
            )

        missing_env = next(
            (
                row.source.split(":", 1)[1]
                for row, report in zip(entries, entry_reports)
                if report.env_missing and row.source.startswith("env:")
            ),
            None,
        )
        if missing_env:
            return AuthCheckReport(
                profile=profile,
                provider=provider,
                de_facto=DeFactoCredential(
                    kind="env",
                    var_or_id=missing_env,
                    label=missing_env,
                    source=f"env:{missing_env}",
                    fingerprint=None,
                    provenance=None,
                ),
                status="env-missing",
                status_detail=f"env:{missing_env} is missing in this profile",
                entries=entry_reports,
            )

        return AuthCheckReport(
            profile=profile,
            provider=provider,
            de_facto=DeFactoCredential(
                kind="none",
                var_or_id=None,
                label=None,
                source=None,
                fingerprint=None,
                provenance=None,
            ),
            status="exhausted",
            status_detail=_retry_detail(pool.next_available_at()) if entries else "no credentials",
            entries=entry_reports,
        )
    except Exception as exc:
        return AuthCheckReport(
            profile=profile,
            provider=provider,
            de_facto=DeFactoCredential(
                kind="error",
                var_or_id=None,
                label=None,
                source=None,
                fingerprint=None,
                provenance=None,
            ),
            status="error",
            status_detail=_safe_error_detail(exc, known_secrets),
            entries=_error_entry_reports(provider, raw_entries),
        )


@contextmanager
def _profile_scope(home: Path) -> Iterator[None]:
    home_token = set_hermes_home_override(str(home))
    secret_token = None
    try:
        secret_token = set_secret_scope(build_profile_secret_scope(home))
        yield
    finally:
        if secret_token is not None:
            reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def collect_auth_check(
    profiles: Sequence[tuple[str, Path]],
    *,
    provider: str | None = None,
) -> list[AuthCheckReport]:
    """Collect one report per selected profile/provider without exposing secret values."""
    reports: list[AuthCheckReport] = []
    for profile, home in profiles:
        with _profile_scope(home):
            raw_pool = read_credential_pool(None)
            providers = _providers_for_profile(raw_pool)
            if provider is not None:
                providers = [candidate for candidate in providers if candidate == provider]
            for provider_id in providers:
                rows = _dict_rows(raw_pool.get(provider_id))
                reports.append(_collect_provider(profile, provider_id, rows))
    return reports


def _de_facto_text(credential: DeFactoCredential) -> str:
    fingerprint = credential.fingerprint or "-"
    if credential.kind == "env" and credential.provenance:
        return f"env:{credential.var_or_id} ({credential.provenance}; {fingerprint})"
    if credential.kind == "env":
        return f"env:{credential.var_or_id} (missing in this profile)"
    if credential.kind == "pool":
        return (
            f"{credential.label} [{credential.var_or_id}] "
            f"({credential.source}; {fingerprint})"
        )
    if credential.kind == "error":
        return "(error)"
    return "(none available)"


def render_reports(reports: Sequence[AuthCheckReport]) -> str:
    """Render adaptive ASCII rows followed by secret-safe per-entry details."""
    headers = ("PROFILE", "PROVIDER", "DE FACTO CREDENTIAL", "STATUS")
    rows = [
        (report.profile, report.provider, _de_facto_text(report.de_facto), report.status)
        for report in reports
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(headers[index])
        for index in range(len(headers))
    ]

    def table_row(values: Sequence[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    lines = [table_row(headers), "-+-".join("-" * width for width in widths)]
    for report, row in zip(reports, rows):
        lines.append(table_row(row))
        if report.status_detail:
            lines.append(f"  status: {report.status_detail}")
        for entry in report.entries:
            marker = " env-missing" if entry.env_missing else ""
            lines.append(
                "  - "
                f"label={entry.label} id={entry.id} source={entry.source} "
                f"auth_type={entry.auth_type} last_status={entry.last_status} "
                f"fingerprint={entry.fingerprint or '-'}{marker}"
            )
    return "\n".join(lines)


def render_reports_json(reports: Sequence[AuthCheckReport]) -> str:
    """Render the report schema deterministically without credential values."""
    return json.dumps([asdict(report) for report in reports], indent=2, sort_keys=True)


def _requested_profile_names(raw_profiles: Sequence[str] | None) -> list[str] | None:
    if not raw_profiles:
        return None
    names = [
        name.strip().lower()
        for raw in raw_profiles
        for name in str(raw).split(",")
        if name.strip()
    ]
    return list(dict.fromkeys(names))


def _resolve_profiles(raw_profiles: Sequence[str] | None) -> list[tuple[str, Path]]:
    from hermes_cli.profiles import _iter_named_profile_dirs

    default_home = get_default_hermes_root()
    available = [("default", default_home), *[(path.name, path) for path in _iter_named_profile_dirs()]]
    requested = _requested_profile_names(raw_profiles)
    if requested is None:
        return available
    homes = dict(available)
    unknown = [name for name in requested if name not in homes]
    if unknown:
        choices = ", ".join(name for name, _home in available)
        raise SystemExit(
            f"Unknown profile(s): {', '.join(unknown)}. Available profiles: {choices}."
        )
    requested_set = set(requested)
    return [(name, home) for name, home in available if name in requested_set]


def auth_check_command(args) -> None:
    """Run ``hermes auth check`` for the selected live profiles and providers."""
    from hermes_cli.auth_commands import _normalize_provider

    profiles = _resolve_profiles(getattr(args, "profile", None))
    raw_provider = str(getattr(args, "provider", "") or "").strip()
    provider = _normalize_provider(raw_provider) if raw_provider else None
    reports = collect_auth_check(profiles, provider=provider)
    output = render_reports_json(reports) if getattr(args, "json", False) else render_reports(reports)
    print(output)
