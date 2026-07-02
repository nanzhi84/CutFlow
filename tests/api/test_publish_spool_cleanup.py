"""Publish spool retention: stale spool entries are pruned, fresh ones (including
scheduled-publish files still inside the retention window) are kept."""

from __future__ import annotations

import os
import time

from apps.api.services.publishing import (
    PUBLISH_SPOOL_RETENTION_SECONDS,
    _sweep_publish_spool,
)


def test_sweep_removes_stale_but_keeps_recent_entries(tmp_path):
    root = tmp_path / "publish-spool"
    root.mkdir()

    stale = root / "old"
    stale.mkdir()
    (stale / "video.mp4").write_bytes(b"x")
    fresh = root / "new"
    fresh.mkdir()
    (fresh / "video.mp4").write_bytes(b"y")

    old_mtime = time.time() - PUBLISH_SPOOL_RETENTION_SECONDS - 3600
    os.utime(stale, (old_mtime, old_mtime))

    _sweep_publish_spool(root)

    assert not stale.exists()
    assert fresh.exists()


def test_sweep_is_noop_when_root_missing(tmp_path):
    # Must not raise when the spool directory has never been created.
    _sweep_publish_spool(tmp_path / "does-not-exist")
