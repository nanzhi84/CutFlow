from __future__ import annotations

from sqlalchemy import text

from packages.core.storage.database import create_database_engine, create_session_factory
from packages.core.storage.seed import seed_database


def bootstrap_sqlalchemy_storage() -> int:
    engine = create_database_engine()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    session_factory = create_session_factory(engine)
    with session_factory() as session:
        return seed_database(session)


def get_sqlalchemy_session_factory():
    return create_session_factory(create_database_engine())
