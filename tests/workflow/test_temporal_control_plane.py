"""Unit tests for the Temporal control-plane hardening (issue #69).

These exercise ``TemporalRuntimeAdapter`` without a real Temporal server by
monkeypatching ``Client.connect``: connect timeout, client reuse across calls,
``WorkflowAlreadyStartedError`` idempotency, and ``close()`` teardown. The real
end-to-end behaviour is covered by the gated ``tests/temporal`` suite.
"""

from __future__ import annotations

import asyncio

import pytest
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from packages.core import contracts as c
from packages.core.workflow import temporal_adapter as ta
from packages.core.workflow.runtime import NodeExecutionError, WorkflowRuntimeSettings


def _adapter() -> ta.TemporalRuntimeAdapter:
    return ta.TemporalRuntimeAdapter(WorkflowRuntimeSettings(runtime="temporal"))


def test_adapter_reuses_a_single_client_across_calls(monkeypatch):
    connects = {"count": 0}

    class _FakeClient:
        pass

    async def _fake_connect(*_args, **_kwargs):
        connects["count"] += 1
        return _FakeClient()

    monkeypatch.setattr(Client, "connect", _fake_connect)
    adapter = _adapter()
    try:
        first = adapter._run(adapter._client())
        second = adapter._run(adapter._client())
        assert first is second
        assert connects["count"] == 1
    finally:
        adapter.close()


def test_adapter_connect_timeout_surfaces_worker_lost(monkeypatch):
    monkeypatch.setattr(ta, "TEMPORAL_CONNECT_TIMEOUT_SECONDS", 0.05)

    async def _slow_connect(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(Client, "connect", _slow_connect)
    adapter = _adapter()
    try:
        with pytest.raises(NodeExecutionError) as excinfo:
            adapter._run(adapter._client())
        assert excinfo.value.error.code == c.ErrorCode.workflow_worker_lost
    finally:
        adapter.close()


def test_start_workflow_is_idempotent_on_already_started(monkeypatch):
    class _FakeClient:
        async def start_workflow(self, *_args, **_kwargs):
            raise WorkflowAlreadyStartedError(
                workflow_id="run_x", workflow_type=ta.WORKFLOW_TYPE, run_id="run_x"
            )

    async def _fake_connect(*_args, **_kwargs):
        return _FakeClient()

    monkeypatch.setattr(Client, "connect", _fake_connect)
    adapter = _adapter()
    try:
        # A workflow already existing for this run_id is treated as success
        # (idempotent create retry), so no exception escapes.
        assert adapter._run(adapter._start_workflow({"run_id": "run_x"})) is None
    finally:
        adapter.close()


def test_start_workflow_passes_rpc_timeout(monkeypatch):
    seen = {}

    class _FakeClient:
        async def start_workflow(self, *_args, **kwargs):
            seen.update(kwargs)

    async def _fake_connect(*_args, **_kwargs):
        return _FakeClient()

    monkeypatch.setattr(Client, "connect", _fake_connect)
    adapter = _adapter()
    try:
        adapter._run(adapter._start_workflow({"run_id": "run_y"}))
        assert seen.get("rpc_timeout") == ta.TEMPORAL_RPC_TIMEOUT
        assert seen.get("task_queue") == adapter.settings.temporal_task_queue
    finally:
        adapter.close()


def test_close_is_idempotent_and_safe_without_a_loop():
    adapter = _adapter()
    # Never started a loop -> close must be a no-op, and calling twice is safe.
    adapter.close()
    adapter.close()
