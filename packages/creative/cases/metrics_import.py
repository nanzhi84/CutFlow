"""Metrics import matching policy (Spec §25.4 / §25.1).

Resolves each import row to a ``publish_record_id`` using a *deterministic* key
selected by ``matching_policy``. Title + publish-time guessing is forbidden
unless the policy is ``strict_manual`` (which also writes a warning).

The matcher is storage-agnostic: callers pass a list of ``PublishRecordIndex``
(the identifiers known for each publish record) and the matcher returns, per row,
the resolved publish_record_id + the canonical observation fields, or ``None``
when the row cannot be matched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from packages.core.contracts import MetricsMatchingPolicy

# Canonical numeric metric names recognized on structured rows (§8.3).
_CANONICAL_METRIC_FIELDS = (
    "impressions",
    "views",
    "avg_watch_sec",
    "completion_rate",
    "like_rate",
    "comment_rate",
    "share_rate",
    "follow_rate",
    "conversion_count",
    "conversion_rate",
)

# Which row keys each policy may use to resolve a publish record.
_POLICY_ROW_KEYS: dict[str, tuple[str, ...]] = {
    "external_post_id": ("external_post_id", "external_ref"),
    "platform_item_id": ("platform_item_id", "external_ref"),
    "published_url": ("published_url", "external_url"),
    "strict_manual": ("publish_record_id",),
}


@dataclass(frozen=True)
class PublishRecordIndex:
    """The set of identifiers a single publish record can be matched on."""

    publish_record_id: str
    video_version_id: str | None = None
    platform: str | None = None
    account_id: str | None = None
    external_post_id: str | None = None
    platform_item_id: str | None = None
    published_url: str | None = None


@dataclass
class MatchedRow:
    row_index: int
    publish_record_id: str
    video_version_id: str | None
    platform: str | None
    account_id: str | None
    metric_name: str
    metric_value: float
    canonical_metrics: dict[str, float]
    window: str | None


@dataclass
class UnmatchedRow:
    row_index: int
    reason: str
    raw: dict[str, Any]


@dataclass
class MatchResult:
    matched: list[MatchedRow]
    unmatched: list[UnmatchedRow]
    warnings: list[str]


def _index_lookup(
    records: list[PublishRecordIndex], policy: MetricsMatchingPolicy
) -> dict[str, PublishRecordIndex]:
    """Build a value->record lookup for the attribute the policy keys on."""
    attr = {
        "external_post_id": "external_post_id",
        "platform_item_id": "platform_item_id",
        "published_url": "published_url",
        "strict_manual": "publish_record_id",
    }[policy]
    lookup: dict[str, PublishRecordIndex] = {}
    for record in records:
        value = getattr(record, attr)
        if value:
            lookup.setdefault(str(value), record)
        # external_post_id / platform_item_id policies also fall back to the
        # publish_record_id itself so connector rows that carry the internal id
        # (or an external_ref equal to it) still resolve deterministically.
        if policy in {"external_post_id", "platform_item_id"}:
            lookup.setdefault(record.publish_record_id, record)
            if record.video_version_id:
                lookup.setdefault(record.video_version_id, record)
    return lookup


def _row_key(row: Mapping[str, Any], policy: MetricsMatchingPolicy) -> str | None:
    for key in _POLICY_ROW_KEYS[policy]:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _canonical_metrics(row: Mapping[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for field in _CANONICAL_METRIC_FIELDS:
        if field in row and row[field] not in (None, ""):
            try:
                out[field] = float(row[field])
            except (TypeError, ValueError):
                continue
    return out


def match_metrics_rows(
    rows: list[Any],
    *,
    policy: MetricsMatchingPolicy,
    records: list[PublishRecordIndex],
    default_platform: str | None = None,
    default_account_id: str | None = None,
) -> MatchResult:
    """Resolve import rows to publish records per the §25.4 matching policy."""
    lookup = _index_lookup(records, policy)
    matched: list[MatchedRow] = []
    unmatched: list[UnmatchedRow] = []
    warnings: list[str] = []

    if policy == "strict_manual":
        warnings.append(
            "matching_policy=strict_manual: rows are bound by operator-supplied "
            "publish_record_id without external-key verification."
        )

    # publish_record_id is the internal deterministic binding; an explicit one is
    # always honored (it is not a title/time guess) regardless of matching_policy.
    by_record_id = {record.publish_record_id: record for record in records}

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            unmatched.append(UnmatchedRow(index, "row_not_object", {"value": row}))
            continue

        record: PublishRecordIndex | None = None
        explicit_id = row.get("publish_record_id")
        if explicit_id not in (None, ""):
            record = by_record_id.get(str(explicit_id))
            if record is None:
                unmatched.append(UnmatchedRow(index, "publish_record_not_found", dict(row)))
                continue
        else:
            if policy == "strict_manual":
                # strict_manual binds only by operator-supplied publish_record_id.
                unmatched.append(UnmatchedRow(index, "publish_record_id_required", dict(row)))
                continue
            key = _row_key(row, policy)
            if key is not None:
                record = lookup.get(key)
            if record is None:
                unmatched.append(UnmatchedRow(index, "no_deterministic_match", dict(row)))
                continue

        canonical = _canonical_metrics(row)
        metric_name = str(row.get("metric_name", "views"))
        try:
            metric_value = float(row.get("metric_value", canonical.get(metric_name, 0)))
        except (TypeError, ValueError):
            metric_value = 0.0
        window = row.get("window")
        matched.append(
            MatchedRow(
                row_index=index,
                publish_record_id=record.publish_record_id,
                video_version_id=record.video_version_id,
                platform=record.platform or default_platform or row.get("platform"),
                account_id=record.account_id or default_account_id or row.get("account_id"),
                metric_name=metric_name,
                metric_value=metric_value,
                canonical_metrics=canonical,
                window=str(window) if window else None,
            )
        )

    return MatchResult(matched=matched, unmatched=unmatched, warnings=warnings)
