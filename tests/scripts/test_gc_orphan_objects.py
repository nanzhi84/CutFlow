from __future__ import annotations

import os
import time

from packages.core.storage.object_store import LocalObjectStore
from scripts.gc_orphan_objects import collect_orphans


def test_collect_orphans_keeps_referenced_and_recent_generated_objects(tmp_path) -> None:
    store = LocalObjectStore(tmp_path, bucket="outputs")
    old_orphan = store.prepare_upload("orphan.mp4", "generated-video")
    old_referenced = store.prepare_upload("kept.mp4", "generated-video")
    recent_orphan = store.prepare_upload("recent.ass", "subtitles")
    unrelated = store.prepare_upload("source.mp4", "portrait")
    for ref in (old_orphan, old_referenced, recent_orphan, unrelated):
        store.put_bytes(ref, b"data")
    old = time.time() - 7200
    os.utime(store._path(old_orphan), (old, old))
    os.utime(store._path(old_referenced), (old, old))

    orphans = collect_orphans(store, {old_referenced.uri}, max_age_hours=1)

    assert [candidate.uri for candidate in orphans] == [old_orphan.uri]
