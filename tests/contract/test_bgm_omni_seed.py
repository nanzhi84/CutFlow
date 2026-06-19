from __future__ import annotations

from packages.core.storage import provider_seed
from packages.core.storage.repository import Repository


def test_omni_profile_capability_and_prices_seeded():
    repo = Repository()
    provider_seed.seed_real_provider_configuration(repo)

    profiles = [
        profile
        for profile in repo.provider_profiles.values()
        if profile.capability == "audio.understanding"
    ]
    assert profiles, "expected an audio.understanding profile"
    profile = profiles[0]
    assert profile.id == "dashscope.omni.prod"
    assert profile.provider_id == "dashscope.omni"
    assert profile.model_id == "qwen3.5-omni-plus"
    assert profile.enabled
    assert profile.secret_ref == "dashscope_prod.secret"

    capabilities = [
        capability
        for capability in repo.provider_capabilities.values()
        if capability.capability == "audio.understanding"
    ]
    assert capabilities
    assert capabilities[0].provider_id == "dashscope.omni"
    assert capabilities[0].model_id == "qwen3.5-omni-plus"

    price_items = [
        item
        for item in repo.price_items.values()
        if item.provider_id == "dashscope.omni"
        and item.model_id == "qwen3.5-omni-plus"
        and item.capability_id == "audio.understanding"
    ]
    assert {item.unit for item in price_items} == {"input_token", "output_token"}
