"""Keep a Gateway recovery outcome from being answered with a quality fallback."""

from __future__ import annotations

from packages.core.contracts import ProviderInvocation
from packages.core.provider_idempotency import is_provider_recovery_error
from packages.core.workflow import NodeExecutionError


def reject_unrecoverable_provider_error(invocation: ProviderInvocation) -> None:
    """Fail the node when the Gateway declined to re-run a paid call.

    See ``PROVIDER_RECOVERY_ERROR_CODES``: these codes report that the previous
    attempt's result is unrecoverable, not that the request was bad. Degrading on them
    ships a worse deliverable (estimated timestamps, template queries, an emptied
    candidate set) and buries the cause under a warning nobody reads.
    """
    error = invocation.error
    if error is not None and is_provider_recovery_error(error.code):
        raise NodeExecutionError(error.code, error.message, retryable=False)
