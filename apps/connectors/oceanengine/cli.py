"""Thin CLI for the OceanEngine offline-import connector.

Examples::

    python -m apps.connectors.oceanengine.cli import-archive --archive-root ./archive
    python -m apps.connectors.oceanengine.cli import-file ./a.xlsx --source-page video_analysis

The CLI is intentionally side-effect-light: it produces (and optionally prints)
``MetricsImportRequest`` payloads. Posting them to the API is left to the caller
so the connector stays a pure offline ETL stage with no network coupling.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.connectors.oceanengine.ingest import (
    IngestResult,
    IngestSummary,
    default_archive,
    import_archive_tree,
    import_archived_xlsx,
)


def _result_payload(result: IngestResult) -> dict[str, object]:
    return {
        "source_page": result.source_page,
        "archived_path": result.archived_path,
        "file_sha256": result.file_sha256,
        "status": result.status,
        "new_row_count": result.new_row_count,
        "skipped_row_count": result.skipped_row_count,
        "reason": result.reason,
        "import_request": result.import_request.model_dump() if result.import_request else None,
    }


def _summary_payload(summary: IngestSummary) -> dict[str, object]:
    return {
        "files_seen": summary.files_seen,
        "files_ingested": summary.files_ingested,
        "files_skipped": summary.files_skipped,
        "files_unsupported": summary.files_unsupported,
        "new_rows": summary.new_rows,
        "skipped_rows": summary.skipped_rows,
        "results": [_result_payload(r) for r in summary.results],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oceanengine-connector",
        description="OceanEngine offline XLSX import (normalize -> MetricsImportRequest)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    tree = subparsers.add_parser("import-archive", help="ingest every archived XLSX under raw/")
    tree.add_argument("--archive-root", required=True)
    tree.add_argument("--db-path")
    tree.add_argument("--dry-run", action="store_true")

    one = subparsers.add_parser("import-file", help="ingest a single archived XLSX")
    one.add_argument("xlsx_path")
    one.add_argument("--archive-root")
    one.add_argument("--source-page")
    one.add_argument("--db-path")
    one.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "import-archive":
        archive = default_archive(args.archive_root) if not args.db_path else _archive(args.db_path)
        summary = import_archive_tree(args.archive_root, archive=archive, dry_run=args.dry_run)
        print(json.dumps(_summary_payload(summary), ensure_ascii=False, indent=2))
        return 0

    if args.command == "import-file":
        if args.db_path:
            archive = _archive(args.db_path)
        elif args.archive_root:
            archive = default_archive(args.archive_root)
        else:
            archive = _archive(Path(args.xlsx_path).resolve().parent / "oceanengine_offline.sqlite3")
        result = import_archived_xlsx(
            args.xlsx_path,
            archive=archive,
            source_page=args.source_page,
            archive_root=args.archive_root,
            dry_run=args.dry_run,
        )
        print(json.dumps(_result_payload(result), ensure_ascii=False, indent=2))
        return 0

    return 1


def _archive(db_path: str | Path):
    from apps.connectors.oceanengine.archive import ImportArchive

    return ImportArchive(db_path)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
