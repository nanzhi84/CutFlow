from __future__ import annotations

from sqlalchemy import text

from packages.core.config import build_settings
from packages.core.storage.database import create_database_engine, create_session_factory
from packages.core.storage.seed import seed_database


def storage_backend() -> str:
    return build_settings().storage.backend


def sqlalchemy_backend_enabled() -> bool:
    # The in-memory storage backend has been removed; the only supported backends
    # are the SQLAlchemy-backed ones (Settings rejects anything else at build time).
    return storage_backend() in {"sqlalchemy", "postgres"}


def bootstrap_sqlalchemy_storage() -> int:
    engine = create_database_engine()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        return seed_database(session)


def get_sqlalchemy_session_factory():
    return create_session_factory(create_database_engine())
