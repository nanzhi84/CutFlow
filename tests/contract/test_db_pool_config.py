"""Engine connection pool is env-configurable for non-sqlite backends."""

from packages.core.storage.database import create_database_engine

_PG_URL = "postgresql+psycopg://u:p@127.0.0.1:5432/db"
_POOL_ENV_VARS = (
    "CUTAGENT_DB_POOL_SIZE",
    "CUTAGENT_DB_MAX_OVERFLOW",
    "CUTAGENT_DB_POOL_RECYCLE",
    "CUTAGENT_DB_POOL_TIMEOUT",
)


def _clear_pool_env(monkeypatch) -> None:
    for name in _POOL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_pool_defaults_when_env_unset(monkeypatch):
    _clear_pool_env(monkeypatch)

    engine = create_database_engine(_PG_URL)
    pool = engine.pool

    assert pool.size() == 5
    assert pool._max_overflow == 10
    assert pool._recycle == 1800
    assert pool._timeout == 30
    assert pool._pre_ping is True


def test_pool_params_honor_env(monkeypatch):
    monkeypatch.setenv("CUTAGENT_DB_POOL_SIZE", "7")
    monkeypatch.setenv("CUTAGENT_DB_MAX_OVERFLOW", "3")
    monkeypatch.setenv("CUTAGENT_DB_POOL_RECYCLE", "900")
    monkeypatch.setenv("CUTAGENT_DB_POOL_TIMEOUT", "15")

    engine = create_database_engine(_PG_URL)
    pool = engine.pool

    assert pool.size() == 7
    assert pool._max_overflow == 3
    assert pool._recycle == 900
    assert pool._timeout == 15
    assert pool._pre_ping is True


def test_blank_env_falls_back_to_defaults(monkeypatch):
    for name in _POOL_ENV_VARS:
        monkeypatch.setenv(name, "  ")

    engine = create_database_engine(_PG_URL)
    pool = engine.pool

    assert pool.size() == 5
    assert pool._max_overflow == 10
    assert pool._recycle == 1800
    assert pool._timeout == 30


def test_sqlite_engine_unaffected_by_pool_env(monkeypatch):
    # Pool sizing env vars must not be passed to a sqlite engine: the default
    # in-memory pool (SingletonThreadPool) does not accept QueuePool sizing
    # args, so building it must still succeed and keep pool_pre_ping=True.
    monkeypatch.setenv("CUTAGENT_DB_POOL_SIZE", "7")
    monkeypatch.setenv("CUTAGENT_DB_MAX_OVERFLOW", "3")
    monkeypatch.setenv("CUTAGENT_DB_POOL_RECYCLE", "900")
    monkeypatch.setenv("CUTAGENT_DB_POOL_TIMEOUT", "15")

    engine = create_database_engine("sqlite+pysqlite:///:memory:")

    assert engine.dialect.name == "sqlite"
    assert engine.pool._pre_ping is True
    # No QueuePool sizing was applied to the sqlite pool.
    assert not hasattr(engine.pool, "_max_overflow")
