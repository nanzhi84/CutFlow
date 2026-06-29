"""Re-seeding must not revert an operator-changed admin password (issue #66)."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from packages.core.storage.database import UserRow
from packages.core.storage.seed import seed_database, seed_rows


def _admin_seed_rows() -> list[UserRow]:
    return [row for row in seed_rows() if isinstance(row, UserRow) and row.id == "usr_admin"]


def test_reseed_preserves_operator_changed_admin_password():
    engine = create_engine("sqlite://")
    UserRow.__table__.create(engine)

    # First bootstrap: creates usr_admin with the known local-dev hash.
    with Session(engine) as session:
        inserted = seed_database(session, _admin_seed_rows())
        assert inserted == 1

    # Operator rotates the admin password.
    with Session(engine) as session:
        admin = session.get(UserRow, "usr_admin")
        admin.password_hash = "OPERATOR-ROTATED-HASH"
        session.commit()

    # A subsequent bootstrap/startup re-seeds — it must NOT overwrite the row.
    with Session(engine) as session:
        inserted = seed_database(session, _admin_seed_rows())
        assert inserted == 0  # already exists, nothing inserted

    with Session(engine) as session:
        admin = session.get(UserRow, "usr_admin")
        assert admin.password_hash == "OPERATOR-ROTATED-HASH"
