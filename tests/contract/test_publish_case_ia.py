from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_global_publish_center_navigation_is_removed() -> None:
    app_shell = _read("apps/web/src/components/AppShell.tsx")
    overview_panel = _read("apps/web/src/components/overview/OverviewSidePanel.tsx")

    assert 'label: "发布中心"' not in app_shell
    assert "routes.publishCenter()" not in app_shell
    assert 'label: "发布中心"' not in overview_panel
    assert "routes.publishCenter()" not in overview_panel


def test_case_publish_page_queries_batches_for_selected_case() -> None:
    publish_page = _read("apps/web/src/pages/publish/PublishCenterPage.tsx")

    assert 'queryKey: ["publish-center", "batches", selectedCaseId]' in publish_page
    assert "api.publishing.batches({ limit: 80, case_id: selectedCaseId })" in publish_page
