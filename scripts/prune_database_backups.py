#!/usr/bin/env python3
"""Prune CutFlow-owned PostgreSQL backups with bounded retention."""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_DIR = ROOT / ".data" / "db-backups"
DEFAULT_PREFIX = "cutagent-auto"
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_COUNT = 32
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MIN_KEEP = 4
DEFAULT_STALE_HOURS = 24
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class BackupFile:
    path: Path
    mtime: float
    size: int


@dataclass(frozen=True)
class RetentionPolicy:
    retention_days: int = DEFAULT_RETENTION_DAYS
    max_count: int = DEFAULT_MAX_COUNT
    max_bytes: int = DEFAULT_MAX_BYTES
    min_keep: int = DEFAULT_MIN_KEEP
    stale_hours: int = DEFAULT_STALE_HOURS

    def validate(self) -> None:
        if self.retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        if self.min_keep < 1:
            raise ValueError("min_keep must be at least 1")
        if self.max_count < self.min_keep:
            raise ValueError("max_count must be greater than or equal to min_keep")
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if self.stale_hours < 1:
            raise ValueError("stale_hours must be at least 1")


@dataclass(frozen=True)
class PruneResult:
    kept: tuple[Path, ...]
    deleted: tuple[Path, ...]
    stale_deleted: tuple[Path, ...]
    reclaimed_bytes: int
    remaining_bytes: int
    byte_budget_satisfied: bool


def _managed_backups(backup_dir: Path, prefix: str) -> list[BackupFile]:
    backups = []
    for path in backup_dir.glob(f"{prefix}-*.dump"):
        if not path.is_file() or path.is_symlink():
            continue
        stat = path.stat()
        backups.append(BackupFile(path=path, mtime=stat.st_mtime, size=stat.st_size))
    return sorted(backups, key=lambda item: (item.mtime, item.path.name), reverse=True)


def _plan_deletions(
    backups: Sequence[BackupFile], policy: RetentionPolicy, now: float
) -> set[Path]:
    protected = {item.path for item in backups[: policy.min_keep]}
    deleted: set[Path] = set()
    age_cutoff = now - policy.retention_days * 24 * 60 * 60

    for item in backups:
        if item.path not in protected and item.mtime < age_cutoff:
            deleted.add(item.path)

    remaining = [item for item in backups if item.path not in deleted]
    while len(remaining) > policy.max_count:
        candidate = remaining[-1]
        if candidate.path in protected:
            break
        deleted.add(candidate.path)
        remaining.pop()

    remaining_bytes = sum(item.size for item in remaining)
    while remaining_bytes > policy.max_bytes:
        candidate = next((item for item in reversed(remaining) if item.path not in protected), None)
        if candidate is None:
            break
        deleted.add(candidate.path)
        remaining.remove(candidate)
        remaining_bytes -= candidate.size

    return deleted


def _stale_artifacts(
    backup_dir: Path, prefix: str, cutoff: float, deleted_backups: set[Path]
) -> list[Path]:
    stale = []
    patterns = (
        f".{prefix}-*.dump.partial",
        f".{prefix}-*.dump.sha256.partial",
        f".{prefix}-*.dump.json.partial",
    )
    for pattern in patterns:
        for path in backup_dir.glob(pattern):
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                stale.append(path)

    for suffix in (".sha256", ".json"):
        for path in backup_dir.glob(f"{prefix}-*.dump{suffix}"):
            if not path.is_file() or path.is_symlink() or path.stat().st_mtime >= cutoff:
                continue
            dump_path = Path(str(path)[: -len(suffix)])
            if not dump_path.exists() or dump_path in deleted_backups:
                stale.append(path)
    return sorted(set(stale))


