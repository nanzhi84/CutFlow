from __future__ import annotations

import asyncio
import logging

import pytest

from apps.worker import main as worker_main


class ReconcilerStub:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def reconcile_once(self) -> int:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return 1


def _cancel_on_sleep(monkeypatch) -> None:
    async def cancel(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker_main.asyncio, "sleep", cancel)


def test_upload_recovery_loop_runs_one_scan(monkeypatch) -> None:
    reconciler = ReconcilerStub()
    _cancel_on_sleep(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker_main._upload_recovery_loop(reconciler, 5))

    assert reconciler.calls == 1


def test_upload_recovery_loop_logs_scan_failure(monkeypatch, caplog) -> None:
    reconciler = ReconcilerStub(RuntimeError("database unavailable"))
    _cancel_on_sleep(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="cutagent.worker"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(worker_main._upload_recovery_loop(reconciler, 5))

    assert reconciler.calls == 1
    assert "Failed to reconcile interrupted uploads" in caplog.text
