"""Stable idempotency keys for Workflow-Run provider calls.

A single helper derives the key every paid provider call inside a Workflow Run
carries, so an infrastructure retry of the same logical call (a Temporal activity
re-run after a crash) reuses one durable provider-call identity instead of
re-submitting to the vendor. The key deliberately excludes ``node_run.id``,
Temporal attempt numbers, timestamps and randomness — those change across
infrastructure retries — and instead composes the Run-stable coordinates of the
call.

The returned key is scheme-prefixed. The ProviderGateway routes only
scheme-prefixed keys through durable persistence + the unique index; ad-hoc keys
that pre-date this helper (and non-Run keys such as asset annotation, BGM audio
understanding, clip embedding, publish copy) never match the scheme and keep
their "request id passed through to the vendor" semantics without touching the
``idempotency_key`` column.
"""

from __future__ import annotations

import hashlib

from packages.core.contracts import ErrorCode

# Version marker used both as the hash domain separator and as the routable output
# prefix. Bump the version when the key composition changes so old and new keys can
# never collide.
PROVIDER_CALL_KEY_SCHEME = "provider-call:v1"

# What the Gateway reports when it declines to re-run a paid call: the prior attempt's
# result cannot be recovered, or its submit may or may not have reached the vendor.
# Neither says anything about the QUALITY of the request, so a node must never answer
# one with its quality fallback — that turns a worker hiccup into a silently degraded
# deliverable and hides the root cause. Fail the node instead and let a retry (or an
# operator) re-drive it.
PROVIDER_RECOVERY_ERROR_CODES = frozenset(
    {
        ErrorCode.idempotency_conflict,
        ErrorCode.provider_submit_outcome_unknown,
    }
)


def is_provider_recovery_error(code: ErrorCode | str | None) -> bool:
    """True when ``code`` is a Gateway recovery outcome that must not be degraded away."""
    if code is None:
        return False
    return ErrorCode(code) in PROVIDER_RECOVERY_ERROR_CODES


def build_provider_call_idempotency_key(
    *,
    run_id: str,
    canonical_node_id: str,
    logical_call_slot: str,
    provider_profile_id: str,
    input_manifest_hash: str,
) -> str:
    """Compose the Run-stable idempotency key for one logical provider call.

    ``logical_call_slot`` distinguishes multiple logical calls inside one node
    (``tts``, ``lipsync:<profile_id>``, ``agent:attempt-<n>`` for a repair loop's
    n-th call, ...). Two calls that must be de-duplicated share a slot; two calls
    that are genuinely distinct (successive repair-loop attempts) must not.
    """
    digest = hashlib.sha256()
    for part in (
        PROVIDER_CALL_KEY_SCHEME,
        run_id,
        canonical_node_id,
        logical_call_slot,
        provider_profile_id,
        input_manifest_hash,
    ):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return f"{PROVIDER_CALL_KEY_SCHEME}:{digest.hexdigest()}"


def is_provider_call_idempotency_key(key: str | None) -> bool:
    """True when ``key`` was minted by :func:`build_provider_call_idempotency_key`."""
    return key is not None and key.startswith(f"{PROVIDER_CALL_KEY_SCHEME}:")
