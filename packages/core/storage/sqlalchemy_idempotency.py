from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from packages.core.storage.database import IdempotencyRecordRow


class SqlAlchemyIdempotencyRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def get(self, *, key: str, method: str, path: str, now: datetime) -> dict | None:
        with self.session_factory() as session:
            row = session.get(IdempotencyRecordRow, (key, method, path))
            if row is None:
                return None
            if row.expires_at <= now:
                session.delete(row)
                session.commit()
                return None
            return {
                "request_hash": row.request_hash,
                "content": row.response_body,
                "status_code": row.response_status,
            }

    def put(
        self,
        *,
        key: str,
        method: str,
        path: str,
        request_hash: str,
        response_status: int,
        response_body,
        expires_at: datetime,
    ) -> None:
        with self.session_factory() as session:
            row = IdempotencyRecordRow(
                key=key,
                method=method,
                path=path,
                request_hash=request_hash,
                response_status=response_status,
                response_body=response_body,
                expires_at=expires_at,
            )
            session.merge(row)
            session.commit()

    def cleanup_expired(self, *, now: datetime) -> int:
        with self.session_factory() as session:
            result = session.execute(delete(IdempotencyRecordRow).where(IdempotencyRecordRow.expires_at <= now))
            session.commit()
            return result.rowcount or 0
