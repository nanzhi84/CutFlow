"""Offline ingest entry points for the OceanEngine connector.

Pipeline per archived file::

    .xlsx  -> read_first_sheet -> normalize_rows -> dedupe -> MetricsImportRequest

The archive directory layout follows the RPA box convention::

    <archive_root>/raw/<YYYY-MM-DD>/<source_page>/<run_id>/<file>.xlsx

``import_archive_tree`` walks every such file; ``import_archived_xlsx`` handles a
single one. Both produce :class:`IngestResult` objects containing a
``MetricsImportRequest`` whose ``rows`` are flattened, one row per numeric
metric, ready to POST to ``POST /api/cases/{case_id}/metrics/import``.

Dedupe is two-tier and makes re-import a full skip: a file whose bytes were
already imported is skipped entirely; otherwise only rows with new content
fingerprints are emitted. Comment pages carry no spend metrics, so they emit no
import rows but are still recorded for dedupe.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from packages.core.contracts import MetricsImportRequest, OceanEngineMetricRow, OceanEngineSourcePage

from apps.connectors.oceanengine.archive import ImportArchive
from apps.connectors.oceanengine.normalize import normalize_rows
from apps.connectors.oceanengine.xlsx import XlsxUnsupportedError, openpyxl_available, read_first_sheet

_KNOWN_SOURCE_PAGES: tuple[OceanEngineSourcePage, ...] = (
    "video_analysis",
    "localpush_account",
    "localpush_unit",
    "comment_content",
)


@dataclass
class IngestResult:
    """Outcome of ingesting one archived XLSX file."""

    source_page: OceanEngineSourcePage
    archived_path: str
    file_sha256: str
    status: str  # "ingested" | "skipped_duplicate_file" | "unsupported"
    new_row_count: int = 0
    skipped_row_count: int = 0
    import_request: MetricsImportRequest | None = None
    reason: str | None = None


@dataclass
class IngestSummary:
    """Aggregate outcome of walking an archive tree."""

    files_seen: int = 0
    files_ingested: int = 0
    files_skipped: int = 0
    files_unsupported: int = 0
    new_rows: int = 0
    skipped_rows: int = 0
    results: list[IngestResult] = field(default_factory=list)

    def add(self, result: IngestResult) -> None:
        self.files_seen += 1
        self.results.append(result)
        if result.status == "ingested":
            self.files_ingested += 1
            self.new_rows += result.new_row_count
            self.skipped_rows += result.skipped_row_count
        elif result.status == "skipped_duplicate_file":
            self.files_skipped += 1
        elif result.status == "unsupported":
            self.files_unsupported += 1


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def infer_source_page(path: str | Path, archive_root: str | Path) -> OceanEngineSourcePage:
    """Infer the source page from the archive path segment.

    Expects ``<archive_root>/raw/<date>/<source_page>/<run_id>/<file>.xlsx``.
    """

    raw_root = Path(archive_root).resolve() / "raw"
    relative = Path(path).resolve().relative_to(raw_root)
    parts = relative.parts
    if len(parts) != 4 or relative.suffix.lower() != ".xlsx":
        raise ValueError(
            "archived xlsx must match <archive_root>/raw/<date>/<source_page>/<run_id>/<file>.xlsx"
        )
    source_page = parts[1]
    if source_page not in _KNOWN_SOURCE_PAGES:
        raise ValueError(f"unknown source_page directory: {source_page}")
    return source_page  # type: ignore[return-value]


def build_import_rows(normalized: list[OceanEngineMetricRow]) -> list[dict[str, object]]:
    """Flatten normalized rows into MetricsImportRequest rows (one per metric).

    Each row carries ``external_ref`` and ``oceanengine_row_fingerprint`` so the
    API-side matching policy can resolve the ``publish_record_id`` (offline ETL
    does not guess that binding).
    """

    rows: list[dict[str, object]] = []
    for record in normalized:
        for metric_name, metric_value in sorted(record.metrics.items()):
            rows.append(
                {
                    "source": "oceanengine_rpa",
                    "source_page": record.source_page,
                    "external_ref": record.external_ref,
                    "metric_name": metric_name,
                    "metric_value": metric_value,
                    "oceanengine_row_fingerprint": record.row_fingerprint,
                }
            )
    return rows


def import_archived_xlsx(
    xlsx_path: str | Path,
    *,
    archive: ImportArchive,
    source_page: OceanEngineSourcePage | None = None,
    archive_root: str | Path | None = None,
    dry_run: bool = False,
) -> IngestResult:
    """Ingest a single archived XLSX into a :class:`MetricsImportRequest`.

    ``source_page`` may be passed explicitly or inferred from ``archive_root``.
    """

    path = Path(xlsx_path).resolve()
    if source_page is None:
        if archive_root is None:
            raise ValueError("source_page or archive_root is required")
        source_page = infer_source_page(path, archive_root)

    archive.initialize()
    file_sha256 = sha256_file(path)

    if archive.file_already_imported(file_sha256):
        return IngestResult(
            source_page=source_page,
            archived_path=str(path),
            file_sha256=file_sha256,
            status="skipped_duplicate_file",
            reason="file sha256 already imported",
        )

    if not openpyxl_available():
        return IngestResult(
            source_page=source_page,
            archived_path=str(path),
            file_sha256=file_sha256,
            status="unsupported",
            reason="openpyxl is not installed",
        )

    try:
        raw_rows = read_first_sheet(path)
    except XlsxUnsupportedError:
        return IngestResult(
            source_page=source_page,
            archived_path=str(path),
            file_sha256=file_sha256,
            status="unsupported",
            reason="openpyxl is not installed",
        )

    normalized = normalize_rows(source_page, raw_rows)
    seen = archive.seen_row_fingerprints([r.row_fingerprint for r in normalized])
    new_rows = [r for r in normalized if r.row_fingerprint not in seen]
    skipped = len(normalized) - len(new_rows)

    import_rows = build_import_rows(new_rows)
    request = (
        MetricsImportRequest(rows=import_rows, dry_run=dry_run) if import_rows else None
    )

    if not dry_run:
        archive.record_import(
            file_sha256=file_sha256,
            source_page=source_page,
            archived_path=str(path),
            row_count=len(normalized),
            row_fingerprints=[r.row_fingerprint for r in new_rows],
        )

    return IngestResult(
        source_page=source_page,
        archived_path=str(path),
        file_sha256=file_sha256,
        status="ingested",
        new_row_count=len(new_rows),
        skipped_row_count=skipped,
        import_request=request,
    )


def import_archive_tree(
    archive_root: str | Path,
    *,
    archive: ImportArchive,
    dry_run: bool = False,
) -> IngestSummary:
    """Walk ``<archive_root>/raw`` and ingest every archived XLSX file."""

    root = Path(archive_root).resolve()
    summary = IngestSummary()
    for xlsx_path in sorted((root / "raw").glob("*/*/*/*.xlsx")):
        result = import_archived_xlsx(
            xlsx_path,
            archive=archive,
            archive_root=root,
            dry_run=dry_run,
        )
        summary.add(result)
    return summary


def default_archive(archive_root: str | Path) -> ImportArchive:
    """Return the canonical dedupe archive living under the archive root."""

    return ImportArchive(Path(archive_root).resolve() / "db" / "oceanengine_offline.sqlite3")
