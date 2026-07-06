from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_run_detail_places_jianying_export_before_runtime_diagnostics() -> None:
    modal = _read("apps/web/src/components/runs/RunDetailModal.tsx")

    action_index = modal.index("<EditorHandoffActions")
    node_timeline_index = modal.index("节点时间线")
    artifact_list_index = modal.index("产物清单")

    assert action_index < node_timeline_index < artifact_list_index
    runtime_sections = modal[node_timeline_index:]
    assert "EditorHandoffActions" not in runtime_sections
    assert "交接包" not in runtime_sections
    assert '<h4 className="text-base font-semibold text-text-primary">剪映工程包</h4>' not in modal
    assert "finishedVideoId={finishedVideo?.id}" in modal


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
