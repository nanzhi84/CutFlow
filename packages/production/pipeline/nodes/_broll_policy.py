"""Shared b-roll selection policy helpers.

Single source of truth for the ``include_generic_coverage`` decision so the
three call sites (MaterialPackPlanning, BrollPlanning, BrollCoveragePlanning)
can never drift apart — a past drift between two of these gates is exactly what
silently emptied the b-roll pool.
"""

from __future__ import annotations

from packages.core.contracts import DigitalHumanVideoRequest


def broll_generic_coverage_enabled(request: DigitalHumanVideoRequest) -> bool:
    """Whether person-free clean clips with no keyword overlap may fill b-roll.

    ``broll_only_v1`` forces it on (its whole purpose is full b-roll coverage);
    every other template follows ``BrollOptions.allow_generic_coverage`` (default
    on). The person/lip-sync gates and the keyword floor for *matched* clips are
    unaffected — this only governs whether the no-overlap fallback is offered.
    """
    return (
        request.workflow_template_id == "broll_only_v1"
        or request.broll.allow_generic_coverage
    )
