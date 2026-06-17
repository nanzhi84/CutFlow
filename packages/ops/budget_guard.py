from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from packages.core.contracts import (
    Budget,
    BudgetEvaluation,
    DegradationNotice,
    ErrorCode,
    ProviderError,
    WarningCode,
)


logger = logging.getLogger(__name__)


class BudgetEvaluationRepository(Protocol):
    def list_budgets(self, *, limit: int = 50) -> list[Budget]:
        ...

    def evaluate_budgets(self, *, now: datetime | None = None) -> list[BudgetEvaluation]:
        ...


class BudgetEnforcementGuard:
    def __init__(self, repository: BudgetEvaluationRepository) -> None:
        self.repository = repository

    def evaluate(self, *, call: object, invocation: object) -> ProviderError | None:
        budgets = {budget.id: budget for budget in self.repository.list_budgets(limit=200)}
        # The guard runs on EVERY provider call. It can only ever block on an
        # enabled+enforce budget, so when none exists skip evaluate_budgets() —
        # which does a full spend scan + alert-sync write — rather than paying that
        # cost (and write amplification) on every call. Alert syncing for
        # warning-only budgets still happens via the ops snapshot path, not here.
        if not any(budget.enabled and budget.enforce for budget in budgets.values()):
            return None
        for evaluation in self.repository.evaluate_budgets():
            budget = budgets.get(evaluation.budget_id)
            if budget is None or not budget.enabled or not budget.enforce:
                continue
            if not evaluation.exceeded:
                continue
            if not self._evaluation_applies(evaluation, call=call, invocation=invocation):
                continue
            notice = self._degradation_notice(evaluation)
            logger.warning(
                "provider call blocked by budget guard",
                extra={
                    "event": "provider.budget_exceeded",
                    "degradation_level": "hard_block",
                    "degradation": notice.model_dump(mode="json"),
                },
            )
            return ProviderError(
                code=ErrorCode.provider_quota_exceeded,
                message=(
                    f"Provider call blocked: {evaluation.scope_type} budget "
                    f"{evaluation.budget_id} is over budget "
                    f"({evaluation.spend.amount} {evaluation.spend.currency} / "
                    f"{evaluation.limit.amount} {evaluation.limit.currency})."
                ),
                retryable=False,
            )
        return None

    def _evaluation_applies(
        self,
        evaluation: BudgetEvaluation,
        *,
        call: object,
        invocation: object,
    ) -> bool:
        scope_type = evaluation.scope_type
        scope_id = evaluation.scope_id
        if scope_type == "global":
            return True
        if scope_type == "workspace":
            workspace_id = self._workspace_id(call, invocation)
            return scope_id is None or workspace_id == scope_id
        if scope_id is None:
            return True
        if scope_type == "provider":
            return getattr(invocation, "provider_id", None) == scope_id
        if scope_type == "capability":
            return (
                getattr(call, "capability_id", None) == scope_id
                or getattr(invocation, "capability_id", None) == scope_id
            )
        if scope_type == "case":
            return (
                getattr(call, "case_id", None) == scope_id
                or getattr(invocation, "case_id", None) == scope_id
            )
        return False

    def _workspace_id(self, *objects: object) -> str | None:
        for item in objects:
            direct = getattr(item, "workspace_id", None)
            if direct:
                return str(direct)
            payload = getattr(item, "input", None)
            if isinstance(payload, dict):
                value = payload.get("workspace_id")
                if value:
                    return str(value)
        return None

    def _degradation_notice(self, evaluation: BudgetEvaluation) -> DegradationNotice:
        return DegradationNotice(
            code=WarningCode.budget_exceeded,
            message=(
                f"Budget {evaluation.budget_id} exceeded for "
                f"{evaluation.scope_type}:{evaluation.scope_id or '*'}."
            ),
            affects_true_yield=True,
            details={
                "budget_id": evaluation.budget_id,
                "scope_type": evaluation.scope_type,
                "scope_id": evaluation.scope_id,
                "spend": str(evaluation.spend.amount),
                "limit": str(evaluation.limit.amount),
                "currency": evaluation.limit.currency,
            },
        )
