from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.core.storage.database import create_database_engine, create_session_factory
from packages.core.storage.object_store import get_object_store
from packages.core.storage.seed import seed_database
from packages.core.storage.seed_media import seed_media_assets


def main() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    session_factory = create_session_factory(create_database_engine())
    with session_factory() as session:
        inserted = seed_database(session)
        media_seeded = seed_media_assets(session, get_object_store())
    print(f"Database bootstrapped; inserted {inserted} seed rows, {media_seeded} demo media source artifacts.")


if __name__ == "__main__":
    main()

