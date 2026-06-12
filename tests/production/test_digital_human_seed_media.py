from __future__ import annotations

from packages.ai.gateway import ProviderGateway
from packages.ai.prompts import PromptRegistry
from packages.core.storage.object_store import LocalObjectStore
from packages.core.storage.repository import Repository
from packages.production.pipeline.digital_human import build_digital_human_workflow


def test_seed_media_uses_stable_content_directories_across_fresh_repositories(
    monkeypatch,
    tmp_path,
):
    object_store = LocalObjectStore(tmp_path / "objects")
    monkeypatch.setattr("packages.core.storage.object_store._OBJECT_STORE", object_store)

    for _ in range(2):
        repository = Repository()
        build_digital_human_workflow(
            repository,
            provider_gateway=ProviderGateway(repository, object_store=object_store),
            prompt_registry=PromptRegistry(repository),
        )

    seed_root = tmp_path / "objects" / "seed-media"
    content_dirs = [path for path in seed_root.iterdir() if path.is_dir()]

    assert len(content_dirs) == 3
