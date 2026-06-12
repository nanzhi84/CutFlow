from __future__ import annotations

import os
import time

from scripts import gc_objectstore


def _write_object(root, prefix: str, key: str, *, age_hours: float) -> tuple[object, int]:
    object_dir = root / prefix / key
    object_dir.mkdir(parents=True)
    payload = object_dir / "payload.bin"
    content = f"{prefix}:{key}".encode("utf-8")
    payload.write_bytes(content)
    timestamp = time.time() - age_hours * 3600
    os.utime(payload, (timestamp, timestamp))
    os.utime(object_dir, (timestamp, timestamp))
    return object_dir, len(content)


def test_gc_objectstore_dry_run_keeps_files_and_apply_deletes_only_old_generated_objects(
    tmp_path,
    capsys,
):
    old_video, old_video_size = _write_object(
        tmp_path, "generated-video", "old-video", age_hours=3
    )
    old_audio, old_audio_size = _write_object(
        tmp_path, "generated-audio", "old-audio", age_hours=3
    )
    new_video, _ = _write_object(tmp_path, "generated-video", "new-video", age_hours=0)
    seed_media, _ = _write_object(tmp_path, "seed-media", "old-seed", age_hours=3)

    assert gc_objectstore.main(["--root", str(tmp_path), "--max-age-hours", "1"]) == 0
    dry_run_output = capsys.readouterr().out

    assert str(old_video) in dry_run_output
    assert str(old_audio) in dry_run_output
    assert str(seed_media) not in dry_run_output
    assert old_video.exists()
    assert old_audio.exists()

    assert (
        gc_objectstore.main(["--root", str(tmp_path), "--max-age-hours", "1", "--apply"])
        == 0
    )
    apply_output = capsys.readouterr().out

    assert str(old_video) in apply_output
    assert str(old_audio) in apply_output
    assert f"{old_video_size + old_audio_size} bytes" in apply_output
    assert not old_video.exists()
    assert not old_audio.exists()
    assert new_video.exists()
    assert seed_media.exists()
