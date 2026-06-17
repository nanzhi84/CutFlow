from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from packages.ai.gateway.sqlalchemy_repository import SqlAlchemyProviderRepository
from packages.core.contracts import TestProviderProfileRequest as ProviderProfileTestRequest
from packages.core.contracts import utcnow
from packages.core.storage.database import ProviderInvocationRow, ProviderProfileRow


sqlite3.register_adapter(dict, json.dumps)
sqlite3.register_adapter(list, json.dumps)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):
    return "JSON"


def _sqlite_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    ProviderProfileRow.__table__.create(engine)
    ProviderInvocationRow.__table__.create(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_sqlalchemy_provider_test_profile_uses_recent_invocation_latency() -> None:
    session_factory = _sqlite_session_factory()
    profile_id = "sandbox.tts.default"
    now = utcnow()
    with session_factory() as session:
        session.add(
            ProviderProfileRow(
                id=profile_id,
                provider_id="sandbox",
                model_id="tts.local",
                capability="tts.speech",
                display_name="Sandbox TTS",
                environment="local",
                retry_policy={},
                options_schema_ref={"schema_id": "provider.tts.options", "schema_version": "v1"},
                default_options={},
                enabled=True,
            )
        )
        for invocation_id, duration_ms in (
            ("pinv_latency_1", 100),
            ("pinv_latency_2", 250),
            ("pinv_latency_3", 500),
        ):
            session.add(
                ProviderInvocationRow(
                    id=invocation_id,
                    provider_id="sandbox",
                    model_id="tts.local",
                    provider_profile_id=profile_id,
                    capability_id="tts.speech",
                    status="succeeded",
                    billing_status="estimated",
                    duration_ms=duration_ms,
                    started_at=now,
                    finished_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.add(
            ProviderInvocationRow(
                id="pinv_old_latency",
                provider_id="sandbox",
                model_id="tts.local",
                provider_profile_id=profile_id,
                capability_id="tts.speech",
                status="succeeded",
                billing_status="estimated",
                duration_ms=5000,
                started_at=now - timedelta(hours=48),
                finished_at=now - timedelta(hours=48),
                created_at=now - timedelta(hours=48),
                updated_at=now - timedelta(hours=48),
            )
        )
        session.commit()

    response = SqlAlchemyProviderRepository(session_factory).test_profile(
        profile_id,
        ProviderProfileTestRequest(),
    )

    assert response.ok is True
    assert response.latency_ms == 500
