from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from fastapi.testclient import TestClient

from apps.api.app import create_app


WRITE_METHODS = {"post", "patch", "put", "delete"}
PUBLIC_SUCCESS_EXEMPTIONS = {
    ("POST", "/api/auth/register"): "public account bootstrap endpoint",
    ("POST", "/api/auth/login"): "public session bootstrap endpoint",
}
VIEWER_FORBIDDEN_EXEMPTIONS = {
    ("POST", "/api/auth/register"): "public endpoint",
    ("POST", "/api/auth/login"): "public endpoint",
    ("POST", "/api/auth/logout"): "self-service endpoint",
    ("PATCH", "/api/auth/me"): "self-service endpoint",
    ("POST", "/api/auth/me/change-password"): "self-service endpoint",
    ("PUT", "/api/auth/me/generation-defaults"): "self-service endpoint",
}
INVALID_BODY_EXEMPTIONS = {
    ("PUT", "/api/uploads/{upload_session_id}/file"): "optional multipart upload body reaches domain state",
}
BODY_OVERRIDES = {
    ("POST", "/api/jobs/digital-human-video"): {
        "case_id": "case_demo",
        "script": "contract matrix body",
        "voice": {"voice_id": "voice_sandbox"},
    },
}
PATH_VALUES = {
    "user_id": "usr_admin",
    "code_id": "reg_seed_local_admin",
    "upload_session_id": "upl_missing",
    "secret_id": "sec_missing",
    "case_id": "case_demo",
    "client_id": "client_missing",
    "account_id": "pubacct_missing",
    "login_id": "login_missing",
    "job_id": "job_missing",
    "run_id": "run_missing",
    "asset_id": "asset_broll_demo",
    "voice_id": "voice_sandbox",
    "template_id": "prompt_creative_intent",
    "version_id": "prompt_creative_intent_v1",
    "profile_id": "runninghub.heygem.default",
    "catalog_id": "price_sandbox",
    "binding_id": "prompt_binding_global_intent",
    "experiment_id": "exp_missing",
    "draft_id": "draft_missing",
    "memory_id": "mem_missing",
    "proposal_id": "bump_missing",
    "finished_video_id": "fv_missing",
    "video_version_id": "vv_missing",
    "id": "missing",
    "package_id": "pkg_missing",
    "batch_id": "pub_batch_missing",
    "item_id": "pub_item_missing",
    "attempt_id": "pub_attempt_missing",
    "budget_id": "budget_missing",
    "event_id": "alert_unpriced",
    "rule_id": "rule_missing",
}


def write_operations(app):
    spec = app.openapi()
    operations = []
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            if method.lower() in WRITE_METHODS:
                operations.append((method.upper(), path, operation))
    return operations


def assert_error_envelope(response, expected_status: int, expected_code: str | None = None) -> None:
    assert response.status_code == expected_status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["request_id"]
    assert response.headers["X-Request-Id"] == body["error"]["request_id"]
    if expected_code is not None:
        assert body["error"]["code"] == expected_code


def login(client: TestClient, email: str, password: str) -> None:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text


def path_for(path: str) -> str:
    resolved = path
    for key, value in PATH_VALUES.items():
        resolved = resolved.replace("{" + key + "}", value)
    assert "{" not in resolved, resolved
    return resolved


def request_body_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    request_body = operation.get("requestBody")
    if not request_body:
        return None
    content = request_body.get("content", {})
    media = content.get("application/json") or content.get("multipart/form-data")
    if not media:
        return None
    return media.get("schema")


def valid_body_for(operation: dict[str, Any], spec: dict[str, Any]) -> Any:
    schema = request_body_schema(operation)
    if schema is None:
        return None
    return value_for_schema(schema, spec)


def matrix_body(method: str, path: str, operation: dict[str, Any], spec: dict[str, Any]) -> Any:
    if (method, path) in BODY_OVERRIDES:
        return BODY_OVERRIDES[(method, path)]
    return valid_body_for(operation, spec)


