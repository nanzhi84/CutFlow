"""Stable idempotency keys for Workflow-Job provider calls.

A single helper derives the key every paid provider call inside a Workflow Run
carries, so a re-run of the same logical call reuses one durable provider-call
identity instead of re-submitting to the vendor. The key deliberately excludes
``node_run.id``, Temporal attempt numbers, timestamps and randomness — those
change across infrastructure retries — and instead composes the Job-stable
coordinates of the call.

The Run coordinate is ``job_id``, not ``run.id``: an operator resume creates a
brand-new run, so a run-scoped key would mint a fresh identity for a call the
vendor was already paid for and re-submit it. ``job_id`` is constant across a
job's whole resume/retry lineage, so the resumed run finds the durable row and
polls (or replays) it instead. A RETRY, which must genuinely re-buy the work, is
separated not by this coordinate but by ``input_manifest_hash``: it re-runs every
node from the top, so its prefix artifacts carry fresh ids and every downstream
manifest — and key — differs (see tests/production/test_manifest_identity.py).

The returned key is scheme-prefixed. The ProviderGateway routes only
scheme-prefixed keys through durable persistence + the unique index; ad-hoc keys
that pre-date this helper (and non-Run keys such as asset annotation, BGM audio
understanding, clip embedding) never match the scheme and keep their "request id
passed through to the vendor" semantics without touching the ``idempotency_key``
column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

from packages.core.contracts import ErrorCode

# Version marker used both as the hash domain separator and as the routable output
# prefix. Bump the version when the key composition changes so old and new keys can
# never collide.
PROVIDER_CALL_KEY_SCHEME = "provider-call:v2"

# The run-scoped scheme v2 replaced. A durable row created under it is still READ
# during the transition — see ProviderCallIdempotency.fallback_keys — so a long task
# already in flight when v2 deploys keeps its durable identity instead of being
# re-submitted under the new key. Nothing writes it any more; delete both this
# constant and the fallback plumbing one release after v2 ships.
_SUPERSEDED_RUN_SCOPED_KEY_SCHEME = "provider-call:v1"

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


@dataclass(frozen=True)
class ProviderCallIdempotency:
    """The idempotency identity of one logical provider call.

    ``key`` is the identity the call is opened under. ``fallback_keys`` are the
    identities the SAME call answered to under superseded schemes: the gateway may
    RECOVER a durable row found under one of them, but never opens a new row under
    one.
    """

    key: str
    fallback_keys: tuple[str, ...] = field(default=())


def build_provider_call_idempotency_key(
    *,
    job_id: str,
    canonical_node_id: str,
    logical_call_slot: str,
    provider_profile_id: str,
    input_manifest_hash: str,
) -> str:
    """Compose the Job-stable idempotency key for one logical provider call.

    ``logical_call_slot`` distinguishes multiple logical calls inside one node
    (``tts``, ``lipsync:<profile_id>``, ``agent:attempt-<n>`` for a repair loop's
    n-th call, ...). Two calls that must be de-duplicated share a slot; two calls
    that are genuinely distinct (successive repair-loop attempts) must not.
    """
    return _scheme_key(
        PROVIDER_CALL_KEY_SCHEME,
        job_id,
        canonical_node_id,
        logical_call_slot,
        provider_profile_id,
        input_manifest_hash,
    )


def build_provider_call_idempotency(
    *,
    job_id: str,
    run_id: str,
    canonical_node_id: str,
    logical_call_slot: str,
    provider_profile_id: str,
    input_manifest_hash: str,
) -> ProviderCallIdempotency:
    """The current key for a call, plus the superseded-scheme key it may recover from."""
    coordinates = (canonical_node_id, logical_call_slot, provider_profile_id, input_manifest_hash)
    return ProviderCallIdempotency(
        key=_scheme_key(PROVIDER_CALL_KEY_SCHEME, job_id, *coordinates),
        fallback_keys=(_scheme_key(_SUPERSEDED_RUN_SCOPED_KEY_SCHEME, run_id, *coordinates),),
    )


def _scheme_key(scheme: str, *parts: str) -> str:
    digest = hashlib.sha256()
    for part in (scheme, *parts):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return f"{scheme}:{digest.hexdigest()}"


def is_provider_call_idempotency_key(key: str | None) -> bool:
    """True when ``key`` was minted by :func:`build_provider_call_idempotency_key`."""
    return key is not None and key.startswith(f"{PROVIDER_CALL_KEY_SCHEME}:")
