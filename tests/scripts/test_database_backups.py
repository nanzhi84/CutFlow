from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.install_database_backup_launchd import (
    BACKUP_LABEL,
    PRUNE_LABEL,
    build_plists,
)
from scripts.prune_database_backups import RetentionPolicy, prune_backups


ROOT = Path(__file__).resolve().parents[2]
DAY = 24 * 60 * 60


def _backup_set(
    directory: Path,
    name: str,
    *,
    size: int,
    mtime: float,
) -> Path:
    dump = directory / f"cutagent-auto-{name}.dump"
    dump.write_bytes(b"x" * size)
    Path(f"{dump}.sha256").write_text("checksum\n", encoding="utf-8")
    Path(f"{dump}.json").write_text("{}\n", encoding="utf-8")
    for path in (dump, Path(f"{dump}.sha256"), Path(f"{dump}.json")):
        os.utime(path, (mtime, mtime))
    return dump


def test_prune_bounds_managed_backups_by_count_without_touching_manual_files(tmp_path):
    now = 10_000_000.0
    backups = [
        _backup_set(tmp_path, f"{index:02d}", size=10, mtime=now - index) for index in range(6)
    ]
    manual = tmp_path / "cutagent-embedding-recovered-818.dump"
    manual.write_bytes(b"keep me")

    result = prune_backups(
        tmp_path,
        RetentionPolicy(retention_days=30, max_count=3, max_bytes=1_000, min_keep=2),
        now=now,
    )

    assert set(result.kept) == set(backups[:3])
    assert set(result.deleted) == set(backups[3:])
    assert result.remaining_bytes == 30
    assert manual.read_bytes() == b"keep me"
    for deleted in backups[3:]:
        assert not deleted.exists()
        assert not Path(f"{deleted}.sha256").exists()
        assert not Path(f"{deleted}.json").exists()


def test_prune_applies_age_then_byte_budget_but_preserves_minimum(tmp_path):
    now = 20_000_000.0
    backups = [
        _backup_set(tmp_path, f"{index:02d}", size=20, mtime=now - index * DAY)
        for index in range(6)
    ]

    result = prune_backups(
        tmp_path,
        RetentionPolicy(retention_days=3, max_count=10, max_bytes=50, min_keep=2),
        now=now,
    )

    assert set(result.kept) == set(backups[:2])
    assert set(result.deleted) == set(backups[2:])
    assert result.remaining_bytes == 40
    assert result.byte_budget_satisfied


def test_prune_dry_run_reports_stale_files_without_deleting_them(tmp_path):
    now = 30_000_000.0
    backup = _backup_set(tmp_path, "current", size=20, mtime=now)
    partial = tmp_path / ".cutagent-auto-old.dump.partial"
    orphan = tmp_path / "cutagent-auto-orphan.dump.json"
    partial.write_bytes(b"partial")
    orphan.write_text("{}", encoding="utf-8")
    for path in (partial, orphan):
        os.utime(path, (now - 2 * DAY, now - 2 * DAY))

    result = prune_backups(
        tmp_path,
        RetentionPolicy(retention_days=30, max_count=5, max_bytes=1_000, min_keep=1),
        now=now,
        dry_run=True,
    )

    assert result.kept == (backup,)
    assert set(result.stale_deleted) == {partial, orphan}
    assert partial.exists()
    assert orphan.exists()


def test_backup_shell_is_valid_and_prints_bounded_defaults():
    subprocess.run(
        ["bash", "-n", "scripts/backup_database.sh"],
        cwd=ROOT,
        check=True,
    )
    result = subprocess.run(
        ["bash", "scripts/backup_database.sh", "--print-config"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    config = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
    assert config["prefix"] == "cutagent-auto"
    assert config["retention_days"] == "14"
    assert config["max_count"] == "32"
    assert config["max_bytes"] == str(2 * 1024 * 1024 * 1024)
    assert config["min_keep"] == "4"
    assert config["verify_restore"] == "1"

    script = (ROOT / "scripts" / "backup_database.sh").read_text(encoding="utf-8")
    assert re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", script) is None


def test_launchd_plists_schedule_independent_backup_and_cleanup_jobs(tmp_path):
    args = SimpleNamespace(
        backup_dir=tmp_path,
        interval_hours=6,
        retention_days=14,
        max_count=32,
        max_bytes=2 * 1024 * 1024 * 1024,
        min_keep=4,
        cleanup_hour=4,
        cleanup_minute=30,
        log_max_bytes=10 * 1024 * 1024,
    )

    plists = build_plists(args)

    assert plists[BACKUP_LABEL]["StartInterval"] == 6 * 60 * 60
    assert plists[BACKUP_LABEL]["EnvironmentVariables"]["CUTAGENT_BACKUP_MAX_COUNT"] == ("32")
    assert plists[PRUNE_LABEL]["StartCalendarInterval"] == {"Hour": 4, "Minute": 30}
    assert "--max-bytes" in plists[PRUNE_LABEL]["ProgramArguments"]
    assert plists[BACKUP_LABEL]["StandardOutPath"] == "/dev/null"


def test_prune_cli_rejects_count_below_minimum(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/prune_database_backups.py",
            "--backup-dir",
            str(tmp_path),
            "--max-count",
            "2",
            "--min-keep",
            "4",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 2
    assert "max_count must be greater than or equal to min_keep" in result.stderr
