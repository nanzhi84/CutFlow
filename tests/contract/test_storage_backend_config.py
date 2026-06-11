import pytest

from packages.core.storage.bootstrap import (
    bootstrap_sqlalchemy_storage_if_enabled,
    sqlalchemy_backend_enabled,
)
from packages.core.storage.database import database_url


def test_storage_backend_defaults_to_sqlalchemy(monkeypatch):
    monkeypatch.delenv("CUTAGENT_STORAGE_BACKEND", raising=False)

    assert sqlalchemy_backend_enabled() is True


def test_sqlalchemy_backend_requires_explicit_database_url(monkeypatch):
    monkeypatch.setenv("CUTAGENT_STORAGE_BACKEND", "sqlalchemy")
    monkeypatch.delenv("CUTAGENT_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="CUTAGENT_DATABASE_URL.*127.0.0.1:55432"):
        database_url()


def test_memory_backend_emits_non_production_warning(monkeypatch, capsys):
    monkeypatch.setenv("CUTAGENT_STORAGE_BACKEND", "memory")

    assert bootstrap_sqlalchemy_storage_if_enabled() == 0

    captured = capsys.readouterr()
    assert "CUTAGENT_STORAGE_BACKEND=memory" in captured.err
    assert "not for production" in captured.err
