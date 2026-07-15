from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _temporal_caption_font_assets(_db_isolation):
    """Exercise the real SQL -> worker font hydration path for subtitle-enabled runs."""

    if os.getenv("CUTAGENT_RUN_TEMPORAL_TESTS") != "1":
        return

    from packages.core.storage.bootstrap import get_sqlalchemy_session_factory
    from packages.core.storage.object_store import get_object_store
    from tests.golden._caption_font_fixture import register_sql_caption_fonts

    session_factory = get_sqlalchemy_session_factory()
    assert session_factory is not None
    register_sql_caption_fonts(session_factory, get_object_store())
