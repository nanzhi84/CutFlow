from __future__ import annotations

import asyncio

from apps.worker.main import _reconcile_cancelling_runs
from packages.core.workflow.temporal_adapter import TEMPORAL_RPC_TIMEOUT


def test_worker_replays_durable_cancellation_modes_after_restart() -> None:
    signals: list[tuple[str, str, dict, object]] = []

    class Repository:
        def run_ids_with_cancelling(self, *, limit: int) -> list[str]:
            assert limit == 100
            return ["run_graceful", "run_force"]

        def run_cancel_mode(self, run_id: str) -> str:
            return "force" if run_id == "run_force" else "graceful"

    class Handle:
        def __init__(self, run_id: str) -> None:
            self.run_id = run_id

        async def signal(self, name: str, payload: dict, *, rpc_timeout) -> None:
            signals.append((self.run_id, name, payload, rpc_timeout))

    class Client:
        def get_workflow_handle(self, run_id: str) -> Handle:
            return Handle(run_id)

    asyncio.run(_reconcile_cancelling_runs(Client(), Repository()))

    assert signals == [
        (
            "run_graceful",
            "cancel",
            {"mode": "graceful", "reason": "worker cancellation reconciliation"},
            TEMPORAL_RPC_TIMEOUT,
        ),
        (
            "run_force",
            "cancel",
            {"mode": "force", "reason": "worker cancellation reconciliation"},
            TEMPORAL_RPC_TIMEOUT,
        ),
    ]
