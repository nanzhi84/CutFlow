#!/usr/bin/env python3
"""Install bounded CutFlow database backup and cleanup jobs for macOS launchd."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
BACKUP_LABEL = "cutflow.database-backup"
PRUNE_LABEL = "cutflow.database-backup-prune"
DEFAULT_BACKUP_DIR = ROOT / ".data" / "db-backups"
DEFAULT_INTERVAL_HOURS = 6
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_COUNT = 32
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MIN_KEEP = 4
DEFAULT_CLEANUP_HOUR = 4
DEFAULT_CLEANUP_MINUTE = 30
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
LAUNCH_PATH = ":".join(
    (
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/Applications/Docker.app/Contents/Resources/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    )
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _hour(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 23:
        raise argparse.ArgumentTypeError("must be between 0 and 23")
    return parsed


def _minute(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 59:
        raise argparse.ArgumentTypeError("must be between 0 and 59")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--interval-hours", type=_positive_int, default=DEFAULT_INTERVAL_HOURS)
    parser.add_argument("--retention-days", type=_positive_int, default=DEFAULT_RETENTION_DAYS)
    parser.add_argument("--max-count", type=_positive_int, default=DEFAULT_MAX_COUNT)
    parser.add_argument("--max-bytes", type=_positive_int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--min-keep", type=_positive_int, default=DEFAULT_MIN_KEEP)
    parser.add_argument("--cleanup-hour", type=_hour, default=DEFAULT_CLEANUP_HOUR)
    parser.add_argument("--cleanup-minute", type=_minute, default=DEFAULT_CLEANUP_MINUTE)
    parser.add_argument("--log-max-bytes", type=_positive_int, default=DEFAULT_LOG_MAX_BYTES)
    parser.add_argument("--no-run-now", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    return parser


def build_plists(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    backup_dir = args.backup_dir.expanduser().resolve()
    python_bin = Path("/usr/bin/python3")
    if not python_bin.exists():
        python_bin = Path(sys.executable).resolve()
    common = {
        "WorkingDirectory": str(ROOT),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "RunAtLoad": False,
        "ThrottleInterval": 60,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    backup_environment = {
        "HOME": str(Path.home()),
        "PATH": LAUNCH_PATH,
        "CUTAGENT_BACKUP_DIR": str(backup_dir),
        "CUTAGENT_BACKUP_PREFIX": "cutagent-auto",
        "CUTAGENT_BACKUP_RETENTION_DAYS": str(args.retention_days),
        "CUTAGENT_BACKUP_MAX_COUNT": str(args.max_count),
        "CUTAGENT_BACKUP_MAX_BYTES": str(args.max_bytes),
        "CUTAGENT_BACKUP_MIN_KEEP": str(args.min_keep),
        "CUTAGENT_BACKUP_LOG_FILE": str(backup_dir / "backup.log"),
        "CUTAGENT_BACKUP_LOG_MAX_BYTES": str(args.log_max_bytes),
        "CUTAGENT_BACKUP_PYTHON": str(python_bin),
    }
    backup = {
        **common,
        "Label": BACKUP_LABEL,
        "ProgramArguments": [
            "/bin/bash",
            str(ROOT / "scripts" / "backup_database.sh"),
        ],
        "EnvironmentVariables": backup_environment,
        "StartInterval": args.interval_hours * 60 * 60,
    }
    prune = {
        **common,
        "Label": PRUNE_LABEL,
        "ProgramArguments": [
            str(python_bin),
            str(ROOT / "scripts" / "prune_database_backups.py"),
            "--backup-dir",
            str(backup_dir),
            "--prefix",
            "cutagent-auto",
            "--retention-days",
            str(args.retention_days),
            "--max-count",
            str(args.max_count),
            "--max-bytes",
            str(args.max_bytes),
            "--min-keep",
            str(args.min_keep),
            "--log-file",
            str(backup_dir / "prune.log"),
            "--log-max-bytes",
            str(args.log_max_bytes),
        ],
        "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": LAUNCH_PATH},
        "StartCalendarInterval": {
            "Hour": args.cleanup_hour,
            "Minute": args.cleanup_minute,
        },
    }
    return {BACKUP_LABEL: backup, PRUNE_LABEL: prune}


def _launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *arguments],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _write_plist(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as output:
        plistlib.dump(payload, output, sort_keys=True)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _bootout(domain: str, label: str) -> None:
    _launchctl("bootout", f"{domain}/{label}", check=False)


def uninstall(domain: str) -> None:
    for label in (BACKUP_LABEL, PRUNE_LABEL):
        _bootout(domain, label)
        _plist_path(label).unlink(missing_ok=True)
        print(f"已卸载 {label}")


def install(args: argparse.Namespace, domain: str) -> None:
    if args.max_count < args.min_keep:
        raise ValueError("max-count must be greater than or equal to min-keep")
    backup_dir = args.backup_dir.expanduser().resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    plists = build_plists(args)

    for label, payload in plists.items():
        path = _plist_path(label)
        _bootout(domain, label)
        _write_plist(path, payload)
        _launchctl("bootstrap", domain, str(path))
        _launchctl("enable", f"{domain}/{label}")
        print(f"已安装 {label}: {path}")

    if not args.no_run_now:
        _launchctl("kickstart", "-k", f"{domain}/{BACKUP_LABEL}")
        print("已触发一次立即备份；后续可在 backup.log 查看结果")

    print(
        f"备份周期：每 {args.interval_hours} 小时；清理时间：每天 "
        f"{args.cleanup_hour:02d}:{args.cleanup_minute:02d}"
    )
    print(
        f"保留上限：{args.retention_days} 天 / {args.max_count} 份 / "
        f"{args.max_bytes} bytes（至少保留 {args.min_keep} 份）"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    if sys.platform != "darwin":
        print("此安装器仅支持 macOS launchd。", file=sys.stderr)
        return 2
    args = _parser().parse_args(argv)
    domain = f"gui/{os.getuid()}"
    try:
        if args.uninstall:
            uninstall(domain)
        else:
            install(args, domain)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"安装失败：{exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
