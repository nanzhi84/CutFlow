from __future__ import annotations

import hashlib

from packages.core.storage.object_store import LocalObjectStore
from packages.media.assets import store_file


def test_store_file_addressed_reuses_same_object_key_for_same_content(tmp_path):
    object_store = LocalObjectStore(tmp_path / "objects")
    source = tmp_path / "seed.mp4"
    source.write_bytes(b"same seed media bytes")

    first = store_file(object_store, source, purpose="seed-media", addressed=True)
    second = store_file(object_store, source, purpose="seed-media", addressed=True)

    assert second.ref.uri == first.ref.uri
    assert second.sha256 == hashlib.sha256(b"same seed media bytes").hexdigest()
    assert [path for path in (tmp_path / "objects").rglob("*") if path.is_file()] == [
        tmp_path / "objects" / first.ref.key
    ]