def value_for_schema(schema: dict[str, Any], spec: dict[str, Any]) -> Any:
    schema = deepcopy(schema)
    if "$ref" in schema:
        schema = resolve_ref(schema["$ref"], spec)
    if "allOf" in schema:
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for item in schema["allOf"]:
            resolved = resolve_ref(item["$ref"], spec) if "$ref" in item else item
            merged["properties"].update(resolved.get("properties", {}))
            merged["required"].extend(resolved.get("required", []))
        schema = merged
    if "anyOf" in schema:
        non_null = [item for item in schema["anyOf"] if item.get("type") != "null"]
        return value_for_schema(non_null[0], spec) if non_null else None
    if "oneOf" in schema:
        return value_for_schema(schema["oneOf"][0], spec)
    if "enum" in schema:
        return schema["enum"][0]
    # Pydantic v2 emits single-value ``Literal[...]`` fields as ``const`` (not
    # ``enum``); honor it so generated bodies for option blocks with fixed-literal
    # fields (e.g. OutputOptions.format, StrictnessOptions.*) stay valid.
    if "const" in schema:
        return schema["const"]

    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        properties = schema.get("properties", {})
        required = schema.get("required") or list(properties)
        return {key: value_for_schema(properties[key], spec) for key in required if key in properties}
    if schema_type == "array":
        return [value_for_schema(schema.get("items", {}), spec)]
    if schema_type == "integer":
        return max(int(schema.get("minimum", 1) or 1), 1)
    if schema_type == "number":
        return max(float(schema.get("minimum", 1) or 1), 1.0)
    if schema_type == "boolean":
        return True
    if schema_type == "string":
        if schema.get("format") == "date-time":
            return datetime.now(timezone.utc).isoformat()
        if schema.get("format") == "email":
            return "matrix@example.com"
        if schema.get("pattern") == "^[A-Z]{3}$":
            return "CNY"
        min_length = int(schema.get("minLength", 1) or 1)
        return "x" * max(min_length, 1)
    return {}


def resolve_ref(ref: str, spec: dict[str, Any]) -> dict[str, Any]:
    current: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        current = current[part]
    return current


def send_matrix_request(client: TestClient, method: str, path: str, body: Any = None):
    url = path_for(path)
    if body is None:
        return client.request(method, url)
    return client.request(method, url, json=body)


def test_write_endpoint_matrix_requires_auth_or_declares_public_exemption():
    app = create_app()
    operations = write_operations(app)
    assert len(PUBLIC_SUCCESS_EXEMPTIONS) / len(operations) <= 0.10
    for method, path, operation in operations:
        if (method, path) in PUBLIC_SUCCESS_EXEMPTIONS:
            continue
        with TestClient(create_app()) as client:
            body = valid_body_for(operation, app.openapi()) if path.startswith("/api/auth/") else {}
            response = send_matrix_request(client, method, path, body)
            assert_error_envelope(response, 401, "auth.unauthorized")


def test_write_endpoint_matrix_rejects_viewer_for_operator_and_admin_routes():
    app = create_app()
    operations = write_operations(app)
    assert len(VIEWER_FORBIDDEN_EXEMPTIONS) / len(operations) <= 0.10
    for method, path, operation in operations:
        if (method, path) in VIEWER_FORBIDDEN_EXEMPTIONS:
            continue
        with TestClient(create_app()) as client:
            login(client, "viewer@local.cutagent", "local-viewer")
            response = send_matrix_request(client, method, path, matrix_body(method, path, operation, app.openapi()))
            assert_error_envelope(response, 403, "auth.forbidden")


def test_write_endpoint_matrix_uses_422_error_envelope_for_invalid_bodies():
    app = create_app()
    # Denominator = write endpoints that ACCEPT a request body (the ones eligible
    # for body validation). Body-less writes (DELETEs, no-body POSTs) are not
    # 422-body-testable, so excluding them keeps the coverage ratio meaningful and
    # robust to legitimately body-less endpoints rather than penalising them.
    body_bearing = [
        (method, path, operation)
        for method, path, operation in write_operations(app)
        if request_body_schema(operation) is not None
    ]
    covered = 0
    for method, path, _operation in body_bearing:
        if (method, path) in INVALID_BODY_EXEMPTIONS:
            continue
        covered += 1
        with TestClient(create_app()) as client:
            login(client, "admin@local.cutagent", "local-admin")
            response = send_matrix_request(client, method, path, {"__unexpected": True})
            assert_error_envelope(response, 422, "validation.invalid_options")
    assert covered / len(body_bearing) >= 0.90


def test_idempotency_replay_returns_200_per_spec_32_11():
    with TestClient(create_app()) as client:
        login(client, "admin@local.cutagent", "local-admin")
        case_payload = {"name": "Idempotency Case"}
        first = client.post("/api/cases", json=case_payload, headers={"Idempotency-Key": "matrix-case"})
        replay = client.post("/api/cases", json=case_payload, headers={"Idempotency-Key": "matrix-case"})
        assert first.status_code == 201, first.text
        assert replay.status_code == 200, replay.text
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json()["id"] == first.json()["id"]

        import_payload = {"import_type": "case", "rows": [{"external_id": "idem_import"}]}
        imported = client.post(
            "/api/import/batches",
            json=import_payload,
            headers={"Idempotency-Key": "matrix-import"},
        )
        replayed_import = client.post(
            "/api/import/batches",
            json=import_payload,
            headers={"Idempotency-Key": "matrix-import"},
        )
        assert imported.status_code == 202, imported.text
        assert replayed_import.status_code == 200, replayed_import.text
        assert replayed_import.headers["Idempotency-Replayed"] == "true"
        assert replayed_import.json()["batch_id"] == imported.json()["batch_id"]
