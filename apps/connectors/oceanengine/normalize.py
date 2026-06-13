"""Normalize raw OceanEngine XLSX rows into typed :class:`OceanEngineMetricRow`.

Each RPA export page (``video_analysis``, ``localpush_account``,
``localpush_unit``, ``comment_content``) has its own Chinese column layout.
The normalizers below collapse those layouts into a uniform shape: a stable
``external_ref``, a numeric ``metrics`` map keyed by canonical English names, a
textual ``attributes`` map, the untouched ``raw`` row, and a content
``row_fingerprint`` used for dedupe. Header aliases are tolerated via
:func:`packages...metrics.pick` so minor export variations do not lose data.
"""

from __future__ import annotations

from typing import Callable

from packages.core.contracts import OceanEngineMetricRow, OceanEngineSourcePage

from apps.connectors.oceanengine.metrics import hash_row, parse_int, parse_number, pick, safe_divide


def _numeric(metrics: dict[str, float], key: str, value: float | int | None) -> None:
    if value is not None:
        metrics[key] = float(value)


def normalize_video_analysis(row: dict[str, str]) -> OceanEngineMetricRow:
    """Per-video creative ROI proxy metrics (consumption / leads / conversions)."""

    cost = parse_number(pick(row, "消耗", "消耗(元)"))
    conversions = parse_int(pick(row, "转化数"))
    leads = parse_int(pick(row, "私信留资数"))
    impressions = parse_int(pick(row, "展示数", "展示次数"))
    clicks = parse_int(pick(row, "点击数", "点击次数"))

    metrics: dict[str, float] = {}
    _numeric(metrics, "cost", cost)
    _numeric(metrics, "impressions", impressions)
    _numeric(metrics, "clicks", clicks)
    _numeric(metrics, "conversions", conversions)
    _numeric(metrics, "private_message_leads", leads)
    _numeric(metrics, "video_cpl", safe_divide(cost, leads))
    _numeric(metrics, "video_cpa", safe_divide(cost, conversions))

    material_id = pick(row, "素材ID", "素材id", "关联视频素材")
    video_id = pick(row, "视频id", "视频ID")
    attributes = {k: v for k, v in {"material_id": material_id, "video_id": video_id}.items() if v}

    return OceanEngineMetricRow(
        source_page="video_analysis",
        external_ref=material_id or video_id or None,
        title=pick(row, "视频标题") or None,
        metrics=metrics,
        attributes=attributes,
        raw=dict(row),
        row_fingerprint=hash_row(row),
    )


def normalize_localpush_account(row: dict[str, str]) -> OceanEngineMetricRow:
    """Account-level local-push (本地推) spend metrics."""

    cost = parse_number(pick(row, "消耗(元)", "消耗"))
    impressions = parse_int(pick(row, "展示次数", "展示数"))
    clicks = parse_int(pick(row, "点击次数", "点击数"))
    conversions = parse_int(pick(row, "转化数"))
    leads = parse_int(pick(row, "私信留资数"))

    metrics: dict[str, float] = {}
    _numeric(metrics, "cost", cost)
    _numeric(metrics, "impressions", impressions)
    _numeric(metrics, "clicks", clicks)
    _numeric(metrics, "conversions", conversions)
    _numeric(metrics, "private_message_leads", leads)
    _numeric(metrics, "cpl", safe_divide(cost, leads))
    _numeric(metrics, "cpa", safe_divide(cost, conversions))

    account_id = pick(row, "账户ID", "账户id")
    account_name = pick(row, "账户信息", "账户名称")
    attributes = {k: v for k, v in {"account_id": account_id, "account_name": account_name}.items() if v}

    return OceanEngineMetricRow(
        source_page="localpush_account",
        external_ref=account_id or None,
        title=account_name or None,
        metrics=metrics,
        attributes=attributes,
        raw=dict(row),
        row_fingerprint=hash_row(row),
    )


def normalize_localpush_unit(row: dict[str, str]) -> OceanEngineMetricRow:
    """Unit-level local-push (本地推) spend metrics."""

    cost = parse_number(pick(row, "消耗(元)", "消耗"))
    impressions = parse_int(pick(row, "展示次数", "展示数"))
    clicks = parse_int(pick(row, "点击次数", "点击数"))
    conversions = parse_int(pick(row, "转化数"))
    lead_count = parse_int(pick(row, "线索留资数"))

    metrics: dict[str, float] = {}
    _numeric(metrics, "cost", cost)
    _numeric(metrics, "impressions", impressions)
    _numeric(metrics, "clicks", clicks)
    _numeric(metrics, "conversions", conversions)
    _numeric(metrics, "lead_count", lead_count)
    _numeric(metrics, "cpl", safe_divide(cost, lead_count))
    _numeric(metrics, "cpa", safe_divide(cost, conversions))

    unit_id = pick(row, "单元ID", "单元id")
    unit_name = pick(row, "单元信息", "单元名称")
    attributes = {k: v for k, v in {"unit_id": unit_id, "unit_name": unit_name}.items() if v}

    return OceanEngineMetricRow(
        source_page="localpush_unit",
        external_ref=unit_id or None,
        title=unit_name or None,
        metrics=metrics,
        attributes=attributes,
        raw=dict(row),
        row_fingerprint=hash_row(row),
    )


def normalize_comment_content(row: dict[str, str]) -> OceanEngineMetricRow:
    """Comment records (no spend metrics; engagement counts + text context)."""

    like_count = parse_int(pick(row, "点赞数"))
    reply_count = parse_int(pick(row, "相关回复数"))

    metrics: dict[str, float] = {}
    _numeric(metrics, "like_count", like_count)
    _numeric(metrics, "related_reply_count", reply_count)

    video_material_id = pick(row, "关联视频素材")
    attributes = {
        k: v
        for k, v in {
            "comment_text": pick(row, "评论内容"),
            "comment_user": pick(row, "评论用户"),
            "comment_time": pick(row, "评论时间"),
            "reply_status": pick(row, "回复状态"),
            "source_unit": pick(row, "评论来源单元"),
            "video_material_id": video_material_id,
        }.items()
        if v
    }

    return OceanEngineMetricRow(
        source_page="comment_content",
        external_ref=video_material_id or None,
        title=pick(row, "视频标题") or None,
        metrics=metrics,
        attributes=attributes,
        raw=dict(row),
        row_fingerprint=hash_row(row),
    )


NORMALIZERS: dict[OceanEngineSourcePage, Callable[[dict[str, str]], OceanEngineMetricRow]] = {
    "video_analysis": normalize_video_analysis,
    "localpush_account": normalize_localpush_account,
    "localpush_unit": normalize_localpush_unit,
    "comment_content": normalize_comment_content,
}


def normalize_rows(
    source_page: OceanEngineSourcePage, rows: list[dict[str, str]]
) -> list[OceanEngineMetricRow]:
    """Normalize every raw row for a given ``source_page``."""

    normalizer = NORMALIZERS.get(source_page)
    if normalizer is None:
        raise ValueError(f"unsupported OceanEngine source_page: {source_page}")
    return [normalizer(row) for row in rows]