def prune_backups(
    backup_dir: Path,
    policy: RetentionPolicy,
    *,
    prefix: str = DEFAULT_PREFIX,
    now: Optional[float] = None,
    dry_run: bool = False,
) -> PruneResult:
    policy.validate()
    now = time.time() if now is None else now
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)

    backups = _managed_backups(backup_dir, prefix)
    deleted_paths = _plan_deletions(backups, policy, now)
    cutoff = now - policy.stale_hours * 60 * 60
    stale_paths = _stale_artifacts(backup_dir, prefix, cutoff, deleted_paths)

    reclaimed_bytes = 0
    for item in backups:
        if item.path not in deleted_paths:
            continue
        reclaimed_bytes += item.size
        if dry_run:
            continue
        item.path.unlink(missing_ok=True)
        Path(f"{item.path}.sha256").unlink(missing_ok=True)
        Path(f"{item.path}.json").unlink(missing_ok=True)

    for path in stale_paths:
        if not dry_run:
            path.unlink(missing_ok=True)

    kept = tuple(item.path for item in backups if item.path not in deleted_paths)
    remaining_bytes = sum(item.size for item in backups if item.path not in deleted_paths)
    return PruneResult(
        kept=kept,
        deleted=tuple(sorted(deleted_paths)),
        stale_deleted=tuple(stale_paths),
        reclaimed_bytes=reclaimed_bytes,
        remaining_bytes=remaining_bytes,
        byte_budget_satisfied=remaining_bytes <= policy.max_bytes,
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None else int(raw)


def _rotate_and_open_log(path: Path, max_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists() and path.stat().st_size > max_bytes:
        keep_bytes = max(max_bytes // 2, 1)
        with path.open("rb") as source:
            source.seek(max(path.stat().st_size - keep_bytes, 0))
            tail = source.read()
        temporary = path.with_name(f".{path.name}.rotate")
        temporary.write_bytes(tail)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    stream = path.open("a", encoding="utf-8", buffering=1)
    os.chmod(path, 0o600)
    sys.stdout = stream
    sys.stderr = stream


def _active_backup_pid(backup_dir: Path) -> Optional[int]:
    pid_path = backup_dir / ".backup.lock" / "pid"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
        return None
    return pid


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path(os.environ.get("CUTAGENT_BACKUP_DIR", DEFAULT_BACKUP_DIR)),
    )
    parser.add_argument(
        "--prefix", default=os.environ.get("CUTAGENT_BACKUP_PREFIX", DEFAULT_PREFIX)
    )
    parser.add_argument(
        "--retention-days",
        type=_positive_int,
        default=_env_int("CUTAGENT_BACKUP_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
    )
    parser.add_argument(
        "--max-count",
        type=_positive_int,
        default=_env_int("CUTAGENT_BACKUP_MAX_COUNT", DEFAULT_MAX_COUNT),
    )
    parser.add_argument(
        "--max-bytes",
        type=_positive_int,
        default=_env_int("CUTAGENT_BACKUP_MAX_BYTES", DEFAULT_MAX_BYTES),
    )
    parser.add_argument(
        "--min-keep",
        type=_positive_int,
        default=_env_int("CUTAGENT_BACKUP_MIN_KEEP", DEFAULT_MIN_KEEP),
    )
    parser.add_argument(
        "--stale-hours",
        type=_positive_int,
        default=_env_int("CUTAGENT_BACKUP_STALE_HOURS", DEFAULT_STALE_HOURS),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument(
        "--log-max-bytes",
        type=_positive_int,
        default=_env_int("CUTAGENT_BACKUP_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.log_file:
        _rotate_and_open_log(args.log_file, args.log_max_bytes)
    active_pid = _active_backup_pid(args.backup_dir)
    if active_pid is not None:
        print(f"备份任务正在运行（pid {active_pid}），本次清理跳过")
        return 0
    policy = RetentionPolicy(
        retention_days=args.retention_days,
        max_count=args.max_count,
        max_bytes=args.max_bytes,
        min_keep=args.min_keep,
        stale_hours=args.stale_hours,
    )
    try:
        result = prune_backups(
            args.backup_dir,
            policy,
            prefix=args.prefix,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    mode = "演练" if args.dry_run else "执行"
    print(
        f"[{timestamp}] 清理{mode}完成：保留 {len(result.kept)} 份，"
        f"删除 {len(result.deleted)} 份，回收 {result.reclaimed_bytes} bytes，"
        f"剩余 {result.remaining_bytes} bytes"
    )
    for path in result.deleted:
        print(f"  删除备份：{path.name}")
    for path in result.stale_deleted:
        print(f"  删除残留：{path.name}")
    if not result.byte_budget_satisfied:
        print(
            "警告：最少保留份数占用已超过容量上限；份数仍有界，请提高容量上限或降低 min_keep。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
