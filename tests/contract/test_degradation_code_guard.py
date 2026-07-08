"""Guard: every ``DegradationNotice`` code must be a valid ``DegradationCode``.

``DegradationNotice.code`` is typed ``WarningCode`` (the superset), but its codes
flow into ``RunPublicReportArtifact.degradations: list[DegradationCode]`` (see
``packages/production/pipeline/digital_human.py`` ``_write_report``). If a notice
carries a ``WarningCode`` that has no ``DegradationCode`` counterpart, building the
public run report raises ``ValidationError`` at run-finalization time.

This test statically scans every ``degradation_notice(...)`` / ``DegradationNotice(...)``
construction under ``packages/`` and extracts literal ``code=WarningCode.<name>``
arguments. Any name that is not also a ``DegradationCode`` member is a latent crash
risk. New violating call sites make this test fail.

Two pre-existing orphan sites are pinned in ``KNOWN_UNMAPPED`` rather than silently
tolerated (see each entry). The set-equality assertion therefore also fails if a
known site is removed — forcing the allowlist to stay honest — while any *new*
orphan site (a different file, or a different orphan code) still fails the guard.
"""

from __future__ import annotations

import ast
import pathlib

from packages.core.contracts import DegradationCode

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PACKAGES_ROOT = _REPO_ROOT / "packages"

# Call targets that construct a graded degradation carrying a ``code`` field.
_CONSTRUCTOR_NAMES = {"degradation_notice", "DegradationNotice"}

# Pre-existing ``code=WarningCode.<orphan>`` construction sites, keyed by the
# repo-relative module path. These are documented, not fixed here:
#   - narration_alignment: the ASR-estimated fallback puts this notice into
#     RunState.degradations, so it CAN reach the public report and crash it. This
#     is a genuine latent bug (an unmapped DegradationCode.timestamp_estimated is
#     the proper fix, which is a contract change tracked separately).
#   - budget_guard: a provider-gateway budget-block notice that is only logged /
#     turned into a ProviderError and never enters RunState.degradations, so it
#     cannot reach the DegradationCode sink today.
KNOWN_UNMAPPED: set[tuple[str, str]] = {
    ("packages/production/pipeline/nodes/narration_alignment.py", "timestamp_estimated"),
    ("packages/ops/budget_guard.py", "budget_exceeded"),
}


def _call_target_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _warningcode_member(node: ast.expr) -> str | None:
    """Return ``<name>`` for a literal ``WarningCode.<name>`` expression, else None."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "WarningCode"
    ):
        return node.attr
    return None


def _code_member(call: ast.Call, target: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "code":
            return _warningcode_member(keyword.value)
    # ``degradation_notice(code, message, ...)`` also accepts ``code`` positionally.
    if target == "degradation_notice" and call.args:
        return _warningcode_member(call.args[0])
    return None


def _scan_degradation_code_sites() -> tuple[list[tuple[str, int, str]], set[tuple[str, str]]]:
    sites: list[tuple[str, int, str]] = []
    orphans: set[tuple[str, str]] = set()
    for path in sorted(_PACKAGES_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(_REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = _call_target_name(node.func)
            if target not in _CONSTRUCTOR_NAMES:
                continue
            member = _code_member(node, target)
            if member is None:
                continue
            sites.append((rel, node.lineno, member))
            if member not in DegradationCode.__members__:
                orphans.add((rel, member))
    return sites, orphans


def test_degradation_notice_codes_are_valid_degradation_codes():
    sites, orphans = _scan_degradation_code_sites()

    # Liveness: the scanner must actually find the pipeline's construction sites,
    # otherwise an empty scan would vacuously "pass".
    assert len(sites) >= 15, f"scanner found too few degradation sites: {len(sites)}"

    assert orphans == KNOWN_UNMAPPED, (
        "DegradationNotice.code must be a valid DegradationCode. Unexpected orphan "
        "WarningCode used at a construction site (it would crash "
        "RunPublicReportArtifact.degradations if it reached the report). "
        f"unexpected_new={sorted(orphans - KNOWN_UNMAPPED)} "
        f"unexpectedly_fixed={sorted(KNOWN_UNMAPPED - orphans)}"
    )
