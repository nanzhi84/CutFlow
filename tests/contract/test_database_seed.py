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


def test_seed_provider_profiles_and_prompt_binding_are_ready_for_workflow():
    rows = seed_rows()
    profile_ids = {row.id for row in rows if isinstance(row, ProviderProfileRow)}
    prompt_versions = {row.id for row in rows if isinstance(row, PromptVersionRow)}
    bindings = [row for row in rows if isinstance(row, PromptBindingRow)]
    assert {"sandbox.tts.default", "runninghub.heygem.default", "sandbox.llm.default"} <= profile_ids
    assert {
        "minimax.tts.prod",
        "dashscope.asr.prod",
        "dashscope.vlm.prod",
        "dashscope.llm.prod",
        "runninghub.heygem.prod",
    } <= profile_ids
    assert "prompt_creative_intent_v1" in prompt_versions
    assert "prompt_case_agent_script_v1" in prompt_versions
    assert "prompt_vlm_annotation_v1" in prompt_versions
    assert any(row.node_id == "ResolveCreativeIntent" for row in bindings)
    assert any(row.node_id == "CaseAgentScriptGenerate" for row in bindings)
    assert any(row.node_id == "MediaAssetAnnotation" for row in bindings)
