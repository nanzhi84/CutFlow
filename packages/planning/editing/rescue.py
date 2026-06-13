"""Feasibility backtracking rescue for boundary window assignment.

Ported from editing_agent/boundary_planning.rescue_boundary_assignment_with_backtracking.
Runs only after every beam relax-pass fails: the fixed-width beam can spend a scarce
large window's reuse budget on an early slot and miss an otherwise-feasible packing
(prod bc881391). This is the same constraint semantics + scoring as the beam (the
caller's injected ``score_fn``), but it can backtrack. Pruning:
  - visited = (position, last_template, template_counts, window_used) — window budget
    MUST participate so "small window first, save the big one for the long chunk"
    paths are not wrongly pruned;
  - a cheap look-ahead: the remaining hardest chunk must have at least one candidate
    that is still feasible ignoring adjacency (usage only grows, so this is a
    necessary condition — never prunes a feasible subtree).
Cut-offs: wall-clock deadline + node-count limit; on overshoot it fails open.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from packages.planning.editing.constants import RESCUE_DEADLINE_SECONDS, RESCUE_NODE_LIMIT


def rescue_boundary_assignment_with_backtracking(
    *,
    chunks: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    score_fn: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], float | None],
    round_time: Callable[[float], float],
    adjacency_penalty: float = 6.0,
    deadline_seconds: float = RESCUE_DEADLINE_SECONDS,
    node_limit: int = RESCUE_NODE_LIMIT,
) -> tuple[dict[int, dict[str, Any]] | None, float, dict[str, Any]]:
    meta: dict[str, Any] = {"nodes_explored": 0, "timed_out": False, "node_limit_hit": False}
    if not chunks or not candidates:
        return None, float("-inf"), meta
    deadline = time.monotonic() + max(0.0, float(deadline_seconds))
    total = len(chunks)
    state: dict[str, Any] = {
        "last_template": None,
        "template_counts": {},
        "diversity_counts": {},
        "window_used": {},
    }
    assignment: dict[int, dict[str, Any]] = {}
    visited: set = set()
    aborted = False

    def remaining_feasible(position: int) -> bool:
        hardest = max(chunks[position:], key=lambda chunk: round_time(chunk.get("duration", 0.0)))
        probe_state = {
            "last_template": None,
            "template_counts": state["template_counts"],
            "diversity_counts": state["diversity_counts"],
            "window_used": state["window_used"],
        }
        return any(score_fn(hardest, candidate, probe_state) is not None for candidate in candidates)

    def dfs(position: int, accumulated: float) -> float | None:
        nonlocal aborted
        if position >= total:
            return accumulated
        meta["nodes_explored"] += 1
        if meta["nodes_explored"] > node_limit:
            meta["node_limit_hit"] = True
            aborted = True
            return None
        if time.monotonic() > deadline:
            meta["timed_out"] = True
            aborted = True
            return None
        visited_key = (
            position,
            state["last_template"],
            frozenset(state["template_counts"].items()),
            frozenset(state["window_used"].items()),
        )
        if visited_key in visited:
            return None
        visited.add(visited_key)
        if not remaining_feasible(position):
            return None
        chunk = chunks[position]
        chunk_duration = round_time(chunk.get("duration", 0.0))
        scored: list[tuple[float, dict[str, Any]]] = []
        for candidate in candidates:
            base = score_fn(chunk, candidate, state)
            if base is None:
                continue
            scored.append((base, candidate))
        scored.sort(key=lambda item: -item[0])
        for base_score, candidate in scored:
            template_id = str(candidate.get("template_id") or "").strip()
            diversity_key = str(candidate.get("diversity_key") or "").strip()
            window_id = str(candidate.get("window_id") or "").strip()
            increment = base_score
            if state["last_template"] is not None and template_id == state["last_template"]:
                increment -= adjacency_penalty
            previous_last = state["last_template"]
            previous_window_used = state["window_used"].get(window_id)
            state["last_template"] = template_id
            state["template_counts"][template_id] = state["template_counts"].get(template_id, 0) + 1
            if diversity_key:
                state["diversity_counts"][diversity_key] = (
                    state["diversity_counts"].get(diversity_key, 0) + 1
                )
            state["window_used"][window_id] = round_time((previous_window_used or 0.0) + chunk_duration)
            assignment[position] = candidate
            result = dfs(position + 1, accumulated + increment)
            if result is not None:
                return result
            del assignment[position]
            state["last_template"] = previous_last
            if state["template_counts"][template_id] <= 1:
                del state["template_counts"][template_id]
            else:
                state["template_counts"][template_id] -= 1
            if diversity_key:
                if state["diversity_counts"][diversity_key] <= 1:
                    del state["diversity_counts"][diversity_key]
                else:
                    state["diversity_counts"][diversity_key] -= 1
            if previous_window_used is None:
                del state["window_used"][window_id]
            else:
                state["window_used"][window_id] = previous_window_used
            if aborted:
                return None
        return None

    final_score = dfs(0, 0.0)
    if final_score is None:
        return None, float("-inf"), meta
    return dict(assignment), final_score, meta
