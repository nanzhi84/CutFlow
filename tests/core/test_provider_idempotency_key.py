"""Unit coverage for the Run-scoped provider-call idempotency key helper (issue #193).

Pure functions over strings — no storage, so these never touch Postgres.
"""

from __future__ import annotations

from packages.core.provider_idempotency import (
    PROVIDER_CALL_KEY_SCHEME,
    build_provider_call_idempotency_key,
    is_provider_call_idempotency_key,
)


def _key(**overrides: str) -> str:
    base = dict(
        run_id="run_1",
        canonical_node_id="Tts",
        logical_call_slot="tts",
        provider_profile_id="profile_1",
        input_manifest_hash="manifest_1",
    )
    base.update(overrides)
    return build_provider_call_idempotency_key(**base)


def test_key_is_stable_regardless_of_node_run_or_attempt():
    # The formula excludes node_run.id / Temporal attempt entirely, so two calls with
    # the same Run coordinates collapse to one identity across infrastructure retries.
    assert _key() == _key()


def test_key_is_scheme_prefixed_and_recognised():
    key = _key()
    assert key.startswith(f"{PROVIDER_CALL_KEY_SCHEME}:")
    assert is_provider_call_idempotency_key(key)


def test_predicate_rejects_legacy_and_non_run_keys():
    # Legacy node keys and non-Run keys (annotation / BGM / clip embedding / publish)
    # never match the scheme, so the gateway keeps them on the transient path.
    assert not is_provider_call_idempotency_key(None)
    assert not is_provider_call_idempotency_key("run_1:node_1:tts")
    assert not is_provider_call_idempotency_key("vlm-anno-abc123")
    assert not is_provider_call_idempotency_key("clip-embedding:asset_1")


def test_distinct_slot_yields_distinct_key():
    # A node's repair loop must carry its attempt in the slot so successive calls get
    # different keys instead of colliding on the unique index / a succeeded first row.
    assert _key(logical_call_slot="agent:attempt-0") != _key(logical_call_slot="agent:attempt-1")


def test_each_identity_field_changes_the_key():
    baseline = _key()
    assert _key(run_id="run_2") != baseline
    assert _key(canonical_node_id="LipSync") != baseline
    assert _key(provider_profile_id="profile_2") != baseline
    assert _key(input_manifest_hash="manifest_2") != baseline
