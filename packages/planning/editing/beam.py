"""Timeline-order fixed-width beam search for boundary window assignment.

Ported from editing_agent/boundary_beam.py. Replaces the old O(18^N) DFS with an
O(N*K*B) beam (N=chunks, K=beam_width, B=branch_factor). Constraints + scoring are
injected by the caller via ``score_fn`` (returns the candidate's base option score,
or None for a hard-constraint violation: capacity / max-reuse / adjacency ban /
original-source gate). The helper maintains per-state template / diversity / window
counts, applies an adjacency penalty for repeating the same template back-to-back,
and keeps the top beam_width states by cumulative score (deterministic tie-break on
the assignment's (window_id, template_id) sequence).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

ScoreFn = Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], float | None]


def _assignment_tiebreak_key(state: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Deterministic tie-break: (window_id, template_id) per position, both stripped."""
    return tuple(
        (
            str(state["assignment"][idx].get("window_id") or "").strip(),
            str(state["assignment"][idx].get("template_id") or "").strip(),
        )
        for idx in sorted(state["assignment"].keys())
    )


def assign_boundary_windows_beam(
    *,
    chunks: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    beam_width: int,
    branch_factor: int,
    score_fn: ScoreFn,
    round_time: Callable[[float], float],
    adjacency_penalty: float = 6.0,
) -> tuple[dict[int, dict[str, Any]] | None, float]:
    if not chunks or not candidates or beam_width < 1:
        # beam_width < 1 (mis-config) -> treat as no solution; caller relax/hard-fails.
        return None, float("-inf")

    beam: list[dict[str, Any]] = [
        {
            "score": 0.0,
            "last_template": None,
            "template_counts": {},
            "diversity_counts": {},
            "window_used": {},
            "assignment": {},
        }
    ]

    for position in range(len(chunks)):
        chunk = chunks[position]
        chunk_dur = round_time(chunk.get("duration", 0.0))
        successors: list[dict[str, Any]] = []
        for state in beam:
            scored: list[tuple[float, dict[str, Any]]] = []
            for candidate in candidates:
                base = score_fn(chunk, candidate, state)
                if base is None:
                    continue
                scored.append((base, candidate))
            # Stable descending by score (ties keep candidate input order).
            scored.sort(key=lambda item: -item[0])
            for base_score, candidate in scored[:branch_factor]:
                template_id = str(candidate.get("template_id") or "").strip()
                diversity_key = str(candidate.get("diversity_key") or "").strip()
                window_id = str(candidate.get("window_id") or "").strip()
                increment = base_score
                if state["last_template"] is not None and template_id == state["last_template"]:
                    increment -= adjacency_penalty
                child = {
                    "score": state["score"] + increment,
                    "last_template": template_id,
                    "template_counts": dict(state["template_counts"]),
                    "diversity_counts": dict(state["diversity_counts"]),
                    "window_used": dict(state["window_used"]),
                    "assignment": dict(state["assignment"]),
                }
                child["template_counts"][template_id] = child["template_counts"].get(template_id, 0) + 1
                if diversity_key:
                    child["diversity_counts"][diversity_key] = (
                        child["diversity_counts"].get(diversity_key, 0) + 1
                    )
                child["window_used"][window_id] = round_time(
                    child["window_used"].get(window_id, 0.0) + chunk_dur
                )
                child["assignment"][position] = candidate
                successors.append(child)
        if not successors:
            return None, float("-inf")
        successors.sort(key=lambda s: (-s["score"], _assignment_tiebreak_key(s)))
        beam = successors[:beam_width]

    best = beam[0]
    return best["assignment"], best["score"]
