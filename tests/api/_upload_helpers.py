"""Shared helper to drive the browser-direct upload flow in API tests.

The browser-direct flow is: prepare -> PUT bytes to the presigned URL -> complete.
On the local/memory test backend the presigned PUT URL is a ``local://`` URI, so
"the browser PUT" is simulated by writing the bytes straight through the object
store (the API never ingests bytes; there is no proxy endpoint anymore).
"""

from __future__ import annotations

import hashlib
import struct

from apps.api.main import app
from packages.core.storage.object_store import parse_local_uri


def minimal_ttf_bytes(*, family: str = "Cutagent Test") -> bytes:
    """Return a tiny sfnt that fontTools can parse without external fixtures."""

    family_bytes = family.encode("utf-16-be")
    name_table = (
        struct.pack(">HHH", 0, 1, 18)
        + struct.pack(">HHHHHH", 3, 1, 0x409, 1, len(family_bytes), 0)
        + family_bytes
    )
    return (
        struct.pack(">4sHHHH", b"\x00\x01\x00\x00", 1, 0, 0, 0)
        + struct.pack(">4sIII", b"name", 0, 28, len(name_table))
        + name_table
    )


def direct_upload(
    client,
    *,
    kind: str,
    filename: str,
    content_type: str,
    body: bytes,
    case_id: str | None = None,
    sha256: str | None = None,
    metadata: dict | None = None,
    stabilize: bool = False,
    client_upload_id: str | None = None,
):
    """Run prepare -> (write staging through the store) -> complete.

    Returns ``(prepared_response, completed_response)``. If prepare fails,
    ``completed_response`` is ``None`` so callers can assert on the prepare error.
    """
    digest = sha256 if sha256 is not None else hashlib.sha256(body).hexdigest()
    stable_client_id = client_upload_id or (
        "client_test_" + hashlib.sha256(f"{kind}:{filename}:{digest}".encode()).hexdigest()[:24]
    )
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "client_upload_id": stable_client_id,
            "kind": kind,
            "case_id": case_id,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(body),
            "sha256": digest,
            "stabilize": stabilize,
        },
    )
    if prepared.status_code != 201:
        return prepared, None
    ticket = prepared.json()
    # Simulate the browser's direct PUT to OSS by writing through the store.
    app.state.object_store.put_bytes(parse_local_uri(ticket["put_url"]), body)
    completed = client.post(
        "/api/uploads/complete",
        json={
            "upload_session_id": ticket["upload_session"]["id"],
            "size_bytes": len(body),
            "sha256": digest,
            "metadata": metadata or {},
        },
    )
    return prepared, completed
