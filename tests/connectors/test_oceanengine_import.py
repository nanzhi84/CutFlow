"""Unit tests for the OceanEngine offline-import connector.

Fixtures are synthetic XLSX files written with openpyxl at test time (no real
RPA archive is available yet). Real archive XLSX from the operator box is needed
for end-to-end acceptance — see the connector's ``deferred`` note.
"""

from __future__ import annotations

from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

from apps.connectors.oceanengine import (
    ImportArchive,
    default_archive,
    import_archive_tree,
    import_archived_xlsx,
)
from apps.connectors.oceanengine.normalize import (
    normalize_comment_content,
    normalize_localpush_account,
    normalize_localpush_unit,
    normalize_video_analysis,
)
from packages.core.contracts import MetricsImportRequest


def _write_xlsx(path: Path, headers: list[str], rows: list[list[str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path


def _archive_file(
    root: Path,
    *,
    date: str,
    source_page: str,
    run_id: str,
    name: str,
    headers: list[str],
    rows: list[list[str]],
) -> Path:
    target = root / "raw" / date / source_page / run_id / name
    return _write_xlsx(target, headers, rows)


# --- normalizer unit tests -------------------------------------------------


def test_video_analysis_normalizer_computes_proxies() -> None:
    record = normalize_video_analysis(
        {
            "视频标题": "demo",
            "素材ID": "mat-1",
            "消耗": "1,000元",
            "转化数": "5",
            "私信留资数": "2",
        }
    )
    assert record.source_page == "video_analysis"
    assert record.external_ref == "mat-1"
    assert record.metrics["cost"] == 1000.0
    assert record.metrics["video_cpl"] == 500.0
    assert record.metrics["video_cpa"] == 200.0


def test_localpush_account_and_unit_use_distinct_lead_fields() -> None:
    account = normalize_localpush_account(
        {"账户ID": "acc-1", "消耗(元)": "500", "私信留资数": "10"}
    )
    unit = normalize_localpush_unit(
        {"单元ID": "u-1", "消耗(元)": "300", "线索留资数": "6"}
    )
    assert account.metrics["cpl"] == 50.0
    assert unit.metrics["cpl"] == 50.0
    assert unit.metrics["lead_count"] == 6.0


def test_comment_content_normalizer_keeps_text_and_engagement() -> None:
    record = normalize_comment_content(
        {"评论内容": "nice", "点赞数": "7", "关联视频素材": "mat-9"}
    )
    assert record.metrics["like_count"] == 7.0
    assert record.attributes["comment_text"] == "nice"
    assert record.external_ref == "mat-9"


# --- ingest + dedupe tests -------------------------------------------------


def test_import_archived_xlsx_builds_metrics_request(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    xlsx = _archive_file(
        root,
        date="2026-06-13",
        source_page="video_analysis",
        run_id="run_1",
        name="export.xlsx",
        headers=["视频标题", "素材ID", "消耗", "转化数", "私信留资数"],
        rows=[["v1", "mat-1", "1000", "5", "2"], ["v2", "mat-2", "200", "1", "1"]],
    )
    archive = default_archive(root)

    result = import_archived_xlsx(xlsx, archive=archive, archive_root=root)

    assert result.status == "ingested"
    assert result.new_row_count == 2
    assert isinstance(result.import_request, MetricsImportRequest)
    metric_names = {row["metric_name"] for row in result.import_request.rows}
    assert "video_cpl" in metric_names
    assert all(row["source"] == "oceanengine_rpa" for row in result.import_request.rows)
    assert all(row["external_ref"] in {"mat-1", "mat-2"} for row in result.import_request.rows)


def test_reimport_same_file_is_full_skip(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    xlsx = _archive_file(
        root,
        date="2026-06-13",
        source_page="localpush_unit",
        run_id="run_1",
        name="export.xlsx",
        headers=["单元ID", "消耗(元)", "线索留资数"],
        rows=[["u-1", "300", "6"]],
    )
    archive = default_archive(root)

    first = import_archived_xlsx(xlsx, archive=archive, archive_root=root)
    assert first.status == "ingested"
    assert first.new_row_count == 1

    second = import_archived_xlsx(xlsx, archive=archive, archive_root=root)
    assert second.status == "skipped_duplicate_file"
    assert second.import_request is None
    assert second.new_row_count == 0


def test_partial_reimport_emits_only_new_rows(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    archive = default_archive(root)

    first_path = _archive_file(
        root,
        date="2026-06-13",
        source_page="localpush_account",
        run_id="run_1",
        name="export.xlsx",
        headers=["账户ID", "消耗(元)", "私信留资数"],
        rows=[["acc-1", "500", "10"]],
    )
    first = import_archived_xlsx(first_path, archive=archive, archive_root=root)
    assert first.new_row_count == 1

    # A later export of the same page that contains the old row plus a new one.
    grown_path = _archive_file(
        root,
        date="2026-06-14",
        source_page="localpush_account",
        run_id="run_2",
        name="export.xlsx",
        headers=["账户ID", "消耗(元)", "私信留资数"],
        rows=[["acc-1", "500", "10"], ["acc-2", "800", "20"]],
    )
    grown = import_archived_xlsx(grown_path, archive=archive, archive_root=root)
    assert grown.status == "ingested"
    assert grown.new_row_count == 1
    assert grown.skipped_row_count == 1
    assert {row["external_ref"] for row in grown.import_request.rows} == {"acc-2"}


def test_import_archive_tree_walks_all_pages(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    _archive_file(
        root,
        date="2026-06-13",
        source_page="video_analysis",
        run_id="run_1",
        name="a.xlsx",
        headers=["视频标题", "素材ID", "消耗"],
        rows=[["v1", "mat-1", "100"]],
    )
    _archive_file(
        root,
        date="2026-06-13",
        source_page="comment_content",
        run_id="run_2",
        name="b.xlsx",
        headers=["评论内容", "点赞数"],
        rows=[["hi", "3"]],
    )
    archive = default_archive(root)

    summary = import_archive_tree(root, archive=archive)

    assert summary.files_seen == 2
    assert summary.files_ingested == 2
    pages = {result.source_page for result in summary.results}
    assert pages == {"video_analysis", "comment_content"}

    # Re-running the whole tree is a full skip.
    rerun = import_archive_tree(root, archive=archive)
    assert rerun.files_seen == 2
    assert rerun.files_skipped == 2
    assert rerun.files_ingested == 0


def test_dry_run_does_not_persist_dedupe_state(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    xlsx = _archive_file(
        root,
        date="2026-06-13",
        source_page="video_analysis",
        run_id="run_1",
        name="export.xlsx",
        headers=["视频标题", "素材ID", "消耗"],
        rows=[["v1", "mat-1", "100"]],
    )
    archive = default_archive(root)

    dry = import_archived_xlsx(xlsx, archive=archive, archive_root=root, dry_run=True)
    assert dry.status == "ingested"
    assert dry.import_request is not None and dry.import_request.dry_run is True

    # Because dry-run did not record state, a real import still sees the row.
    real = import_archived_xlsx(xlsx, archive=archive, archive_root=root)
    assert real.status == "ingested"
    assert real.new_row_count == 1


def test_infer_source_page_rejects_bad_layout(tmp_path: Path) -> None:
    root = tmp_path / "archive"
    bad = root / "raw" / "2026-06-13" / "video_analysis" / "loose.xlsx"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not-a-real-xlsx")
    archive = default_archive(root)
    with pytest.raises(ValueError):
        import_archived_xlsx(bad, archive=archive, archive_root=root)
