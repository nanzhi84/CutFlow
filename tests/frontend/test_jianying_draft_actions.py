from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_run_detail_places_jianying_export_before_node_timeline() -> None:
    modal = _read("apps/web/src/components/runs/RunDetailModal.tsx")

    action_index = modal.index("<EditorHandoffActions")
    node_timeline_index = modal.index("节点时间线")

    assert action_index < node_timeline_index
    runtime_sections = modal[node_timeline_index:]
    assert "EditorHandoffActions" not in runtime_sections
    assert "交接包" not in runtime_sections
    assert '<h4 className="text-base font-semibold text-text-primary">剪映工程包</h4>' not in modal
    assert "finishedVideoId={finishedVideo?.id}" in modal
    assert "调试产物" not in modal
    assert "TechnicalArtifacts" not in modal


def test_run_detail_title_does_not_include_run_id() -> None:
    modal = _read("apps/web/src/components/runs/RunDetailModal.tsx")

    assert 'title="运行详情"' in modal
    assert "运行详情 ${" not in modal
    assert "shortId(card.runId)" not in modal


def test_run_detail_only_surfaces_lipsync_fallback_provider() -> None:
    modal = _read("apps/web/src/components/runs/RunDetailModal.tsx")

    assert 'providerName === "VideoReTalk"' in modal
    assert "if (!fallbackUsed) return null" in modal
    assert 'caption: `${providerName} 兜底生成`' in modal
    assert 'caption: fallbackUsed ? `${providerName} 兜底生成` : `${providerName} 生成`' not in modal


def test_run_detail_fallback_warning_copy_hides_internal_fields() -> None:
    modal = _read("apps/web/src/components/runs/RunDetailModal.tsx")
    pipeline = _read("apps/web/src/components/runs/NodePipeline.tsx")

    assert "原封面生成不可用，已改用" in modal
    assert "兜底自" not in modal
    assert "fallbackFrom.join" not in modal
    assert "compactDetail" not in modal
    assert 'badge.detail ? `：${badge.detail}` : ""' in pipeline
    assert "badge.count && badge.count > 0 ? ` ${badge.count}`" not in pipeline


def test_node_pipeline_tints_successful_nodes_with_warnings_as_degraded() -> None:
    pipeline = _read("apps/web/src/components/runs/NodePipeline.tsx")

    assert 'if (status === "succeeded" && issues > 0) return "degraded"' in pipeline
    assert "const itemVisualStatus = visualStatus(item.status, itemIssueCount)" in pipeline
    assert "className={chipClass(itemVisualStatus, selectedId === item.nodeId)}" in pipeline
    assert "{statusIcon(itemVisualStatus)}" in pipeline


def test_jianying_export_action_is_the_only_frontstage_editor_package() -> None:
    actions = _read("apps/web/src/components/editor-handoff/EditorHandoffActions.tsx")

    assert "createJianyingDraft" in actions
    assert "latestJianyingDraft" in actions
    assert "createEditorHandoff" not in actions
    assert "导出交接包" not in actions
    assert "编辑交接包" not in actions
    assert "下载剪映工程包" in actions
    assert "下载发布包" in actions
    assert "api.finishedVideos.download" in actions


def test_jianying_export_uses_signed_download_url_and_auto_downloads() -> None:
    actions = _read("apps/web/src/components/editor-handoff/EditorHandoffActions.tsx")

    assert "triggerDownload(value.download_url" in actions
    assert "packageResult?.download_url" in actions
    assert "href={packageResult" not in actions
    assert "href={videoUrl" not in actions


def test_jianying_action_bar_has_no_result_card_or_summary_title() -> None:
    actions = _read("apps/web/src/components/editor-handoff/EditorHandoffActions.tsx")

    assert "<details" not in actions
    assert "<summary" not in actions
    assert "rounded-lg border border-border/70 bg-white/70 p-3" not in actions
