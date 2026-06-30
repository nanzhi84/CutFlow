"""Idempotency middleware rejects large / streamed bodies (issue #65)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from apps.api.app import create_app


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def test_idempotency_key_rejects_octet_stream_write():
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        # A binary/streamed write carrying an Idempotency-Key must be refused
        # (413) before the middleware buffers the whole body.
        response = client.post(
            "/api/jobs/digital-human-video",
            content=b"x" * 64,
            headers={
                "Idempotency-Key": "binary-1",
                "Content-Type": "application/octet-stream",
            },
        )
        assert response.status_code == 413, response.text
        assert response.json()["error"]["code"] == "upload.too_large"


def test_idempotency_key_rejects_oversized_declared_body(monkeypatch):
    monkeypatch.setenv("CUTAGENT_IDEMPOTENCY_MAX_BODY_BYTES", "128")
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        # Content-Length over the cap is rejected up front (the body itself is
        # never buffered/hashed).
        response = client.post(
            "/api/jobs/digital-human-video",
            content=b"y" * 512,
            headers={
                "Idempotency-Key": "big-1",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 413, response.text


def test_idempotency_key_rejects_chunked_oversized_body_without_content_length(monkeypatch):
    monkeypatch.setenv("CUTAGENT_IDEMPOTENCY_MAX_BODY_BYTES", "128")
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        payload = {
            "case_id": "case_demo",
            "title": "Chunked over cap",
            "script": "x" * 512,
            "voice": {"voice_id": "voice_sandbox"},
            "strictness": {"strict_timestamps": False},
        }
        body = json.dumps(payload).encode()
        response = client.post(
            "/api/jobs/digital-human-video",
            content=iter((body[:64], body[64:192], body[192:])),
            headers={
                "Idempotency-Key": "chunked-big-1",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 413, response.text
        assert response.json()["error"]["code"] == "upload.too_large"


def test_small_json_write_with_idempotency_key_still_works():
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        payload = {
            "case_id": "case_demo",
            "title": "Idempotent small write",
            "script": "小 JSON 控制面请求仍然支持幂等。",
            "voice": {"voice_id": "voice_sandbox"},
            "strictness": {"strict_timestamps": False},
        }
        first = client.post(
            "/api/jobs/digital-human-video", json=payload, headers={"Idempotency-Key": "ok-1"}
        )
        assert first.status_code == 201, first.text
        # Replaying the same key + body returns the cached response (200 replay).
        second = client.post(
            "/api/jobs/digital-human-video", json=payload, headers={"Idempotency-Key": "ok-1"}
        )
        assert second.status_code == 200, second.text
        assert second.headers.get("Idempotency-Replayed") == "true"


def test_idempotency_oversized_response_streams_intact_and_is_not_cached(monkeypatch):
    """End-to-end through the real middleware: an over-cap 2xx response must be
    streamed back INTACT (never fully buffered) and must NOT be cached for replay
    (a retry re-executes). Drives the overflow branch by shrinking the response cap
    so a normal 201 job-creation body exceeds it."""
    monkeypatch.setenv("CUTAGENT_IDEMPOTENCY_MAX_RESPONSE_BYTES", "8")  # tiny: any 201 body overflows
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        payload = {
            "case_id": "case_demo",
            "title": "Oversized response, streamed",
            "script": "响应体超过缓存上限时必须流式回传且不入幂等记录。",
            "voice": {"voice_id": "voice_sandbox"},
            "strictness": {"strict_timestamps": False},
        }
        first = client.post(
            "/api/jobs/digital-human-video", json=payload, headers={"Idempotency-Key": "over-1"}
        )
        assert first.status_code == 201, first.text
        # Body streamed back intact (parses as JSON despite the passthrough).
        assert first.json()["job"]["id"], first.text
        # Over-cap response was NOT cached → replay re-executes (201), not a 200 replay.
        second = client.post(
            "/api/jobs/digital-human-video", json=payload, headers={"Idempotency-Key": "over-1"}
        )
        assert second.status_code == 201, second.text
        assert second.headers.get("Idempotency-Replayed") != "true"


def test_idempotency_response_capture_stops_at_cap():
    """#65 regression: an over-cap response must NOT be fully buffered.

    The middleware captured the WHOLE response body into memory before checking the
    cap (the loop never broke), so a 100MB response still blew the streaming memory
    boundary. ``_read_response_body_capped`` must stop reading the moment the cap is
    exceeded — proven here by a body_iterator that records how many chunks it was
    asked for: a bounded read pulls only a few, an unbounded one drains them all.
    """
    import asyncio

    from apps.api.dependencies import _read_response_body_capped

    pulled: list[int] = []

    async def _run():
        async def _gen():
            for i in range(10):
                pulled.append(i)
                yield b"x" * 100  # 100 bytes/chunk, 1000 bytes total

        class _FakeResp:
            body_iterator = _gen()

        return await _read_response_body_capped(_FakeResp(), cap=250)

    buffered, overflowed = asyncio.run(_run())
    assert overflowed is True
    # Bounded: stops within one chunk of the cap, never accumulates the full body.
    assert len(buffered) <= 250 + 100
    assert len(pulled) < 10, f"buffered the whole body ({len(pulled)} chunks); cap not enforced"


def test_idempotency_request_capture_stops_at_cap():
    """Chunked/no-length request bodies must stop reading when they exceed cap."""
    import asyncio

    from apps.api.dependencies import _read_request_body_capped

    pulled: list[int] = []

    async def _run():
        class _FakeReq:
            async def stream(self):
                for i in range(10):
                    pulled.append(i)
                    yield b"x" * 100

        return await _read_request_body_capped(_FakeReq(), cap=250)

    buffered, overflowed = asyncio.run(_run())
    assert overflowed is True
    assert len(buffered) <= 250
    assert len(pulled) < 10, f"buffered the whole body ({len(pulled)} chunks); cap not enforced"


def test_idempotency_request_capture_boundary_at_cap():
    """A body exactly == cap passes through (no overflow); cap+1 overflows."""
    import asyncio

    from apps.api.dependencies import _read_request_body_capped

    async def _read(total: int, cap: int) -> tuple[bytes, bool]:
        class _FakeReq:
            async def stream(self):
                yield b"x" * total

        return await _read_request_body_capped(_FakeReq(), cap=cap)

    body, overflowed = asyncio.run(_read(128, 128))
    assert overflowed is False
    assert len(body) == 128
    body, overflowed = asyncio.run(_read(129, 128))
    assert overflowed is True


def test_idempotency_oversized_passthrough_preserves_full_body():
    """The over-cap passthrough must stream back the COMPLETE body (already-read
    prefix + the remaining chunks), never dropping or corrupting bytes."""
    import asyncio

    from apps.api.dependencies import _read_response_body_capped

    original = b"".join(bytes([i]) * 100 for i in range(10))  # 1000 distinct bytes

    async def _run():
        async def _gen():
            for i in range(10):
                yield bytes([i]) * 100

        class _FakeResp:
            body_iterator = _gen()

        resp = _FakeResp()
        buffered, overflowed = await _read_response_body_capped(resp, cap=250)
        # Reconstruct exactly as the middleware passthrough does: prefix + remainder.
        out = bytearray(buffered)
        async for chunk in resp.body_iterator:
            out += chunk
        return bytes(out), overflowed

    out, overflowed = asyncio.run(_run())
    assert overflowed is True
    assert out == original, "streamed passthrough dropped/corrupted body bytes"
