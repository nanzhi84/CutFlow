from __future__ import annotations

from types import SimpleNamespace

from packages.production.sqlalchemy_repository import _clip_embedding_record_from_row


def test_clip_embedding_row_mapper_preserves_index_contract():
    row = SimpleNamespace(
        clip_embedding_key="clipemb_test",
        asset_id="asset_broll",
        asset_revision="asset:asset_broll:v1:v1:test",
        clip_id="clip_1",
        source_start=1.0,
        source_end=3.0,
        source_frames_available=60,
        index_namespace="broll",
        embedding_scope="clip",
        embedding_input_type="video_clip",
        embedding_input_ref="asset_broll:clip_1:1.000000:3.000000",
        sample_policy={"policy_id": "test"},
        embedding_id="emb_1",
        embedding=[1, 0, 0],
        provider_profile_id="sandbox.embedding.default",
        embedding_model="qwen3-vl-embedding",
        embedding_dimension=1024,
        normalization="l2",
        instruct="video_clip_retrieval_v1",
        index_version="clip-vl-qwen3-v1",
    )

    record = _clip_embedding_record_from_row(row)

    assert record.clip_embedding_key == "clipemb_test"
    assert record.index_namespace == "broll"
    assert record.embedding == [1.0, 0.0, 0.0]
    assert record.embedding_model == "qwen3-vl-embedding"
    assert record.embedding_dimension == 1024
