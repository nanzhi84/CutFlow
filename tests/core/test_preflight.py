"""Production startup preflight unit tests (issue #66)."""

from __future__ import annotations

import pytest

from packages.core.config import build_settings, validate_startup_settings


def _codes(issues: list[str]) -> set[str]:
    return {issue.split(":", 1)[0] for issue in issues}


def _arm_unsafe_production(monkeypatch) -> None:
    monkeypatch.setenv("CUTAGENT_ENV", "production")
    # The in-memory storage backend was removed (it is rejected at Settings
    # construction now), so the unsafe signal here is the sqlalchemy backend with
    # NO database_url — the preflight flags that under the "database_url" code.
    monkeypatch.setenv("CUTAGENT_STORAGE_BACKEND", "sqlalchemy")
    monkeypatch.delenv("CUTAGENT_DATABASE_URL", raising=False)
    monkeypatch.setenv("CUTAGENT_REGISTRATION_OPEN", "true")
    monkeypatch.setenv("CUTAGENT_REGISTRATION_CODE_SALT", "local-dev-registration-code-salt")
    monkeypatch.setenv("CUTAGENT_SEED_LOCAL_AUTH", "true")
    monkeypatch.delenv("CUTAGENT_AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST", raising=False)
    monkeypatch.setenv("CUTAGENT_ALLOW_SANDBOX_FALLBACK", "1")
    monkeypatch.setenv("CUTAGENT_XIAOVMAO_CDP_HOST", "127.0.0.1")
    monkeypatch.delenv("CUTAGENT_PUBLISHING_LOCAL_PROXY", raising=False)
    monkeypatch.setenv("CUTAGENT_WORKFLOW_RUNTIME", "local")


def _arm_safe_production(monkeypatch) -> None:
    monkeypatch.setenv("CUTAGENT_ENV", "production")
    monkeypatch.setenv("CUTAGENT_STORAGE_BACKEND", "sqlalchemy")
    monkeypatch.setenv("CUTAGENT_DATABASE_URL", "postgresql+psycopg://u:p@db:5432/cutagent")
    monkeypatch.setenv("CUTAGENT_REGISTRATION_OPEN", "false")
    monkeypatch.setenv("CUTAGENT_REGISTRATION_CODE_SALT", "a-unique-production-salt")
    monkeypatch.setenv("CUTAGENT_SEED_LOCAL_AUTH", "false")
    monkeypatch.setenv("CUTAGENT_AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("CUTAGENT_ENFORCE_PROVIDER_HOST_ALLOWLIST", "1")
    monkeypatch.delenv("CUTAGENT_ALLOW_SANDBOX_FALLBACK", raising=False)
    monkeypatch.setenv("CUTAGENT_PUBLISHING_LOCAL_PROXY", "1")
    monkeypatch.setenv("CUTAGENT_WORKFLOW_RUNTIME", "local")
    monkeypatch.delenv("CUTAGENT_REPLICA_COUNT", raising=False)


def test_preflight_flags_unsafe_production_defaults(monkeypatch):
    _arm_unsafe_production(monkeypatch)
    issues = validate_startup_settings(build_settings())
    codes = _codes(issues)
    assert {
        "database_url",
        "registration_open",
        "registration_code_salt",
        "seed_local_auth",
        "cookie_secure",
        "provider_host_allowlist",
        "sandbox_fallback",
        "publishing_cdp_host",
    } <= codes


def test_preflight_flags_invalid_storage_backend(monkeypatch):
    # The in-memory backend is rejected at Settings construction, but any *other*
    # non-sqlalchemy/postgres backend (a typo, or e.g. "sqlite") still constructs
    # and must be flagged by the preflight under the "storage_backend" code. This
    # keeps coverage of that (still reachable) branch after the memory backend
    # removal moved the unsafe-defaults case onto the "database_url" code.
    _arm_safe_production(monkeypatch)
    monkeypatch.setenv("CUTAGENT_STORAGE_BACKEND", "sqlite")
    assert "storage_backend" in _codes(validate_startup_settings(build_settings()))


def test_preflight_passes_a_safe_production_config(monkeypatch):
    _arm_safe_production(monkeypatch)
    assert validate_startup_settings(build_settings()) == []


def test_preflight_is_a_noop_outside_production(monkeypatch):
    # Same unsafe settings, but environment != production -> no findings, so local
    # dev / tests are never blocked.
    _arm_unsafe_production(monkeypatch)
    monkeypatch.setenv("CUTAGENT_ENV", "local")
    assert validate_startup_settings(build_settings()) == []


def test_preflight_requires_redis_and_shared_object_store_for_multi_replica(monkeypatch):
    _arm_safe_production(monkeypatch)
    monkeypatch.setenv("CUTAGENT_REPLICA_COUNT", "3")
    monkeypatch.delenv("CUTAGENT_REDIS_URL", raising=False)
    # default object store backend is local
    codes = _codes(validate_startup_settings(build_settings()))
    assert "redis_required" in codes
    assert "durable_object_store" in codes


def test_preflight_flags_temporal_with_local_ephemeral(monkeypatch):
    _arm_safe_production(monkeypatch)
    monkeypatch.setenv("CUTAGENT_WORKFLOW_RUNTIME", "temporal")
    monkeypatch.setenv("CUTAGENT_EPHEMERAL_OBJECTSTORE_BACKEND", "local")
    assert "ephemeral_object_store" in _codes(validate_startup_settings(build_settings()))


@pytest.mark.parametrize("seed_value", ["true", "1", "yes"])
def test_preflight_always_flags_seed_local_auth_in_production(monkeypatch, seed_value):
    _arm_safe_production(monkeypatch)
    monkeypatch.setenv("CUTAGENT_SEED_LOCAL_AUTH", seed_value)
    assert "seed_local_auth" in _codes(validate_startup_settings(build_settings()))
