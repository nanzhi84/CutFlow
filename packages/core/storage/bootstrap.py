from __future__ import annotations

import sys

from sqlalchemy import text

from packages.core.config import build_settings
from packages.core.storage.database import create_database_engine, create_session_factory
from packages.core.storage.seed import seed_database


def storage_backend() -> str:
    return build_settings().storage.backend


def sqlalchemy_backend_enabled() -> bool:
    return storage_backend() in {"sqlalchemy", "postgres"}


def warn_if_memory_backend() -> None:
    if storage_backend() != "memory":
        return
    print(
        "WARNING: CUTAGENT_STORAGE_BACKEND=memory is for tests/demo only and is not for production.",
        file=sys.stderr,
    )


def bootstrap_sqlalchemy_storage() -> int:
    engine = create_database_engine()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        return seed_database(session)


def bootstrap_sqlalchemy_storage_if_enabled() -> int:
    if not sqlalchemy_backend_enabled():
        warn_if_memory_backend()
        return 0
    return bootstrap_sqlalchemy_storage()


def get_sqlalchemy_session_factory_if_enabled():
    if not sqlalchemy_backend_enabled():
        return None
    return create_session_factory(create_database_engine())
