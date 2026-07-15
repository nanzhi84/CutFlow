"""Job-scoped provider-call keys are stable across infrastructure retries AND across a
resume's new run (issue #193/#202 acceptance), stay distinct across a node's repair-loop
attempts, and the async long-task nodes carry the upgraded activity retry policy.
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


def _ctx(
    *,
    node_run_id: str,
    node_id: str = "TTS",
    manifest: str = "m1",
    run_id: str = "run_1",
    job_id: str = "job_1",
) -> NodeContext:
    return NodeContext(
        adapter=None,  # unused by the key helper
        run=SimpleNamespace(id=run_id, job_id=job_id),
        node_run=SimpleNamespace(id=node_run_id, node_id=node_id, input_manifest_hash=manifest),
        state=None,
    )


def _key(ctx: NodeContext, *, slot: str = "tts", profile: str = "p1") -> str:
    return ctx.provider_call_idempotency(
        logical_call_slot=slot, provider_profile_id=profile
    ).key


def test_key_is_stable_across_node_run_id_and_scheme_prefixed():
    key_a = _key(_ctx(node_run_id="nr_a"))
    key_b = _key(_ctx(node_run_id="nr_b"))
    assert key_a == key_b
    assert is_provider_call_idempotency_key(key_a)
    # The node_run.id must never leak into the key, else an activity re-run mints a
    # fresh identity and re-submits.
    assert "nr_a" not in key_a and "nr_b" not in key_a


def test_key_survives_the_new_run_a_resume_creates():
    # A resume re-drives the same job under a brand-new run id. If the run id were a key
    # coordinate, the resumed run would open a second durable identity and pay the vendor
    # again for a task it already bought.
    failed_run = _key(_ctx(node_run_id="nr_a", run_id="run_a"))
    resumed_run = _key(_ctx(node_run_id="nr_b", run_id="run_b"))
    assert failed_run == resumed_run
    assert "run_a" not in failed_run and "run_b" not in failed_run
    # A different job is a different piece of work and must never collide.
    assert _key(_ctx(node_run_id="nr_a", job_id="job_2")) != failed_run


def test_repair_loop_attempts_take_distinct_keys():
    ctx = _ctx(node_run_id="nr_a", node_id="MediaSelectionAgentPlanning")
    attempt_0 = _key(ctx, slot="media_selection_agent:attempt-0")
    attempt_1 = _key(ctx, slot="media_selection_agent:attempt-1")
    assert attempt_0 != attempt_1


def test_profile_and_manifest_are_key_coordinates():
    base = _ctx(node_run_id="nr_a")
    assert _key(base) != _key(base, profile="p2")
    assert _key(base) != _key(_ctx(node_run_id="nr_a", manifest="m2"))


def test_retired_node_id_does_not_alias_an_active_provider_key():
    retired = _key(_ctx(node_run_id="nr_a", node_id="TimelinePlanning"), slot="slot")
    active = _key(
        _ctx(node_run_id="nr_b", node_id="TimelineAssemblyValidation"), slot="slot"
    )
    assert retired != active


def test_fallback_key_follows_the_run_so_in_flight_tasks_keep_their_identity():
    # The superseded v1 key was run-scoped. A task in flight when v2 deploys must still be
    # findable, so the node hands the gateway the run-scoped key as a read-only fallback.
    identity = _ctx(node_run_id="nr_a", run_id="run_a").provider_call_idempotency(
        logical_call_slot="tts", provider_profile_id="p1"
    )
    other_run = _ctx(node_run_id="nr_a", run_id="run_b").provider_call_idempotency(
        logical_call_slot="tts", provider_profile_id="p1"
    )
    assert identity.fallback_keys and identity.fallback_keys != other_run.fallback_keys
    assert identity.key == other_run.key


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
