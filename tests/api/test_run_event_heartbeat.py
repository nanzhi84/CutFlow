"""Run event-stream WebSocket sends idle heartbeats (issue #74)."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.services import jobs_runs


def test_run_websocket_sends_heartbeat_when_idle(monkeypatch):
    # Shrink the heartbeat cadence so the test does not wait the real 15s.
    monkeypatch.setattr(jobs_runs, "EVENT_STREAM_HEARTBEAT_INTERVAL_SECONDS", 0.02)
    app = create_app()
    with TestClient(app) as client:
        token = app.state.event_tokens.issue("run_hb", timedelta(minutes=5)).token
        with client.websocket_connect(f"/ws/runs/run_hb?token={token}") as ws:
            # No real run events flow, so the only frames are heartbeats.
            saw_heartbeat = False
            for _ in range(10):
                message = ws.receive_json()
                if message.get("event_type") == "heartbeat":
                    saw_heartbeat = True
                    assert message.get("server_time")
                    break
            assert saw_heartbeat, "expected an idle heartbeat frame"
