"""G4/R6/F-#2 in-memory coverage: case profile fields, list counts, import brief.

These exercise the in-memory backend (CUTAGENT_STORAGE_BACKEND=memory, set by
tests/conftest.py) through the public API so the contract field names and the
service mappings are validated end-to-end. The SQLAlchemy path is covered by the
gated integration tests in tests/integration/test_sqlalchemy_cases.py and
test_sqlalchemy_case_learning.py.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.services.cases import _with_counts
from packages.ai.prompts.registry import case_prompt_variables
from packages.core import contracts as c


def test_quality_count_only_counts_qcd_videos_not_pending():
    # R6: the "质检" (quality_count) column counts finished videos that have
    # actually been quality-checked — i.e. a terminal qc_status — NOT every
    # finished video. ``qc_status`` is never empty (defaults to "pending"), so a
    # "has any status" filter would silently count everything.
    case = c.CaseDetail(id="case_q", name="Quality Case")
    repo = SimpleNamespace(
        media_assets={},
        scripts={},
        finished_videos={
            "fv_pending": SimpleNamespace(case_id="case_q", qc_status="pending"),
            "fv_passed": SimpleNamespace(case_id="case_q", qc_status="passed"),
            "fv_failed": SimpleNamespace(case_id="case_q", qc_status="failed"),
            "fv_warning": SimpleNamespace(case_id="case_q", qc_status="warning"),
            "fv_other_case": SimpleNamespace(case_id="case_other", qc_status="passed"),
        },
    )
    item = _with_counts(repo, case)
    # passed + failed + warning for this case = 3; pending and the other case excluded.
    assert item.quality_count == 3


def test_material_count_includes_unified_video_assets():
    case = c.CaseDetail(id="case_video_count", name="Video Count Case")
    repo = SimpleNamespace(
        media_assets={
            "asset_video": SimpleNamespace(case_id="case_video_count", kind="video"),
            "asset_video_b": SimpleNamespace(case_id="case_video_count", kind="video"),
            "asset_voice": SimpleNamespace(case_id="case_video_count", kind="voice"),
            "asset_other_case": SimpleNamespace(case_id="case_other", kind="video"),
        },
        voices={
            "voice_bound": SimpleNamespace(case_ids=["case_video_count"]),
            "voice_other": SimpleNamespace(case_ids=["case_other"]),
        },
        scripts={},
        finished_videos={},
    )

    item = _with_counts(repo, case)

    assert item.material_count == 2
    assert item.voice_count == 2


def test_case_count_material_kind_allowlists_include_unified_video_assets():
    from apps.api.services.cases import MATERIAL_ASSET_KINDS as API_MATERIAL_ASSET_KINDS
    from packages.core.contracts import CASE_MATERIAL_ASSET_KINDS
    from packages.creative.cases.sqlalchemy_repository import (
        MATERIAL_ASSET_KINDS as SQLA_MATERIAL_ASSET_KINDS,
    )

    assert API_MATERIAL_ASSET_KINDS is CASE_MATERIAL_ASSET_KINDS
    assert SQLA_MATERIAL_ASSET_KINDS is CASE_MATERIAL_ASSET_KINDS
    assert "video" in API_MATERIAL_ASSET_KINDS
    assert "video" in SQLA_MATERIAL_ASSET_KINDS


def _login_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


PROFILE_PAYLOAD = {
    "name": "Profile Case",
    "industry": "retail",
    "product": "Widget",
    "target_audience": "operators",
    "description": "Seeded profile case.",
    "key_selling_points": ["fast", "cheap"],
    "ip_persona": "friendly expert",
    "brand_voice": "warm and direct",
    "strategy_tags": ["promo", "q3"],
    "brand_keywords": ["acme"],
    "competitor_names": ["globex"],
}


def test_create_case_round_trips_all_profile_fields() -> None:
    with TestClient(create_app()) as client:
        _login_admin(client)
        created = client.post("/api/cases", json=PROFILE_PAYLOAD)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["key_selling_points"] == ["fast", "cheap"]
        assert body["ip_persona"] == "friendly expert"
        assert body["brand_voice"] == "warm and direct"
        assert body["strategy_tags"] == ["promo", "q3"]
        assert body["brand_keywords"] == ["acme"]
        assert body["competitor_names"] == ["globex"]
        assert body["industry"] == "retail"


def test_patch_case_updates_profile_fields() -> None:
    with TestClient(create_app()) as client:
        _login_admin(client)
        created = client.post("/api/cases", json={"name": "Patch Profile Case"})
        assert created.status_code == 201, created.text
        case_id = created.json()["id"]

        patched = client.patch(
            f"/api/cases/{case_id}",
            json={
                "industry": "education",
                "key_selling_points": ["x", "y"],
                "ip_persona": "mentor",
                "brand_voice": "calm",
                "strategy_tags": ["evergreen"],
                "brand_keywords": ["kw"],
                "competitor_names": ["rival"],
            },
        )
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["industry"] == "education"
        assert body["key_selling_points"] == ["x", "y"]
        assert body["ip_persona"] == "mentor"
        assert body["brand_voice"] == "calm"
        assert body["strategy_tags"] == ["evergreen"]
        assert body["brand_keywords"] == ["kw"]
        assert body["competitor_names"] == ["rival"]

        detail = client.get(f"/api/cases/{case_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["industry"] == "education"
        assert detail.json()["key_selling_points"] == ["x", "y"]


def test_list_cases_industry_filter_and_counts() -> None:
    with TestClient(create_app()) as client:
        _login_admin(client)
        created = client.post(
            "/api/cases",
            json={"name": "Filterable Case", "industry": "fintech"},
        )
        assert created.status_code == 201, created.text
        case_id = created.json()["id"]

        matched = client.get("/api/cases", params={"industry": "fintech"})
        assert matched.status_code == 200, matched.text
        assert any(item["id"] == case_id for item in matched.json()["items"])

        unmatched = client.get("/api/cases", params={"industry": "no-such-industry"})
        assert unmatched.status_code == 200, unmatched.text
        assert all(item["id"] != case_id for item in unmatched.json()["items"])

        # Every list item exposes the R6 count fields with safe defaults.
        for item in matched.json()["items"]:
            assert "material_count" in item
            assert "script_count" in item
            assert "voice_count" in item
            assert "quality_count" in item


def test_seeded_demo_case_reports_material_count_from_assets() -> None:
    # The seed creates 6 reusable media assets for case_demo: 3 distinct portrait
    # sources (asset-level portrait uniqueness, issue #102, needs >1 for a multi-segment
    # main track) plus broll/bgm/font, so material_count must be 6 (R6 count semantics).
    with TestClient(create_app()) as client:
        _login_admin(client)
        listing = client.get("/api/cases", params={"search": "Demo", "limit": 200})
        assert listing.status_code == 200, listing.text
        demo = next(item for item in listing.json()["items"] if item["id"] == "case_demo")
        assert demo["material_count"] == 6
        assert demo["voice_count"] == 0  # seeded voice is a VoiceProfile, not a media asset
        assert demo["script_count"] == 0
        assert demo["quality_count"] == 0


def test_case_prompt_variables_bridges_contract_to_template_vocabulary() -> None:
    case = c.CaseDetail(
        id="case_probe",
        name="Acme Co",
        product="Widget",
        industry="retail",
        target_audience="ops",
        description="desc",
        key_selling_points=["fast", "cheap"],
        ip_persona="friendly",
        brand_voice="warm",
        strategy_tags=["promo", "q3"],
        brand_keywords=["acme"],
        competitor_names=["globex"],
    )
    variables = case_prompt_variables(case)
    assert variables["case_name"] == "Acme Co"
    assert variables["product_name"] == "Widget"
    assert variables["industry"] == "retail"
    assert variables["target_audience"] == "ops"
    assert variables["ip_persona"] == "friendly"
    assert variables["brand_voice"] == "warm"
    # List fields are joined, not Python-list reprs.
    assert variables["key_selling_points"] == "fast, cheap"
    assert variables["tags"] == "promo, q3"
    assert variables["description"] == "desc"
    # brand_keywords / competitor_names have no template var today.
    assert "brand_keywords" not in variables
    assert "competitor_names" not in variables
