import pytest
from pydantic import ValidationError

from packages.core.config import build_settings
from packages.core.storage.database import database_url


def test_storage_backend_defaults_to_sqlalchemy(monkeypatch):
    monkeypatch.delenv("CUTAGENT_STORAGE_BACKEND", raising=False)

    assert build_settings().storage.backend == "sqlalchemy"


def test_sqlalchemy_backend_requires_explicit_database_url(monkeypatch):
    monkeypatch.setenv("CUTAGENT_STORAGE_BACKEND", "sqlalchemy")
    monkeypatch.delenv("CUTAGENT_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="CUTAGENT_DATABASE_URL.*127.0.0.1:55432"):
        database_url()


def test_memory_backend_is_rejected(monkeypatch):
    # The in-memory storage backend has been removed; configuring it must fail loudly
    # instead of silently degrading to a non-durable store.
    monkeypatch.setenv("CUTAGENT_STORAGE_BACKEND", "memory")

    with pytest.raises(ValidationError, match="memory is no longer supported"):
        build_settings()
