from argon2 import PasswordHasher

from packages.core.storage.database import (
    CaseRow,
    MediaAssetRow,
    OpsAlertEventRow,
    PromptBindingRow,
    PromptTemplateRow,
    PromptVersionRow,
    ProviderCapabilityRow,
    ProviderPriceCatalogRow,
    ProviderPriceItemRow,
    ProviderProfileRow,
    RegistrationCodeRow,
    UserRow,
    VoiceProfileRow,
)
from packages.core.storage.seed import seed_rows
from packages.core.storage.seed import seed_database


def test_seed_rows_cover_local_operational_baseline():
    rows = seed_rows()
    row_types = {type(row) for row in rows}
    assert {
        UserRow,
        RegistrationCodeRow,
        CaseRow,
        MediaAssetRow,
        VoiceProfileRow,
        ProviderProfileRow,
        ProviderCapabilityRow,
        PromptTemplateRow,
        PromptVersionRow,
        PromptBindingRow,
        ProviderPriceCatalogRow,
        ProviderPriceItemRow,
        OpsAlertEventRow,
    } <= row_types


def test_seed_users_store_argon2id_password_hashes():
    users = [row for row in seed_rows() if isinstance(row, UserRow)]
    admin = next(row for row in users if row.id == "usr_admin")
    assert admin.password_hash.startswith("$argon2id$")
    assert PasswordHasher().verify(admin.password_hash, "local-admin")


def test_seed_rows_can_skip_local_auth_bootstrap_rows():
    rows = seed_rows(include_local_auth_seed=False)

    user_ids = {row.id for row in rows if isinstance(row, UserRow)}
    registration_code_ids = {row.id for row in rows if isinstance(row, RegistrationCodeRow)}
    demo_case = next(row for row in rows if isinstance(row, CaseRow) and row.id == "case_demo")

    assert "usr_admin" not in user_ids
    assert "usr_viewer" not in user_ids
    assert "reg_seed_local_admin" not in registration_code_ids
    assert demo_case.owner_user_id is None


def test_seed_provider_profiles_and_prompt_binding_are_ready_for_workflow():
    rows = seed_rows()
    profile_ids = {row.id for row in rows if isinstance(row, ProviderProfileRow)}
    prompt_versions = {row.id for row in rows if isinstance(row, PromptVersionRow)}
    bindings = [row for row in rows if isinstance(row, PromptBindingRow)]
    assert {
        "sandbox.tts.default",
        "runninghub.heygem.default",
        "sandbox.llm.default",
        "sandbox.embedding.default",
    } <= profile_ids
    assert {
        "minimax.tts.prod",
        "dashscope.asr.prod",
        "dashscope.vlm.prod",
        "dashscope.llm.prod",
        "dashscope.multimodal_embedding.prod",
        "runninghub.heygem.prod",
        "volcengine.seedream.prod",
    } <= profile_ids
    seedream_profile = next(row for row in rows if row.id == "volcengine.seedream.prod")
    assert seedream_profile.default_options["size"] == "1440x2560"
    assert "prompt_creative_intent_v1" in prompt_versions
    assert "prompt_creative_intent_v2" in prompt_versions
    assert "prompt_case_agent_script_v1" in prompt_versions
    assert "prompt_vlm_annotation_v1" in prompt_versions
    assert "prompt_window_query_v1" in prompt_versions
    assert any(row.node_id == "ResolveCreativeIntent" for row in bindings)
    assert any(row.node_id == "CaseAgentScriptGenerate" for row in bindings)
    assert any(row.node_id == "MediaAssetAnnotation" for row in bindings)
    assert any(row.node_id == "WindowQueryPlanning" for row in bindings)


class _FakeSeedSession:
    def __init__(self, existing):
        self.rows = {(type(row), row.id): row for row in existing}
        self.committed = False

    def get(self, row_type, row_id):
        return self.rows.get((row_type, row_id))

    def add(self, row):
        self.rows[(type(row), row.id)] = row

    def commit(self):
        self.committed = True


def test_seed_database_syncs_bgm_agent_boundary_contract():
    current = next(
        row
        for row in seed_rows()
        if isinstance(row, PromptVersionRow) and row.id == "prompt_bgm_agent_v1"
    )
    stale = PromptVersionRow(
        id="prompt_bgm_agent_v1",
        prompt_template_id="prompt_bgm_agent",
        content="{script}\n{bgm_candidates}\n可以顺便输出字幕建议",
        status="published",
    )
    session = _FakeSeedSession([stale])

    inserted = seed_database(session, rows=[current])

    assert inserted == 0
    assert session.committed is True
    assert stale.content == current.content
    assert "只能输出 bgm_id 和 analysis" in stale.content
    assert "字幕、强调、字体、颜色、坐标、时间线和音效不属于你的职责" in stale.content


def test_seed_database_preserves_published_creative_intent_v1():
    current_v1 = next(
        row
        for row in seed_rows()
        if isinstance(row, PromptVersionRow) and row.id == "prompt_creative_intent_v1"
    )
    stale = PromptVersionRow(
        id="prompt_creative_intent_v1",
        prompt_template_id="prompt_creative_intent",
        content="historical immutable prompt",
        status="published",
    )
    session = _FakeSeedSession([stale])

    inserted = seed_database(session, rows=[current_v1])

    assert inserted == 0
    assert stale.content == "historical immutable prompt"
