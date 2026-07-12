"""Run-scoped provider-call keys are stable across infrastructure retries and
distinct across a node's repair-loop attempts (issue #193 acceptance), and the
async long-task nodes carry the upgraded activity retry policy.
"""

from __future__ import annotations

from types import SimpleNamespace

from packages.core.provider_idempotency import is_provider_call_idempotency_key
from packages.production.pipeline._node_context import NodeContext
from packages.production.pipeline.digital_human import (
    _INFRA_RETRY_NODES,
    _PROVIDER_SIDE_EFFECT_NODES,
    _node_retry_policy,
    template_for,
)


def _ctx(*, node_run_id: str, node_id: str = "TTS", manifest: str = "m1", run_id: str = "run_1") -> NodeContext:
    return NodeContext(
        adapter=None,  # unused by the key helper
        run=SimpleNamespace(id=run_id),
        node_run=SimpleNamespace(id=node_run_id, node_id=node_id, input_manifest_hash=manifest),
        state=None,
    )


def test_key_is_stable_across_node_run_id_and_scheme_prefixed():
    key_a = _ctx(node_run_id="nr_a").provider_call_idempotency_key(
        logical_call_slot="tts", provider_profile_id="p1"
    )
    key_b = _ctx(node_run_id="nr_b").provider_call_idempotency_key(
        logical_call_slot="tts", provider_profile_id="p1"
    )
    assert key_a == key_b
    assert is_provider_call_idempotency_key(key_a)
    # The node_run.id must never leak into the key, else an activity re-run mints a
    # fresh identity and re-submits.
    assert "nr_a" not in key_a and "nr_b" not in key_a


def test_repair_loop_attempts_take_distinct_keys():
    ctx = _ctx(node_run_id="nr_a", node_id="MediaSelectionAgentPlanning")
    attempt_0 = ctx.provider_call_idempotency_key(
        logical_call_slot="media_selection_agent:attempt-0", provider_profile_id="p1"
    )
    attempt_1 = ctx.provider_call_idempotency_key(
        logical_call_slot="media_selection_agent:attempt-1", provider_profile_id="p1"
    )
    assert attempt_0 != attempt_1


def test_profile_and_manifest_are_key_coordinates():
    base = _ctx(node_run_id="nr_a")
    key = base.provider_call_idempotency_key(logical_call_slot="tts", provider_profile_id="p1")
    other_profile = base.provider_call_idempotency_key(
        logical_call_slot="tts", provider_profile_id="p2"
    )
    other_manifest = _ctx(node_run_id="nr_a", manifest="m2").provider_call_idempotency_key(
        logical_call_slot="tts", provider_profile_id="p1"
    )
    assert key != other_profile
    assert key != other_manifest


def test_canonical_node_alias_folds_into_same_key():
    # A historical node id and its active semantic name must produce one key so a
    # resumed run recovers the same durable identity.
    legacy = _ctx(node_run_id="nr_a", node_id="TimelinePlanning").provider_call_idempotency_key(
        logical_call_slot="slot", provider_profile_id="p1"
    )
    active = _ctx(
        node_run_id="nr_b", node_id="TimelineAssemblyValidation"
    ).provider_call_idempotency_key(logical_call_slot="slot", provider_profile_id="p1")
    assert legacy == active


def test_every_paid_node_is_retried_and_free_nodes_are_not():
    # A node that spends money must survive a worker crash: the durable key lets the
    # re-run poll or replay the call it already paid for. A node that spends nothing has
    # nothing to recover, so it keeps the single-attempt default.
    assert _INFRA_RETRY_NODES == _PROVIDER_SIDE_EFFECT_NODES
    for node_id in _INFRA_RETRY_NODES:
        assert _node_retry_policy(node_id).max_attempts == 3
    # MaterialPackPlanning keeps its own 3-attempt validation-retry policy.
    assert _node_retry_policy("MaterialPackPlanning").max_attempts == 3
    assert _node_retry_policy("RenderFinalTimeline").max_attempts == 1
    assert _node_retry_policy("ValidateRequest").max_attempts == 1


def test_infra_retry_backoff_outlasts_the_crash_it_recovers_from():
    # Re-entering seconds after a heartbeat timeout burns attempts against a durable row
    # the Gateway has not yet been able to judge dead.
    assert _node_retry_policy("LipSync").backoff_seconds >= 60


def test_template_specs_reflect_infra_retry_policy():
    main = {spec.node_id: spec for spec in template_for("digital_human_v2").nodes}
    assert main["LipSync"].retry_policy.max_attempts == 3
    assert main["NarrationAlignment"].retry_policy.max_attempts == 3
    assert main["TTS"].retry_policy.max_attempts == 3
    assert main["RenderFinalTimeline"].retry_policy.max_attempts == 1
    seedance = {spec.node_id: spec for spec in template_for("seedance_t2v_v1").nodes}
    assert seedance["SeedanceGenerateVideo"].retry_policy.max_attempts == 3
