from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse

from packages.ai.gateway import ProviderGateway, SqlAlchemyProviderRepository, SqlAlchemyProviderRuntimeRepository
from packages.ai.prompts import PromptRegistry, SqlAlchemyPromptRepository, SqlAlchemyPromptRuntimeRepository
from packages.core import contracts as c
from packages.core.auth import AuthService, SqlAlchemyAuthService, create_sqlalchemy_auth_service
from packages.core.auth.service import create_password_hasher
from packages.core.registration_codes import hash_registration_code
from packages.core.observability import metric_snapshot
from packages.core.storage import ObjectStore, Repository, get_object_store
from packages.core.storage.bootstrap import (
    bootstrap_sqlalchemy_storage_if_enabled,
    get_sqlalchemy_session_factory_if_enabled,
)
from packages.core.storage.object_store import parse_local_uri
from packages.core.storage.repository import new_id
from packages.core.contracts.state_machines import assert_transition
from packages.core.storage.sqlalchemy_idempotency import SqlAlchemyIdempotencyRepository
from packages.core.storage.sqlalchemy_secrets import SqlAlchemySecretRepository
from packages.core.storage.sqlalchemy_uploads import SqlAlchemyUploadRepository
from packages.core.storage.secret_store import LocalSecretStore, SecretStore
from packages.core.workflow import NodeExecutionError
from packages.creative.cases import SqlAlchemyCaseLearningRepository, SqlAlchemyCaseRepository
from packages.media import SqlAlchemyMediaRepository
from packages.ops import SqlAlchemyOpsRepository
from packages.production import SqlAlchemyProductionRepository
from packages.publishing import SqlAlchemyPublishingRepository
from packages.production.pipeline import DigitalHumanWorkflow, build_digital_human_workflow


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap_sqlalchemy_storage_if_enabled()
    session_factory = get_sqlalchemy_session_factory_if_enabled()
    configure_app_state(app, session_factory=session_factory)
    yield


app = FastAPI(
    title="Cutagent Clean-Slate API",
    version="0.1.0",
    description="Case-first digital human production API generated from the clean-slate spec.",
    lifespan=lifespan,
)


def configure_app_state(app: FastAPI, *, session_factory=None) -> None:
    runtime_repository = Repository()
    app.state.repository = runtime_repository
    app.state.object_store = get_object_store()
    app.state.secret_store = LocalSecretStore()
    if session_factory is None:
        app.state.sqlalchemy_case_repository = None
        app.state.sqlalchemy_case_learning_repository = None
        app.state.sqlalchemy_upload_repository = None
        app.state.sqlalchemy_media_repository = None
        app.state.sqlalchemy_prompt_repository = None
        app.state.sqlalchemy_provider_repository = None
        app.state.sqlalchemy_idempotency_repository = None
        app.state.sqlalchemy_secret_repository = None
        app.state.sqlalchemy_ops_repository = None
        app.state.sqlalchemy_publishing_repository = None
        app.state.sqlalchemy_production_repository = None
        app.state.auth_service = AuthService(runtime_repository, create_password_hasher())
        provider_reader = None
        prompt_reader = None
    else:
        app.state.sqlalchemy_case_repository = SqlAlchemyCaseRepository(session_factory)
        app.state.sqlalchemy_case_learning_repository = SqlAlchemyCaseLearningRepository(session_factory)
        app.state.sqlalchemy_upload_repository = SqlAlchemyUploadRepository(session_factory)
        app.state.sqlalchemy_media_repository = SqlAlchemyMediaRepository(session_factory)
        app.state.sqlalchemy_prompt_repository = SqlAlchemyPromptRepository(session_factory)
        app.state.sqlalchemy_provider_repository = SqlAlchemyProviderRepository(session_factory)
        app.state.sqlalchemy_idempotency_repository = SqlAlchemyIdempotencyRepository(session_factory)
        app.state.sqlalchemy_secret_repository = SqlAlchemySecretRepository(session_factory, app.state.secret_store)
        app.state.sqlalchemy_ops_repository = SqlAlchemyOpsRepository(session_factory)
        app.state.sqlalchemy_publishing_repository = SqlAlchemyPublishingRepository(session_factory)
        app.state.sqlalchemy_production_repository = SqlAlchemyProductionRepository(session_factory)
        app.state.auth_service = create_sqlalchemy_auth_service(session_factory)
        provider_reader = SqlAlchemyProviderRuntimeRepository(session_factory)
        prompt_reader = SqlAlchemyPromptRuntimeRepository(session_factory)
    app.state.provider_gateway = ProviderGateway(
        runtime_repository,
        provider_reader=provider_reader,
        secret_store=app.state.secret_store,
    )
    app.state.prompt_registry = PromptRegistry(runtime_repository, prompt_reader=prompt_reader)
    app.state.workflow = build_digital_human_workflow(
        runtime_repository,
        provider_gateway=app.state.provider_gateway,
        prompt_registry=app.state.prompt_registry,
    )


configure_app_state(app)


def repository() -> Repository:
    return app.state.repository


def object_store() -> ObjectStore:
    return app.state.object_store


def secret_store() -> SecretStore:
    return app.state.secret_store


def auth() -> AuthService | SqlAlchemyAuthService:
    return app.state.auth_service


def workflow_runtime() -> DigitalHumanWorkflow:
    return app.state.workflow


def case_repository() -> SqlAlchemyCaseRepository | None:
    return app.state.sqlalchemy_case_repository


def case_learning_repository() -> SqlAlchemyCaseLearningRepository | None:
    return app.state.sqlalchemy_case_learning_repository


def upload_repository() -> SqlAlchemyUploadRepository | None:
    return app.state.sqlalchemy_upload_repository


def media_repository() -> SqlAlchemyMediaRepository | None:
    return app.state.sqlalchemy_media_repository


def prompt_repository() -> SqlAlchemyPromptRepository | None:
    return app.state.sqlalchemy_prompt_repository


def provider_repository() -> SqlAlchemyProviderRepository | None:
    return app.state.sqlalchemy_provider_repository


def idempotency_repository() -> SqlAlchemyIdempotencyRepository | None:
    return app.state.sqlalchemy_idempotency_repository


def secret_repository() -> SqlAlchemySecretRepository | None:
    return app.state.sqlalchemy_secret_repository


def ops_repository() -> SqlAlchemyOpsRepository | None:
    return app.state.sqlalchemy_ops_repository


def publishing_repository() -> SqlAlchemyPublishingRepository | None:
    return app.state.sqlalchemy_publishing_repository


def production_repository() -> SqlAlchemyProductionRepository | None:
    return app.state.sqlalchemy_production_repository

SESSION_COOKIE = "cutagent_session"
PUBLIC_API_PATHS = {"/api/health"}
PUBLIC_PATHS = {"/metrics"}
PUBLIC_API_PREFIXES = ("/api/auth/",)
IDEMPOTENT_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
REQUEST_ID_CONTEXT: ContextVar[str | None] = ContextVar("request_id", default=None)


def request_id() -> str:
    current = REQUEST_ID_CONTEXT.get()
    if current is not None:
        return current
    return f"req_{uuid4().hex[:12]}"


def page(items, limit: int = 50):
    values = list(items)[:limit]
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def get_case(case_id: str) -> c.CaseDetail:
    if case_repository() is not None:
        case = case_repository().get_case(case_id)
        if case is not None:
            return case
    if case_id not in repository().cases:
        raise NodeExecutionError(c.ErrorCode.validation_missing_case, f"Case {case_id} does not exist.")
    return repository().cases[case_id]


def signed(path: str) -> c.SignedUrlResponse:
    return object_store().signed_url(f"local://cutagent-local/{path}").model_copy(
        update={"request_id": request_id()}
    )


def ensure_artifact_ref(artifact_id: str) -> c.ArtifactRef:
    if artifact_id not in repository().artifacts:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, f"Artifact {artifact_id} does not exist.")
    return repository().artifact_ref(artifact_id)


def current_user(request: Request) -> c.AuthUser:
    return auth().authenticate_token(request.cookies.get(SESSION_COOKIE))


def require_role(request: Request, minimum: c.UserRole) -> c.AuthUser:
    user = current_user(request)
    auth().require_role(user, minimum)
    return user


def node_error_response(exc: NodeExecutionError, *, status_override: int | None = None) -> JSONResponse:
    error = exc.error.model_copy(update={"request_id": request_id()})
    status = 400
    if error.code in {c.ErrorCode.auth_unauthorized, c.ErrorCode.auth_invalid_credentials}:
        status = 401
    elif error.code in {c.ErrorCode.auth_forbidden, c.ErrorCode.auth_user_disabled}:
        status = 403
    elif error.code == c.ErrorCode.idempotency_conflict:
        status = 409
    elif error.code in {c.ErrorCode.artifact_missing, c.ErrorCode.validation_missing_case}:
        status = 404
    return JSONResponse(
        status_code=status_override or status,
        content=c.ErrorEnvelope(error=error).model_dump(mode="json"),
        headers={"X-Request-Id": error.request_id or request_id()},
    )


def not_found_response(message: str) -> JSONResponse:
    return node_error_response(NodeExecutionError(c.ErrorCode.artifact_missing, message), status_override=404)


@app.exception_handler(NodeExecutionError)
async def node_error_handler(request: Request, exc: NodeExecutionError) -> JSONResponse:
    return node_error_response(exc)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return node_error_response(
        NodeExecutionError(
            c.ErrorCode.validation_invalid_options,
            "Request validation failed.",
            details={"errors": jsonable_encoder(exc.errors())},
        ),
        status_override=422,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = c.ErrorCode.validation_invalid_options
    if exc.status_code == 401:
        code = c.ErrorCode.auth_unauthorized
    elif exc.status_code == 403:
        code = c.ErrorCode.auth_forbidden
    elif exc.status_code == 404:
        code = c.ErrorCode.artifact_missing
    elif exc.status_code == 409:
        code = c.ErrorCode.idempotency_conflict
    return node_error_response(NodeExecutionError(code, str(exc.detail)))


def requires_authenticated_api(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return False
    if path in PUBLIC_PATHS or path in PUBLIC_API_PATHS:
        return False
    return path.startswith("/api/") and not any(path.startswith(prefix) for prefix in PUBLIC_API_PREFIXES)


@app.middleware("http")
async def authenticate_api_request(request: Request, call_next):
    token = REQUEST_ID_CONTEXT.set(request.headers.get("X-Request-Id") or f"req_{uuid4().hex[:12]}")
    request.state.request_id = request_id()
    user: c.AuthUser | None = None
    try:
        if requires_authenticated_api(request.url.path, request.method):
            try:
                user = current_user(request)
            except NodeExecutionError as exc:
                return node_error_response(exc)
        idempotency_key = request.headers.get("Idempotency-Key")
        if user is not None and idempotency_key and request.method in IDEMPOTENT_WRITE_METHODS:
            body = await request.body()
            request_hash = hashlib.sha256(body).hexdigest()
            record_key = f"{user.id}:{idempotency_key}"
            record_method = request.method
            record_path = request.url.path
            store = idempotency_repository()
            existing = (
                store.get(key=record_key, method=record_method, path=record_path, now=c.utcnow())
                if store is not None
                else repository().idempotency_records.get(f"{record_key}:{record_method}:{record_path}")
            )
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    return node_error_response(
                        NodeExecutionError(
                            c.ErrorCode.idempotency_conflict,
                            "Idempotency-Key was already used with a different request body.",
                        )
                    )
                replay = JSONResponse(
                    status_code=200,
                    content=existing["content"],
                    headers={"Idempotency-Replayed": "true"},
                )
                replay.headers["X-Request-Id"] = request_id()
                return replay

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            replayable_request = Request(request.scope, receive)
            response = await call_next(replayable_request)
            if 200 <= response.status_code < 300:
                response_body = b""
                async for chunk in response.body_iterator:
                    response_body += chunk
                try:
                    content = json.loads(response_body) if response_body else None
                except json.JSONDecodeError:
                    response.headers["X-Request-Id"] = request_id()
                    return Response(
                        content=response_body,
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        media_type=response.media_type,
                    )
                expires_at = c.utcnow() + timedelta(hours=24)
                if store is not None:
                    store.put(
                        key=record_key,
                        method=record_method,
                        path=record_path,
                        request_hash=request_hash,
                        response_status=response.status_code,
                        response_body=content,
                        expires_at=expires_at,
                    )
                else:
                    repository().idempotency_records[f"{record_key}:{record_method}:{record_path}"] = {
                        "request_hash": request_hash,
                        "content": content,
                        "status_code": response.status_code,
                        "expires_at": expires_at,
                    }
                response.headers["X-Request-Id"] = request_id()
                return Response(
                    content=response_body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )
            response.headers["X-Request-Id"] = request_id()
            return response
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id()
        return response
    finally:
        REQUEST_ID_CONTEXT.reset(token)


@app.get("/api/health", response_model=c.OkResponse)
def health() -> c.OkResponse:
    return c.OkResponse(request_id=request_id())


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    return metric_snapshot(repository())


@app.post("/api/auth/register", response_model=c.AuthResponse, status_code=201)
def register(payload: c.RegisterRequest, response: Response) -> c.AuthResponse:
    auth_response, token = auth().register(payload)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return auth_response.model_copy(update={"request_id": request_id()})


@app.post("/api/auth/login", response_model=c.AuthResponse)
def login(payload: c.LoginRequest, response: Response) -> c.AuthResponse:
    auth_response, token = auth().login(payload.email, payload.password)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return auth_response.model_copy(update={"request_id": request_id()})


@app.post("/api/auth/logout", response_model=c.OkResponse)
def logout(request: Request, response: Response) -> c.OkResponse:
    current_user(request)
    auth().logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE)
    return c.OkResponse(request_id=request_id())


@app.get("/api/auth/session", response_model=c.SessionInfo)
def session(request: Request) -> c.SessionInfo:
    user = auth().authenticate_token(request.cookies.get(SESSION_COOKIE))
    return auth().session_info(user, request_id())


@app.get("/api/auth/me", response_model=c.AuthUser)
def me(request: Request) -> c.AuthUser:
    return auth().authenticate_token(request.cookies.get(SESSION_COOKIE))


@app.patch("/api/auth/me", response_model=c.AuthUser)
def update_me(payload: c.UpdateMeRequest, request: Request) -> c.AuthUser:
    current_user = auth().authenticate_token(request.cookies.get(SESSION_COOKIE))
    if isinstance(auth(), SqlAlchemyAuthService):
        user = auth().update_me(current_user.id, payload)
        if user is None:
            raise NodeExecutionError(c.ErrorCode.auth_unauthorized, "User not found.")
        return user
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        return current_user
    return repository().patch(repository().users, current_user.id, updates)


@app.post("/api/auth/me/change-password", response_model=c.OkResponse)
def change_password(payload: c.ChangePasswordRequest, request: Request) -> c.OkResponse:
    current_user = auth().authenticate_token(request.cookies.get(SESSION_COOKIE))
    if isinstance(auth(), SqlAlchemyAuthService):
        auth().change_password(current_user.id, payload)
        return c.OkResponse(request_id=request_id())
    if not auth().verify_password(current_user.id, payload.old_password):
        raise NodeExecutionError(c.ErrorCode.auth_invalid_credentials, "Invalid credentials.")
    auth().repository.password_hashes[current_user.id] = auth().hash_password(payload.new_password)
    return c.OkResponse(request_id=request_id())


@app.get("/api/auth/users", response_model=c.PageResponse[c.AuthUser])
def list_users(request: Request, limit: int = 50) -> c.PageResponse[c.AuthUser]:
    require_role(request, c.UserRole.admin)
    if isinstance(auth(), SqlAlchemyAuthService):
        values = auth().list_users(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository().users.values(), limit)


@app.post("/api/auth/users", response_model=c.AuthUser, status_code=201)
def create_user(payload: c.AdminCreateUserRequest, request: Request) -> c.AuthUser:
    require_role(request, c.UserRole.admin)
    if isinstance(auth(), SqlAlchemyAuthService):
        return auth().create_user(payload)
    user = c.AuthUser(
        id=new_id("usr"),
        email=payload.email,
        display_name=payload.display_name,
        role=payload.role,
    )
    repository().users[user.id] = user
    repository().password_hashes[user.id] = auth().hash_password(payload.password or new_id("pwd"))
    return user


@app.patch("/api/auth/users/{user_id}", response_model=c.AuthUser)
def patch_user(user_id: str, payload: c.AdminUpdateUserRequest, request: Request) -> c.AuthUser:
    require_role(request, c.UserRole.admin)
    if isinstance(auth(), SqlAlchemyAuthService):
        user = auth().patch_user(user_id, payload)
        if user is None:
            raise NodeExecutionError(c.ErrorCode.auth_unauthorized, "User not found.")
        return user
    if user_id not in repository().users:
        raise NodeExecutionError(c.ErrorCode.auth_unauthorized, "User not found.")
    updates = payload.model_dump(exclude_none=True)
    return repository().patch(repository().users, user_id, updates)


@app.get("/api/auth/registration-codes", response_model=c.PageResponse[c.RegistrationCodePreview])
def registration_codes(request: Request, limit: int = 50) -> c.PageResponse[c.RegistrationCodePreview]:
    require_role(request, c.UserRole.admin)
    if isinstance(auth(), SqlAlchemyAuthService):
        values = auth().list_registration_codes(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository().registration_codes.values(), limit)


@app.post(
    "/api/auth/registration-codes",
    response_model=c.RegistrationCodePreview,
    status_code=201,
)
def create_registration_code(
    payload: c.CreateRegistrationCodeRequest, request: Request
) -> c.RegistrationCodePreview:
    require_role(request, c.UserRole.admin)
    if isinstance(auth(), SqlAlchemyAuthService):
        return auth().create_registration_code(payload)
    plaintext_code = new_id("reg_code")
    code = c.RegistrationCodePreview(
        id=new_id("reg"),
        role=payload.role,
        status="active",
        max_uses=payload.max_uses,
        used_count=0,
        expires_at=payload.expires_at,
        created_at=c.utcnow(),
    )
    repository().registration_codes[code.id] = code
    repository().registration_code_hashes[hash_registration_code(plaintext_code)] = code.id
    return code


@app.patch(
    "/api/auth/registration-codes/{code_id}",
    response_model=c.RegistrationCodePreview,
)
def patch_registration_code(
    code_id: str, payload: c.UpdateRegistrationCodeRequest, request: Request
) -> c.RegistrationCodePreview:
    require_role(request, c.UserRole.admin)
    if isinstance(auth(), SqlAlchemyAuthService):
        code = auth().patch_registration_code(code_id, payload)
        if code is None:
            raise NodeExecutionError(c.ErrorCode.auth_registration_closed, "Registration code not found.")
        return code
    return repository().patch(repository().registration_codes, code_id, payload.model_dump(exclude_none=True))


@app.post("/api/uploads/prepare", response_model=c.UploadSession, status_code=201)
def prepare_upload(payload: c.PrepareUploadRequest, request: Request) -> c.UploadSession:
    require_role(request, c.UserRole.operator)
    object_ref = object_store().prepare_upload(payload.filename, payload.kind.value)
    upload = c.UploadSession(
        id=new_id("upl"),
        kind=payload.kind,
        case_id=payload.case_id,
        filename=payload.filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
        sha256=payload.sha256,
        upload_url=object_store().signed_url(object_ref.uri).url,
        object_uri=object_ref.uri,
    )
    if upload_repository() is not None:
        return upload_repository().create_upload(upload)
    repository().uploads[upload.id] = upload
    return upload


@app.put("/api/uploads/{upload_session_id}/file", response_model=c.UploadSession)
async def upload_file(
    upload_session_id: str, request: Request, file: UploadFile | None = None
) -> c.UploadSession:
    require_role(request, c.UserRole.operator)
    if upload_repository() is not None:
        upload = upload_repository().get_upload(upload_session_id)
    else:
        upload = repository().uploads.get(upload_session_id)
    if upload is None:
        raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload session not found.")
    if file is not None and upload.object_uri:
        content = await file.read()
        stored = object_store().put_bytes(parse_local_uri(upload.object_uri), content)
        if stored.size_bytes != upload.size_bytes:
            raise NodeExecutionError(c.ErrorCode.upload_size_mismatch, "Upload size mismatch.")
        if upload.sha256 and upload.sha256 != stored.sha256:
            raise NodeExecutionError(c.ErrorCode.upload_sha256_mismatch, "Upload sha256 mismatch.")
        updates = {"status": c.UploadStatus.uploading, "sha256": upload.sha256 or stored.sha256}
    else:
        updates = {"status": c.UploadStatus.uploading}
    if upload_repository() is not None:
        return upload_repository().patch_upload(upload_session_id, updates)
    if "status" in updates:
        assert_transition("upload_session", upload.status, updates["status"])
    return repository().patch(repository().uploads, upload_session_id, updates)


@app.post("/api/uploads/complete", response_model=c.CompleteUploadResponse)
def complete_upload(payload: c.CompleteUploadRequest, request: Request) -> c.CompleteUploadResponse:
    require_role(request, c.UserRole.operator)
    upload = (
        upload_repository().get_upload(payload.upload_session_id)
        if upload_repository() is not None
        else repository().uploads[payload.upload_session_id]
    )
    if upload is None:
        raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload session not found.")
    assert_transition("upload_session", upload.status, c.UploadStatus.completed)
    if payload.size_bytes is not None and payload.size_bytes != upload.size_bytes:
        raise NodeExecutionError(c.ErrorCode.upload_size_mismatch, "Upload size mismatch.")
    if upload.sha256 and payload.sha256 and upload.sha256 != payload.sha256:
        raise NodeExecutionError(c.ErrorCode.upload_sha256_mismatch, "Upload sha256 mismatch.")
    if upload_repository() is not None:
        upload = upload_repository().patch_upload(upload.id, {"status": c.UploadStatus.completed})
        artifact = upload_repository().create_artifact_from_upload(upload)
        artifact_ref = upload_repository().artifact_ref(artifact.id)
    else:
        upload = repository().patch(repository().uploads, upload.id, {"status": c.UploadStatus.completed})
        artifact = repository().create_artifact(
            kind=c.ArtifactKind.uploaded_file,
            payload_schema="UploadedFileArtifact.v1",
            payload=upload.model_dump(mode="json"),
            uri=upload.object_uri,
            sha256=upload.sha256,
        )
        artifact_ref = repository().artifact_ref(artifact.id)
    media_asset = None
    publish_package = None
    if upload.kind in {
        c.UploadKind.portrait,
        c.UploadKind.broll,
        c.UploadKind.bgm,
        c.UploadKind.font,
        c.UploadKind.cover_template,
    }:
        media_payload = c.CreateMediaAssetFromUploadRequest(
            upload_session_id=upload.id,
            case_id=upload.case_id,
            title=payload.metadata.get("title") or upload.filename,
            kind=upload.kind.value,
            tags=[upload.kind.value, "upload"],
        )
        if media_repository() is not None:
            media_asset = media_repository().create_asset_from_upload(media_payload)
        else:
            media_asset = c.MediaAssetRecord(
                id=new_id("asset"),
                case_id=media_payload.case_id,
                title=media_payload.title,
                kind=media_payload.kind,
                source_artifact_id=artifact.id,
                tags=media_payload.tags,
            )
            repository().media_assets[media_asset.id] = media_asset
    elif upload.kind == c.UploadKind.publish_video:
        package_payload = c.CreatePublishPackageRequest(
            upload_artifact_id=artifact.id,
            title=payload.metadata.get("title") or upload.filename,
            description=payload.metadata.get("description", ""),
        )
        if publishing_repository() is not None:
            publish_package = publishing_repository().create_package(package_payload)
        else:
            publish_package = c.PublishPackage(
                id=new_id("pkg"),
                case_id=upload.case_id,
                upload_artifact_id=artifact.id,
                video_artifact=artifact_ref,
                platform_defaults=c.PublishDefaults(
                    title=package_payload.title,
                    description=package_payload.description,
                ),
            )
            repository().publish_packages[publish_package.id] = publish_package
    return c.CompleteUploadResponse(
        upload_session=upload,
        artifact=artifact_ref,
        media_asset=media_asset,
        publish_package=publish_package,
        request_id=request_id(),
    )


@app.post("/api/uploads/{upload_session_id}/cancel", response_model=c.UploadSession)
def cancel_upload(upload_session_id: str, request: Request) -> c.UploadSession:
    require_role(request, c.UserRole.operator)
    if upload_repository() is not None:
        return upload_repository().patch_upload(
            upload_session_id,
            {"status": c.UploadStatus.cancelled},
        )
    upload = repository().uploads[upload_session_id]
    assert_transition("upload_session", upload.status, c.UploadStatus.cancelled)
    return repository().patch(repository().uploads, upload_session_id, {"status": c.UploadStatus.cancelled})


@app.get("/api/uploads/{upload_session_id}", response_model=c.UploadSession)
def get_upload(upload_session_id: str, request: Request) -> c.UploadSession:
    require_role(request, c.UserRole.operator)
    if upload_repository() is not None:
        upload = upload_repository().get_upload(upload_session_id)
        if upload is None:
            raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload session not found.")
        return upload
    return repository().uploads[upload_session_id]


@app.get("/api/secrets", response_model=c.PageResponse[c.SecretPreview])
def list_secrets(request: Request, limit: int = 50) -> c.PageResponse[c.SecretPreview]:
    require_role(request, c.UserRole.admin)
    if secret_repository() is not None:
        values = secret_repository().list_secrets(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository().secrets.values(), limit)


@app.post("/api/secrets", response_model=c.SecretPreview, status_code=201)
def create_secret(payload: c.CreateSecretRequest, request: Request) -> c.SecretPreview:
    require_role(request, c.UserRole.admin)
    if secret_repository() is not None:
        return secret_repository().create_secret(payload)
    secret = c.SecretPreview(
        id=new_id("sec"),
        provider_id=payload.provider_id,
        environment=payload.environment,
        name=payload.name,
        secret_ref=secret_store().put(payload.plaintext_secret, secret_ref=f"{new_id('sec')}.secret"),
    )
    repository().secrets[secret.id] = secret
    return secret


@app.post("/api/secrets/{secret_id}/rotate", response_model=c.SecretPreview)
def rotate_secret(secret_id: str, payload: c.RotateSecretRequest, request: Request) -> c.SecretPreview:
    require_role(request, c.UserRole.admin)
    if secret_repository() is not None:
        return secret_repository().rotate_secret(secret_id, payload)
    old_secret = repository().secrets[secret_id]
    repository().secrets[secret_id] = old_secret.model_copy(
        update={"status": c.SecretStatus.rotated, "rotated_at": c.utcnow(), "updated_at": c.utcnow()}
    )
    new_secret = c.SecretPreview(
        id=new_id("sec"),
        provider_id=old_secret.provider_id,
        environment=old_secret.environment,
        name=old_secret.name,
        secret_ref=secret_store().put(payload.plaintext_secret, secret_ref=f"{new_id('sec')}.secret"),
        rotated_from_secret_id=old_secret.id,
    )
    repository().secrets[new_secret.id] = new_secret
    return new_secret


@app.patch("/api/secrets/{secret_id}/disable", response_model=c.SecretPreview)
def disable_secret(secret_id: str, payload: c.DisableSecretRequest, request: Request) -> c.SecretPreview:
    require_role(request, c.UserRole.admin)
    if secret_repository() is not None:
        return secret_repository().disable_secret(secret_id, payload)
    secret = repository().secrets[secret_id]
    if secret.secret_ref:
        secret_store().disable(secret.secret_ref)
    return repository().patch(repository().secrets, secret_id, {"status": c.SecretStatus.disabled, "disabled_at": c.utcnow()})


@app.get("/api/cases", response_model=c.PageResponse[c.CaseListItem])
def list_cases(
    limit: int = 50,
    search: str | None = None,
    owner_user_id: str | None = None,
) -> c.PageResponse[c.CaseListItem]:
    if case_repository() is not None:
        values = case_repository().list_cases(
            search=search,
            owner_user_id=owner_user_id,
            limit=limit,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    values = list(repository().cases.values())
    if search:
        values = [case for case in values if search.lower() in case.name.lower()]
    if owner_user_id:
        values = [case for case in values if case.owner_user_id == owner_user_id]
    return page(values, limit)


@app.post("/api/cases", response_model=c.CaseDetail, status_code=201)
def create_case(payload: c.CreateCaseRequest, request: Request) -> c.CaseDetail:
    user = require_role(request, c.UserRole.operator)
    if case_repository() is not None:
        return case_repository().create_case(payload, owner_user_id=user.id)
    case = c.CaseDetail(id=new_id("case"), owner_user_id=user.id, **payload.model_dump())
    repository().cases[case.id] = case
    return case


@app.get("/api/cases/{case_id}", response_model=c.CaseDetail)
def case_detail(case_id: str) -> c.CaseDetail:
    return get_case(case_id)


@app.patch("/api/cases/{case_id}", response_model=c.CaseDetail)
def patch_case(case_id: str, payload: c.PatchCaseRequest, request: Request) -> c.CaseDetail:
    require_role(request, c.UserRole.operator)
    if case_repository() is not None:
        case = case_repository().patch_case(case_id, payload)
        if case is None:
            raise NodeExecutionError(c.ErrorCode.validation_missing_case, f"Case {case_id} does not exist.")
        return case
    get_case(case_id)
    return repository().patch(repository().cases, case_id, payload.model_dump(exclude_none=True))


@app.post("/api/jobs/digital-human-video", response_model=c.CreateJobResponse, status_code=201)
def create_digital_human_job(
    payload: c.CreateDigitalHumanVideoJobRequest, request: Request
) -> c.CreateJobResponse:
    require_role(request, c.UserRole.operator)
    case = get_case(payload.case_id)
    if payload.case_id not in repository().cases:
        repository().cases[payload.case_id] = case
    job = c.Job(
        id=new_id("job"),
        type=c.JobType.digital_human_video,
        case_id=payload.case_id,
        created_by="usr_admin",
        request_schema=payload.schema_version,
        request=payload,
    )
    repository().jobs[job.id] = job
    run = workflow_runtime().start_digital_human_run(job_id=job.id, mode="new")
    if production_repository() is not None:
        production_repository().sync_workflow_snapshot(
            job=repository().jobs[job.id],
            run=run,
            repository=repository(),
        )
    return c.CreateJobResponse(job=repository().jobs[job.id], initial_run=run, request_id=request_id())


@app.get("/api/jobs/{job_id}", response_model=c.JobDetailResponse)
def job_detail(job_id: str) -> c.JobDetailResponse:
    if production_repository() is not None:
        detail = production_repository().job_detail(job_id, request_id())
        if detail is not None:
            return detail
    job = repository().jobs[job_id]
    runs = [run for run in repository().runs.values() if run.job_id == job_id]
    return c.JobDetailResponse(
        job=job,
        runs=runs,
        latest_report_artifact_id=runs[-1].public_report_artifact_id if runs else None,
        request_id=request_id(),
    )


@app.post("/api/jobs/{job_id}/runs", response_model=c.WorkflowRunResponse, status_code=201)
def create_run(job_id: str, payload: c.CreateRunRequest, request: Request) -> c.WorkflowRunResponse:
    require_role(request, c.UserRole.operator)
    previous = repository().jobs[job_id].active_run_id
    run = workflow_runtime().start_digital_human_run(
        job_id=job_id,
        mode=payload.mode,
        from_run_id=previous if payload.mode in {"retry", "resume"} else None,
        reason=payload.reason,
    )
    if production_repository() is not None:
        production_repository().sync_workflow_snapshot(
            job=repository().jobs[job_id],
            run=run,
            repository=repository(),
        )
    return c.WorkflowRunResponse(run=run, request_id=request_id())


@app.get("/api/runs/{run_id}", response_model=c.RunDetailResponse)
def run_detail(run_id: str) -> c.RunDetailResponse:
    if production_repository() is not None:
        detail = production_repository().run_detail(run_id, request_id())
        if detail is not None:
            return detail
    run = repository().runs[run_id]
    node_runs = repository().node_runs.get(run_id, [])
    artifacts = [
        repository().artifact_ref(artifact.id) for artifact in repository().artifacts.values() if artifact.run_id == run_id
    ]
    return c.RunDetailResponse(run=run, node_runs=node_runs, artifacts=artifacts, request_id=request_id())


@app.post("/api/runs/{run_id}/cancel", response_model=c.RunActionResponse, status_code=202)
def cancel_run(run_id: str, payload: c.CancelRunRequest, request: Request) -> c.RunActionResponse:
    require_role(request, c.UserRole.operator)
    run = workflow_runtime().cancel_run(run_id, force=payload.force, reason=payload.reason)
    if production_repository() is not None:
        production_repository().sync_workflow_snapshot(
            job=repository().jobs[run.job_id],
            run=run,
            repository=repository(),
        )
    return c.RunActionResponse(run=run, accepted=True, request_id=request_id())


@app.post("/api/runs/{run_id}/retry", response_model=c.RetryRunResponse, status_code=201)
def retry_run(run_id: str, payload: c.RetryRunRequest, request: Request) -> c.RetryRunResponse:
    require_role(request, c.UserRole.operator)
    run = repository().runs[run_id]
    new_run = workflow_runtime().start_digital_human_run(
        job_id=run.job_id,
        mode="retry",
        from_run_id=run_id,
        reason=payload.reason,
    )
    if production_repository() is not None:
        production_repository().sync_workflow_snapshot(
            job=repository().jobs[new_run.job_id],
            run=new_run,
            repository=repository(),
        )
    return c.RetryRunResponse(run=new_run, request_id=request_id())


@app.post("/api/runs/{run_id}/resume", response_model=c.ResumeRunResponse, status_code=201)
def resume_run(run_id: str, payload: c.ResumeRunRequest, request: Request) -> c.ResumeRunResponse:
    require_role(request, c.UserRole.operator)
    run = repository().runs[run_id]
    new_run = workflow_runtime().start_digital_human_run(
        job_id=run.job_id,
        mode="resume",
        from_run_id=run_id if payload.reuse_valid_artifacts else None,
        reason=payload.reason,
    )
    if production_repository() is not None:
        production_repository().sync_workflow_snapshot(
            job=repository().jobs[new_run.job_id],
            run=new_run,
            repository=repository(),
        )
    return c.ResumeRunResponse(run=new_run, request_id=request_id())


@app.get("/api/runs/{run_id}/report", response_model=c.RunReportResponse)
def run_report(run_id: str) -> c.RunReportResponse:
    if production_repository() is not None:
        report = production_repository().run_report(run_id, request_id())
        if report is not None:
            return report
    run = repository().runs[run_id]
    if not run.public_report_artifact_id:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Run report is not available.")
    public_payload = repository().artifacts[run.public_report_artifact_id].payload
    debug_payload = (
        repository().artifacts[run.debug_report_artifact_id].payload if run.debug_report_artifact_id else None
    )
    return c.RunReportResponse(
        public_report=c.RunPublicReportArtifact.model_validate(public_payload),
        debug_report=c.RunDebugReportArtifact.model_validate(debug_payload) if debug_payload else None,
        request_id=request_id(),
    )


@app.get("/api/runs/{run_id}/artifacts", response_model=c.RunArtifactsResponse)
def run_artifacts(run_id: str) -> c.RunArtifactsResponse:
    if production_repository() is not None:
        response = production_repository().run_artifacts(run_id, request_id())
        if response is not None:
            return response
    refs = [repository().artifact_ref(item.id) for item in repository().artifacts.values() if item.run_id == run_id]
    return c.RunArtifactsResponse(run_id=run_id, artifacts=refs, request_id=request_id())


@app.get("/api/runs/{run_id}/events", response_model=c.EventStreamTokenResponse)
def run_events(run_id: str) -> c.EventStreamTokenResponse:
    if production_repository() is not None and not production_repository().run_exists(run_id):
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, f"Run {run_id} does not exist.")
    return c.EventStreamTokenResponse(
        stream_url=f"/api/ws/runs/{run_id}",
        token=new_id("stream"),
        expires_at=c.utcnow() + timedelta(minutes=10),
        request_id=request_id(),
    )


@app.get("/api/media/assets", response_model=c.PageResponse[c.MediaAssetCard])
def list_media_assets(
    limit: int = 50,
    case_id: str | None = None,
    kind: str | None = None,
    annotation_status: str | None = None,
) -> c.PageResponse[c.MediaAssetCard]:
    if media_repository() is not None:
        values = media_repository().list_assets(
            limit=limit,
            case_id=case_id,
            kind=kind,
            annotation_status=annotation_status,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    assets = list(repository().media_assets.values())
    if case_id:
        assets = [asset for asset in assets if asset.case_id == case_id]
    if kind:
        assets = [asset for asset in assets if asset.kind == kind]
    if annotation_status:
        assets = [asset for asset in assets if asset.annotation_status == annotation_status]
    return page([c.MediaAssetCard(asset=asset, preview_url=f"local://media/{asset.id}") for asset in assets], limit)


@app.post("/api/media/assets", response_model=c.MediaAssetRecord, status_code=201)
def create_media_asset(payload: c.CreateMediaAssetFromUploadRequest, request: Request) -> c.MediaAssetRecord:
    require_role(request, c.UserRole.operator)
    if media_repository() is not None:
        return media_repository().create_asset_from_upload(payload)
    upload = repository().uploads[payload.upload_session_id]
    if upload.status != c.UploadStatus.completed:
        raise NodeExecutionError(c.ErrorCode.upload_invalid_state, "Upload must be completed first.")
    asset = c.MediaAssetRecord(
        id=new_id("asset"),
        case_id=payload.case_id,
        title=payload.title,
        kind=payload.kind,
        source_artifact_id=upload.id,
        tags=payload.tags,
    )
    repository().media_assets[asset.id] = asset
    return asset


@app.get("/api/media/assets/{asset_id}", response_model=c.MediaAssetDetail)
def media_asset_detail(asset_id: str) -> c.MediaAssetDetail:
    if media_repository() is not None:
        detail = media_repository().get_asset_detail(asset_id)
        if detail is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        return detail
    asset = repository().media_assets[asset_id]
    return c.MediaAssetDetail(asset=asset, preview_url=f"local://media/{asset.id}")


@app.get("/api/media/assets/{asset_id}/preview-url", response_model=c.SignedUrlResponse)
def media_asset_preview(asset_id: str) -> c.SignedUrlResponse:
    if media_repository() is not None:
        uri = media_repository().artifact_uri_for_asset(asset_id)
        if uri is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        if uri:
            return object_store().signed_url(uri).model_copy(update={"request_id": request_id()})
        return signed(f"media/{asset_id}")
    if asset_id not in repository().media_assets:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
    asset = repository().media_assets[asset_id]
    if asset.source_artifact_id and asset.source_artifact_id in repository().artifacts:
        artifact = repository().artifacts[asset.source_artifact_id]
        if artifact.uri:
            return object_store().signed_url(artifact.uri).model_copy(update={"request_id": request_id()})
    return signed(f"media/{asset_id}")


@app.get("/api/annotations/{asset_id}", response_model=c.AnnotationEditorVm)
def get_annotation(asset_id: str) -> c.AnnotationEditorVm:
    if media_repository() is not None:
        editor = media_repository().get_or_create_annotation(asset_id)
        if editor is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        return editor
    asset = repository().media_assets[asset_id]
    if asset_id not in repository().annotations:
        repository().annotations[asset_id] = c.AnnotationEditorVm(
            asset=asset,
            etag=new_id("etag"),
            canonical={"labels": asset.tags, "kind": asset.kind},
            projection={"title": asset.title, "usable": asset.usable},
            editable_paths=["/labels", "/usable", "/title"],
        )
    return repository().annotations[asset_id]


@app.patch("/api/annotations/{asset_id}", response_model=c.AnnotationEditorVm)
def patch_annotation(asset_id: str, payload: c.PatchAnnotationRequest, request: Request) -> c.AnnotationEditorVm:
    require_role(request, c.UserRole.operator)
    if media_repository() is not None:
        editor = media_repository().patch_annotation(asset_id, payload)
        if editor is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        return editor
    editor = get_annotation(asset_id)
    updated = editor.model_copy(update={"etag": new_id("etag")})
    repository().annotations[asset_id] = updated
    repository().media_assets[asset_id] = repository().media_assets[asset_id].model_copy(
        update={"annotation_status": "annotated", "updated_at": c.utcnow()}
    )
    return updated


@app.post("/api/annotations/{asset_id}/rerun", response_model=c.AnnotationRunResponse, status_code=202)
def rerun_annotation(
    asset_id: str, payload: c.RerunAnnotationRequest, request: Request
) -> c.AnnotationRunResponse:
    require_role(request, c.UserRole.operator)
    if media_repository() is not None:
        response = media_repository().rerun_annotation(asset_id, payload)
        if response is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Asset missing.")
        return response
    repository().media_assets[asset_id] = repository().media_assets[asset_id].model_copy(
        update={"annotation_status": "annotated", "updated_at": c.utcnow()}
    )
    return c.AnnotationRunResponse(asset_id=asset_id, run_id=None, status="completed")


@app.get("/api/voices", response_model=c.PageResponse[c.VoiceProfile])
def list_voices(
    limit: int = 50,
    source: str | None = None,
    enabled: bool | None = None,
) -> c.PageResponse[c.VoiceProfile]:
    if media_repository() is not None:
        values = media_repository().list_voices(source=source, enabled=enabled, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    values = list(repository().voices.values())
    if source:
        values = [voice for voice in values if voice.source == source]
    if enabled is not None:
        values = [voice for voice in values if voice.enabled == enabled]
    return page(values, limit)


@app.post("/api/voices/clone", response_model=c.VoiceProfile, status_code=202)
def clone_voice(payload: c.CloneVoiceRequest, request: Request) -> c.VoiceProfile:
    require_role(request, c.UserRole.operator)
    if media_repository() is not None:
        return media_repository().clone_voice(payload)
    voice = c.VoiceProfile(
        id=new_id("voice"),
        display_name=payload.display_name,
        source="cloned",
        provider_profile_id=payload.provider_profile_id or "sandbox.tts.default",
    )
    repository().voices[voice.id] = voice
    return voice


@app.post("/api/voices/design", response_model=c.VoiceProfile, status_code=202)
def design_voice(payload: c.DesignVoiceRequest, request: Request) -> c.VoiceProfile:
    require_role(request, c.UserRole.operator)
    if media_repository() is not None:
        return media_repository().design_voice(payload)
    voice = c.VoiceProfile(
        id=new_id("voice"),
        display_name=payload.display_name,
        source="designed",
        provider_profile_id=payload.provider_profile_id or "sandbox.tts.default",
    )
    repository().voices[voice.id] = voice
    return voice


@app.post("/api/voices/{voice_id}/preview", response_model=c.VoicePreviewResponse)
def voice_preview(voice_id: str, payload: c.VoicePreviewRequest, request: Request) -> c.VoicePreviewResponse:
    require_role(request, c.UserRole.operator)
    if media_repository() is not None:
        response = media_repository().preview_voice(voice_id, payload)
        if response is None:
            raise NodeExecutionError(c.ErrorCode.validation_missing_voice, "Voice not found.")
        return response
    if voice_id not in repository().voices:
        raise NodeExecutionError(c.ErrorCode.validation_missing_voice, "Voice not found.")
    artifact = repository().create_artifact(
        kind=c.ArtifactKind.audio_tts,
        payload_schema="VoicePreviewArtifact.v1",
        payload={"text": payload.text},
        uri=f"sandbox://voice-preview/{voice_id}.wav",
    )
    return c.VoicePreviewResponse(
        voice_id=voice_id,
        audio_artifact=repository().artifact_ref(artifact.id),
        duration_sec=max(1, len(payload.text) / 6),
    )


@app.patch("/api/voices/{voice_id}", response_model=c.VoiceProfile)
def patch_voice(voice_id: str, payload: c.PatchVoiceRequest, request: Request) -> c.VoiceProfile:
    require_role(request, c.UserRole.operator)
    if media_repository() is not None:
        voice = media_repository().patch_voice(voice_id, payload)
        if voice is None:
            raise NodeExecutionError(c.ErrorCode.validation_missing_voice, "Voice not found.")
        return voice
    return repository().patch(repository().voices, voice_id, payload.model_dump(exclude_none=True))


@app.delete("/api/voices/{voice_id}", response_model=c.OkResponse)
def delete_voice(voice_id: str, request: Request) -> c.OkResponse:
    require_role(request, c.UserRole.admin)
    if media_repository() is not None:
        media_repository().delete_voice(voice_id)
        return c.OkResponse(request_id=request_id())
    repository().voices.pop(voice_id, None)
    return c.OkResponse(request_id=request_id())


@app.get("/api/prompts", response_model=c.PageResponse[c.PromptTemplateView])
def list_prompts(
    limit: int = 50,
    status: str | None = None,
    purpose: str | None = None,
) -> c.PageResponse[c.PromptTemplateView]:
    if prompt_repository() is not None:
        values = prompt_repository().list_templates(status=status, purpose=purpose, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    views = []
    for template in repository().prompt_templates.values():
        if status and template.status != status:
            continue
        if purpose and template.purpose != purpose:
            continue
        published = next(
            (
                version
                for version in repository().prompt_versions.values()
                if version.prompt_template_id == template.id and version.status == "published"
            ),
            None,
        )
        views.append(c.PromptTemplateView(template=template, published_version=published))
    return page(views, limit)


@app.post("/api/prompts", response_model=c.PromptTemplateView, status_code=201)
def create_prompt(payload: c.CreatePromptTemplateRequest, request: Request) -> c.PromptTemplateView:
    require_role(request, c.UserRole.admin)
    if prompt_repository() is not None:
        return prompt_repository().create_template(payload)
    template = c.PromptTemplate(id=new_id("prompt"), status="draft", **payload.model_dump())
    repository().prompt_templates[template.id] = template
    return c.PromptTemplateView(template=template)


@app.get("/api/prompts/{template_id}/versions", response_model=c.PageResponse[c.PromptVersionView])
def prompt_versions(template_id: str, limit: int = 50) -> c.PageResponse[c.PromptVersionView]:
    if prompt_repository() is not None:
        values = prompt_repository().list_versions(template_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    template = repository().prompt_templates[template_id]
    versions = [
        c.PromptVersionView(version=version, template=template)
        for version in repository().prompt_versions.values()
        if version.prompt_template_id == template_id
    ]
    return page(versions, limit)


@app.post(
    "/api/prompts/{template_id}/versions",
    response_model=c.PromptVersionView,
    status_code=201,
)
def create_prompt_version(
    template_id: str, payload: c.CreatePromptVersionRequest, request: Request
) -> c.PromptVersionView:
    require_role(request, c.UserRole.admin)
    if prompt_repository() is not None:
        return prompt_repository().create_version(template_id, payload)
    version = c.PromptVersion(
        id=new_id("pver"),
        prompt_template_id=template_id,
        content=payload.content,
        changelog=payload.changelog,
    )
    repository().prompt_versions[version.id] = version
    return c.PromptVersionView(version=version, template=repository().prompt_templates[template_id])


@app.post(
    "/api/prompts/{template_id}/versions/{version_id}/approve",
    response_model=c.PromptVersionView,
)
def approve_prompt_version(
    template_id: str, version_id: str, payload: c.ApprovePromptVersionRequest, request: Request
) -> c.PromptVersionView:
    require_role(request, c.UserRole.admin)
    if prompt_repository() is not None:
        return prompt_repository().approve_version(template_id, version_id, payload)
    version = repository().prompt_versions[version_id]
    if version.status == "draft":
        assert_transition("prompt_version", version.status, "reviewing")
        version = repository().patch(repository().prompt_versions, version_id, {"status": "reviewing"})
    assert_transition("prompt_version", version.status, "approved")
    version = repository().patch(repository().prompt_versions, version_id, {"status": "approved", "approved_at": c.utcnow()})
    return c.PromptVersionView(version=version, template=repository().prompt_templates[template_id])


@app.post(
    "/api/prompts/{template_id}/versions/{version_id}/publish",
    response_model=c.PromptVersionView,
)
def publish_prompt_version(
    template_id: str, version_id: str, payload: c.PublishPromptVersionRequest, request: Request
) -> c.PromptVersionView:
    require_role(request, c.UserRole.admin)
    if prompt_repository() is not None:
        return prompt_repository().publish_version(template_id, version_id, payload)
    version = repository().prompt_versions[version_id]
    assert_transition("prompt_version", version.status, "published")
    version = repository().patch(repository().prompt_versions, version_id, {"status": "published", "published_at": c.utcnow()})
    return c.PromptVersionView(version=version, template=repository().prompt_templates[template_id])


@app.post("/api/prompts/{template_id}/rollback", response_model=c.PromptVersionView)
def rollback_prompt(
    template_id: str, payload: c.RollbackPromptRequest, request: Request
) -> c.PromptVersionView:
    require_role(request, c.UserRole.admin)
    if prompt_repository() is not None:
        return prompt_repository().rollback(template_id, payload)
    version = repository().prompt_versions[payload.target_version_id]
    assert_transition("prompt_version", version.status, "published")
    version = repository().patch(repository().prompt_versions, payload.target_version_id, {"status": "published"})
    return c.PromptVersionView(version=version, template=repository().prompt_templates[template_id])


@app.get("/api/prompts/bindings", response_model=c.PageResponse[c.PromptBindingView])
def prompt_bindings(limit: int = 50) -> c.PageResponse[c.PromptBindingView]:
    if prompt_repository() is not None:
        values = prompt_repository().list_bindings(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(
        [
            c.PromptBindingView(
                binding=binding,
                resolved_version=repository().prompt_versions.get(binding.prompt_version_id),
            )
            for binding in repository().prompt_bindings.values()
        ],
        limit,
    )


@app.post("/api/prompts/bindings", response_model=c.PromptBindingView, status_code=201)
def create_prompt_binding(payload: c.CreatePromptBindingRequest, request: Request) -> c.PromptBindingView:
    require_role(request, c.UserRole.admin)
    if prompt_repository() is not None:
        return prompt_repository().create_binding(payload)
    binding = c.PromptBinding(id=new_id("pbind"), **payload.model_dump())
    repository().prompt_bindings[binding.id] = binding
    return c.PromptBindingView(binding=binding, resolved_version=repository().prompt_versions.get(binding.prompt_version_id))


@app.patch("/api/prompts/bindings/{binding_id}", response_model=c.PromptBindingView)
def patch_prompt_binding(
    binding_id: str, payload: c.PatchPromptBindingRequest, request: Request
) -> c.PromptBindingView:
    require_role(request, c.UserRole.admin)
    if prompt_repository() is not None:
        return prompt_repository().patch_binding(binding_id, payload)
    binding = repository().patch(repository().prompt_bindings, binding_id, payload.model_dump(exclude_none=True))
    return c.PromptBindingView(binding=binding, resolved_version=repository().prompt_versions.get(binding.prompt_version_id))


@app.get("/api/prompts/experiments", response_model=c.PageResponse[c.PromptExperiment])
def prompt_experiments(
    limit: int = 50,
    prompt_template_id: str | None = None,
    status: str | None = None,
) -> c.PageResponse[c.PromptExperiment]:
    if prompt_repository() is not None:
        values = prompt_repository().list_experiments(
            prompt_template_id=prompt_template_id,
            status=status,
            limit=limit,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository().prompt_experiments.values(), limit)


@app.post("/api/prompts/experiments", response_model=c.PromptExperiment, status_code=201)
def create_prompt_experiment(
    payload: c.CreatePromptExperimentRequest, request: Request
) -> c.PromptExperiment:
    require_role(request, c.UserRole.admin)
    if prompt_repository() is not None:
        return prompt_repository().create_experiment(payload)
    experiment = c.PromptExperiment(id=new_id("pexp"), **payload.model_dump())
    repository().prompt_experiments[experiment.id] = experiment
    return experiment


@app.patch("/api/prompts/experiments/{experiment_id}", response_model=c.PromptExperiment)
def patch_prompt_experiment(
    experiment_id: str, payload: c.PatchPromptExperimentRequest, request: Request
) -> c.PromptExperiment:
    require_role(request, c.UserRole.admin)
    if prompt_repository() is not None:
        return prompt_repository().patch_experiment(experiment_id, payload)
    return repository().patch(repository().prompt_experiments, experiment_id, payload.model_dump(exclude_none=True))


@app.get("/api/providers/profiles", response_model=c.PageResponse[c.ProviderProfile])
def provider_profiles(
    limit: int = 50,
    provider_id: str | None = None,
    capability: str | None = None,
    environment: str | None = None,
) -> c.PageResponse[c.ProviderProfile]:
    if provider_repository() is not None:
        values = provider_repository().list_profiles(
            provider_id=provider_id,
            capability=capability,
            environment=environment,
            limit=limit,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    values = list(repository().provider_profiles.values())
    if provider_id:
        values = [profile for profile in values if profile.provider_id == provider_id]
    if capability:
        values = [profile for profile in values if profile.capability == capability]
    if environment:
        values = [profile for profile in values if profile.environment == environment]
    return page(values, limit)


@app.post("/api/providers/profiles", response_model=c.ProviderProfile, status_code=201)
def create_provider_profile(payload: c.CreateProviderProfileRequest, request: Request) -> c.ProviderProfile:
    require_role(request, c.UserRole.admin)
    if provider_repository() is not None:
        return provider_repository().create_profile(payload)
    profile = c.ProviderProfile(id=new_id("provider_profile"), **payload.model_dump())
    repository().provider_profiles[profile.id] = profile
    return profile


@app.patch("/api/providers/profiles/{profile_id}", response_model=c.ProviderProfile)
def patch_provider_profile(
    profile_id: str, payload: c.PatchProviderProfileRequest, request: Request
) -> c.ProviderProfile:
    require_role(request, c.UserRole.admin)
    if provider_repository() is not None:
        return provider_repository().patch_profile(profile_id, payload)
    return repository().patch(repository().provider_profiles, profile_id, payload.model_dump(exclude_none=True))


@app.post("/api/providers/profiles/{profile_id}/test", response_model=c.ProviderHealthCheckResponse)
def test_provider_profile(
    profile_id: str, payload: c.TestProviderProfileRequest, request: Request
) -> c.ProviderHealthCheckResponse:
    require_role(request, c.UserRole.admin)
    if provider_repository() is not None:
        return provider_repository().test_profile(profile_id, payload)
    return c.ProviderHealthCheckResponse(profile_id=profile_id, ok=profile_id in repository().provider_profiles, latency_ms=1)


@app.get("/api/providers/capabilities", response_model=list[c.ProviderCapability])
def provider_capabilities() -> list[c.ProviderCapability]:
    if provider_repository() is not None:
        return provider_repository().list_capabilities()
    return list(repository().provider_capabilities.values())


@app.get("/api/providers/price-catalogs", response_model=c.PageResponse[c.ProviderPriceCatalog])
def price_catalogs(
    limit: int = 50,
    provider_id: str | None = None,
    active_only: bool = False,
) -> c.PageResponse[c.ProviderPriceCatalog]:
    if provider_repository() is not None:
        values = provider_repository().list_price_catalogs(
            provider_id=provider_id,
            active_only=active_only,
            limit=limit,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    values = list(repository().price_catalogs.values())
    if provider_id:
        values = [catalog for catalog in values if catalog.provider_id == provider_id]
    if active_only:
        values = [catalog for catalog in values if catalog.status == "published"]
    return page(values, limit)


@app.post("/api/providers/price-catalogs", response_model=c.ProviderPriceCatalog, status_code=201)
def upsert_price_catalog(payload: c.UpsertPriceCatalogRequest, request: Request) -> c.ProviderPriceCatalog:
    require_role(request, c.UserRole.admin)
    if provider_repository() is not None:
        return provider_repository().upsert_price_catalog(payload)
    repository().price_catalogs[payload.catalog.id] = payload.catalog
    for item in payload.items:
        repository().price_items[item.id] = item
    return payload.catalog


@app.post("/api/providers/price-catalogs/{catalog_id}/approve", response_model=c.ProviderPriceCatalog)
def approve_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    require_role(request, c.UserRole.admin)
    if provider_repository() is not None:
        return provider_repository().patch_price_catalog_status(catalog_id, "approved", payload)
    return repository().patch(repository().price_catalogs, catalog_id, {"status": "approved"})


@app.post("/api/providers/price-catalogs/{catalog_id}/publish", response_model=c.ProviderPriceCatalog)
def publish_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    require_role(request, c.UserRole.admin)
    if provider_repository() is not None:
        return provider_repository().patch_price_catalog_status(catalog_id, "published", payload)
    return repository().patch(repository().price_catalogs, catalog_id, {"status": "published"})


@app.post("/api/providers/price-catalogs/{catalog_id}/deprecate", response_model=c.ProviderPriceCatalog)
def deprecate_price_catalog(
    catalog_id: str, payload: c.GovernedActionRequest, request: Request
) -> c.ProviderPriceCatalog:
    require_role(request, c.UserRole.admin)
    if provider_repository() is not None:
        return provider_repository().patch_price_catalog_status(catalog_id, "deprecated", payload)
    return repository().patch(repository().price_catalogs, catalog_id, {"status": "deprecated"})


@app.get("/api/providers/usage", response_model=c.ProviderUsageReport)
def provider_usage(
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    provider_id: str | None = None,
    case_id: str | None = None,
) -> c.ProviderUsageReport:
    if ops_repository() is not None:
        return ops_repository().provider_usage(
            window_start=window_start,
            window_end=window_end,
            provider_id=provider_id,
            case_id=case_id,
        )
    invocations = list(repository().provider_invocations.values())
    if provider_id:
        invocations = [item for item in invocations if item.provider_id == provider_id]
    if case_id:
        invocations = [item for item in invocations if item.case_id == case_id]
    amount = sum((item.estimated_cost.amount for item in invocations if item.estimated_cost), c.Decimal("0"))
    return c.ProviderUsageReport(
        invocations=len(invocations),
        estimated_cost=c.Money(amount=amount, currency="CNY"),
        unpriced_invocation_count=len([item for item in invocations if item.billing_status == "unpriced"]),
    )


@app.get("/api/providers/balances", response_model=c.ProviderBalanceReport)
def provider_balances(
    request: Request,
    provider_id: str | None = None,
    environment: str | None = None,
) -> c.ProviderBalanceReport:
    require_role(request, c.UserRole.admin)
    if provider_repository() is not None:
        return provider_repository().balances(
            request_id=request_id(),
            provider_id=provider_id,
            environment=environment,
        )
    providers = sorted({profile.provider_id for profile in repository().provider_profiles.values()})
    return c.ProviderBalanceReport(
        items=[
            c.ProviderBalanceItem(
                provider_id=provider_id,
                balance=c.Money(amount=9999, currency="CNY"),
                quota_remaining=1_000_000,
                checked_at=c.utcnow(),
                status="ok",
            )
            for provider_id in providers
        ],
        request_id=request_id(),
    )


@app.post("/api/providers/reconcile-billing", response_model=c.ReconcileBillingResponse, status_code=202)
def reconcile_billing(payload: c.ReconcileBillingRequest, request: Request) -> c.ReconcileBillingResponse:
    require_role(request, c.UserRole.admin)
    if ops_repository() is not None:
        return ops_repository().reconcile_billing(payload, request_id())
    return c.ReconcileBillingResponse(reconciliation_run_id=new_id("recon"), status="queued", request_id=request_id())


@app.get(
    "/api/cases/{case_id}/agent/source-bindings",
    response_model=c.PageResponse[c.CaseAgentSourceBinding],
)
def source_bindings(case_id: str, limit: int = 50) -> c.PageResponse[c.CaseAgentSourceBinding]:
    get_case(case_id)
    if case_learning_repository() is not None:
        values = case_learning_repository().list_source_bindings(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository().source_bindings.values() if item.case_id == case_id], limit)


@app.post(
    "/api/cases/{case_id}/agent/source-bindings",
    response_model=c.CaseAgentSourceBinding,
    status_code=201,
)
def create_source_binding(
    case_id: str, payload: c.CreateSourceBindingRequest, request: Request
) -> c.CaseAgentSourceBinding:
    require_role(request, c.UserRole.operator)
    get_case(case_id)
    if case_learning_repository() is not None:
        return case_learning_repository().create_source_binding(case_id=case_id, payload=payload)
    binding = c.CaseAgentSourceBinding(id=new_id("src"), case_id=case_id, **payload.model_dump())
    repository().source_bindings[binding.id] = binding
    return binding


@app.post("/api/cases/{case_id}/agent/import-source", response_model=c.CaseAgentRun, status_code=202)
def import_case_source(case_id: str, payload: c.ImportCaseSourceRequest, request: Request) -> c.CaseAgentRun:
    require_role(request, c.UserRole.operator)
    get_case(case_id)
    if case_learning_repository() is not None:
        run = case_learning_repository().import_case_source(case_id=case_id, payload=payload)
        if run is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Source binding is missing.")
        return run
    run = c.CaseAgentRun(
        id=new_id("agent_run"),
        case_id=case_id,
        goal="brief",
        status=c.RunStatus.succeeded,
        source_binding_ids=[payload.source_binding_id],
    )
    repository().case_agent_runs[run.id] = run
    brief = c.CreativeBrief(id=new_id("brief"), case_id=case_id, summary="Imported source summary.")
    repository().briefs[brief.id] = brief
    return run


@app.post("/api/cases/{case_id}/agent/runs", response_model=c.CaseAgentRun, status_code=202)
def start_case_agent_run(
    case_id: str, payload: c.StartCaseAgentRunRequest, request: Request
) -> c.CaseAgentRun:
    require_role(request, c.UserRole.operator)
    get_case(case_id)
    if case_learning_repository() is not None:
        return case_learning_repository().start_agent_run(case_id=case_id, payload=payload)
    run = c.CaseAgentRun(
        id=new_id("agent_run"),
        case_id=case_id,
        goal=payload.goal,
        status=c.RunStatus.succeeded,
        source_binding_ids=payload.source_binding_ids,
    )
    repository().case_agent_runs[run.id] = run
    if payload.goal == "script_draft":
        draft = c.ScriptDraft(
            id=new_id("draft"),
            case_id=case_id,
            title="Agent generated draft",
            script="开场提出痛点。展示解决方案。收束到行动建议。",
        )
        repository().drafts[draft.id] = draft
    if payload.goal == "memory_proposal":
        proposal = c.MemoryProposal(
            id=new_id("mem"),
            case_id=case_id,
            insight="Short hooks with concrete outcomes perform better for this case.",
            evidence=[],
            proposed_by_reflection_run_id=run.id,
        )
        repository().memory_proposals[proposal.id] = proposal
    return run


@app.get("/api/cases/{case_id}/agent/runs", response_model=c.PageResponse[c.CaseAgentRun])
def case_agent_runs(case_id: str, limit: int = 50) -> c.PageResponse[c.CaseAgentRun]:
    if case_learning_repository() is not None:
        values = case_learning_repository().list_agent_runs(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository().case_agent_runs.values() if item.case_id == case_id], limit)


@app.get("/api/cases/{case_id}/agent/runs/{run_id}", response_model=c.CaseAgentRunDetail)
def case_agent_run_detail(case_id: str, run_id: str) -> c.CaseAgentRunDetail:
    if case_learning_repository() is not None:
        detail = case_learning_repository().agent_run_detail(case_id=case_id, run_id=run_id)
        if detail is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Agent run is missing.")
        return detail
    run = repository().case_agent_runs[run_id]
    return c.CaseAgentRunDetail(
        run=run,
        briefs=[item for item in repository().briefs.values() if item.case_id == case_id],
        drafts=[item for item in repository().drafts.values() if item.case_id == case_id],
        memory_proposals=[item for item in repository().memory_proposals.values() if item.case_id == case_id],
    )


@app.get("/api/cases/{case_id}/agent/drafts", response_model=c.PageResponse[c.ScriptDraft])
def script_drafts(case_id: str, limit: int = 50) -> c.PageResponse[c.ScriptDraft]:
    if case_learning_repository() is not None:
        values = case_learning_repository().list_drafts(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository().drafts.values() if item.case_id == case_id], limit)


@app.post(
    "/api/cases/{case_id}/agent/drafts/{draft_id}/adopt",
    response_model=c.ScriptVersion,
    status_code=201,
)
def adopt_script_draft(
    case_id: str, draft_id: str, payload: c.AdoptScriptDraftRequest, request: Request
) -> c.ScriptVersion:
    require_role(request, c.UserRole.operator)
    if case_learning_repository() is not None:
        script = case_learning_repository().adopt_draft(
            case_id=case_id,
            draft_id=draft_id,
            payload=payload,
        )
        if script is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Script draft is missing.")
        return script
    draft = repository().drafts[draft_id]
    script = c.ScriptVersion(
        id=new_id("script"),
        case_id=case_id,
        title=payload.title or draft.title,
        script=payload.publish_content or draft.script,
        adopted_from_draft_id=draft.id,
    )
    repository().scripts[script.id] = script
    repository().drafts[draft.id] = draft.model_copy(update={"status": "adopted", "updated_at": c.utcnow()})
    return script


@app.get("/api/cases/{case_id}/agent/memory-proposals", response_model=c.PageResponse[c.MemoryProposal])
def memory_proposals(case_id: str, limit: int = 50) -> c.PageResponse[c.MemoryProposal]:
    if case_learning_repository() is not None:
        values = case_learning_repository().list_memory_proposals(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository().memory_proposals.values() if item.case_id == case_id], limit)


@app.get("/api/cases/{case_id}/knowledge", response_model=c.CaseKnowledgeResponse)
def case_knowledge(case_id: str) -> c.CaseKnowledgeResponse:
    get_case(case_id)
    if case_learning_repository() is not None:
        return case_learning_repository().knowledge(case_id=case_id)
    return c.CaseKnowledgeResponse(
        case_id=case_id,
        memories=[item for item in repository().memories.values() if item.case_id == case_id],
        recent_script_versions=[item for item in repository().scripts.values() if item.case_id == case_id][-10:],
        recent_video_versions=[item for item in repository().video_versions.values() if item.case_id == case_id][-10:],
    )


@app.get("/api/cases/{case_id}/memory", response_model=c.PageResponse[c.CaseMemory])
def case_memory(case_id: str, limit: int = 50) -> c.PageResponse[c.CaseMemory]:
    if case_learning_repository() is not None:
        values = case_learning_repository().list_memory(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository().memories.values() if item.case_id == case_id], limit)


@app.post("/api/cases/{case_id}/memory/{memory_id}/approve", response_model=c.CaseMemory)
def approve_memory(
    case_id: str, memory_id: str, payload: c.ApproveMemoryRequest, request: Request
) -> c.CaseMemory:
    require_role(request, c.UserRole.operator)
    if case_learning_repository() is not None:
        memory = case_learning_repository().approve_memory(case_id=case_id, memory_id=memory_id)
        if memory is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Memory proposal is missing.")
        return memory
    proposal = repository().memory_proposals.get(memory_id) or repository().memories[memory_id]
    next_status = proposal.status
    if next_status == "proposed":
        assert_transition("case_memory", next_status, "approved")
        next_status = "approved"
    assert_transition("case_memory", next_status, "active")
    memory = c.CaseMemory.model_validate(
        proposal.model_dump(exclude={"proposed_by_reflection_run_id"})
    ).model_copy(update={"status": "active"})
    repository().memories[memory.id] = memory
    return memory


@app.post("/api/cases/{case_id}/memory/{memory_id}/reject", response_model=c.MemoryProposal)
def reject_memory(
    case_id: str, memory_id: str, payload: c.RejectMemoryRequest, request: Request
) -> c.MemoryProposal:
    require_role(request, c.UserRole.operator)
    if case_learning_repository() is not None:
        proposal = case_learning_repository().reject_memory(case_id=case_id, memory_id=memory_id)
        if proposal is None:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Memory proposal is missing.")
        return proposal
    proposal = repository().memory_proposals[memory_id].model_copy(update={"status": "rejected"})
    repository().memory_proposals[memory_id] = proposal
    return proposal


@app.get("/api/cases/{case_id}/performance", response_model=c.CasePerformanceResponse)
def case_performance(case_id: str, window: str = "7d") -> c.CasePerformanceResponse:
    if production_repository() is not None:
        return production_repository().case_performance(case_id=case_id, window=window)
    observations = [item for item in repository().performance_observations.values() if item.case_id == case_id]
    metrics = c.PerformanceMetricView(
        impressions=int(sum(item.metric_value for item in observations if item.metric_name == "impressions")),
        views=int(sum(item.metric_value for item in observations if item.metric_name == "views")),
        likes=int(sum(item.metric_value for item in observations if item.metric_name == "likes")),
    )
    return c.CasePerformanceResponse(metrics=metrics, observations=observations)


@app.post("/api/cases/{case_id}/metrics/import", response_model=c.ImportBatchReport, status_code=202)
def import_metrics(case_id: str, payload: c.MetricsImportRequest, request: Request) -> c.ImportBatchReport:
    require_role(request, c.UserRole.operator)
    if production_repository() is not None:
        return production_repository().import_metrics(
            case_id=case_id,
            payload=payload,
            request_id=request_id(),
        )
    rows = []
    for index, row in enumerate(payload.rows):
        if isinstance(row, dict):
            obs = c.PerformanceObservation(
                id=new_id("perf"),
                case_id=case_id,
                publish_record_id=str(row.get("publish_record_id", "manual")),
                metric_name=str(row.get("metric_name", "views")),
                metric_value=float(row.get("metric_value", 0)),
            )
            if not payload.dry_run:
                repository().performance_observations[obs.id] = obs
            rows.append(c.ImportRowResult(row_index=index, status="created", internal_id=obs.id))
    report = c.ImportBatchReport(
        batch_id=new_id("imp"),
        import_type="performance",
        status=c.ImportBatchStatus.completed,
        created_count=len(rows),
        skipped_count=0,
        failed_count=0,
        results=rows,
        request_id=request_id(),
    )
    repository().import_reports[report.batch_id] = report
    return report


@app.post("/api/cases/{case_id}/reflection-runs", response_model=c.ReflectionRun, status_code=202)
def start_reflection(case_id: str, payload: c.StartReflectionRunRequest, request: Request) -> c.ReflectionRun:
    require_role(request, c.UserRole.operator)
    if case_learning_repository() is not None:
        get_case(case_id)
        return case_learning_repository().start_reflection(case_id=case_id, payload=payload)
    reflection = c.ReflectionRun(
        id=new_id("refl"),
        case_id=case_id,
        status=c.RunStatus.succeeded,
        window=payload.window,
    )
    repository().reflection_runs[reflection.id] = reflection
    proposal = c.MemoryProposal(
        id=new_id("mem"),
        case_id=case_id,
        insight="Reuse the best performing hook style from recent videos.",
        evidence=[reflection.id],
        confidence=0.65,
        proposed_by_reflection_run_id=reflection.id,
    )
    repository().memory_proposals[proposal.id] = proposal
    return reflection


@app.get("/api/cases/{case_id}/insights", response_model=c.PageResponse[c.CaseInsightCard])
def case_insights(case_id: str, limit: int = 50) -> c.PageResponse[c.CaseInsightCard]:
    if case_learning_repository() is not None:
        values = case_learning_repository().insights(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    cards = [
        c.CaseInsightCard(
            id=new_id("insight"),
            case_id=case_id,
            title="Memory proposals",
            body=f"{len([item for item in repository().memory_proposals.values() if item.case_id == case_id])} proposal(s) waiting for review.",
        )
    ]
    return page(cards, limit)


@app.get("/api/cases/{case_id}/creative-patterns", response_model=c.PageResponse[c.CreativePattern])
def creative_patterns(case_id: str, limit: int = 50) -> c.PageResponse[c.CreativePattern]:
    if case_learning_repository() is not None:
        values = case_learning_repository().creative_patterns(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    patterns = [item for item in repository().creative_patterns.values() if item.case_id == case_id]
    if not patterns:
        patterns = [
            c.CreativePattern(
                id=new_id("pattern"),
                case_id=case_id,
                label="Concrete hook + short CTA",
                lift=None,
                evidence_count=len(repository().finished_videos),
            )
        ]
    return page(patterns, limit)


@app.post("/api/cases/{case_id}/scripts/generate-with-memory", response_model=c.ScriptDraft, status_code=202)
def generate_script_with_memory(
    case_id: str, payload: c.GenerateScriptWithMemoryRequest, request: Request
) -> c.ScriptDraft:
    require_role(request, c.UserRole.operator)
    if case_learning_repository() is not None:
        get_case(case_id)
        return case_learning_repository().generate_script_with_memory(
            case_id=case_id,
            payload=payload,
        )
    memories = [repository().memories[mid].insight for mid in payload.memory_ids if mid in repository().memories]
    draft = c.ScriptDraft(
        id=new_id("draft"),
        case_id=case_id,
        title="Memory-guided draft",
        script=f"{payload.brief}\n\n参考记忆：{' / '.join(memories) if memories else '暂无'}",
        memory_ids=payload.memory_ids,
    )
    repository().drafts[draft.id] = draft
    return draft


@app.get("/api/videos/{video_version_id}/performance-attribution", response_model=c.PerformanceAttributionResponse)
def performance_attribution(video_version_id: str) -> c.PerformanceAttributionResponse:
    if production_repository() is not None:
        attribution = production_repository().performance_attribution(video_version_id)
        if attribution is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Video version is missing.")
        return attribution
    video = repository().video_versions[video_version_id]
    return c.PerformanceAttributionResponse(
        video_version_id=video_version_id,
        feature_vector=c.CreativeFeatureVector(broll_count=1),
        observations=[item for item in repository().performance_observations.values() if item.case_id == video.case_id],
        contributing_memories=[item for item in repository().memories.values() if item.case_id == video.case_id],
    )


@app.get("/api/cases/{case_id}/finished-videos", response_model=c.PageResponse[c.FinishedVideo])
def case_finished_videos(case_id: str, limit: int = 50) -> c.PageResponse[c.FinishedVideo]:
    if production_repository() is not None:
        values = production_repository().list_finished_videos(case_id=case_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page([item for item in repository().finished_videos.values() if item.case_id == case_id], limit)


@app.get("/api/finished-videos/{id}", response_model=c.FinishedVideoDetail)
def finished_video_detail(id: str) -> c.FinishedVideoDetail:
    if production_repository() is not None:
        detail = production_repository().finished_video_detail(id)
        if detail is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
        return detail
    finished = repository().finished_videos[id]
    version = next(
        (item for item in repository().video_versions.values() if item.finished_video_id == id),
        None,
    )
    records = [item for item in repository().publish_records.values() if item.video_version_id == (version.id if version else None)]
    return c.FinishedVideoDetail(finished_video=finished, video_version=version, publish_records=records)


@app.get("/api/finished-videos/{id}/preview-url", response_model=c.SignedUrlResponse)
def finished_video_preview(id: str) -> c.SignedUrlResponse:
    if production_repository() is not None:
        uri = production_repository().artifact_uri_for_finished_video(id)
        if uri is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Finished video is missing.")
        if uri:
            return object_store().signed_url(uri).model_copy(update={"request_id": request_id()})
        return signed(f"finished-videos/{id}/preview.mp4")
    finished = repository().finished_videos[id]
    artifact = repository().artifacts.get(finished.video_artifact.artifact_id)
    if artifact and artifact.uri:
        return object_store().signed_url(artifact.uri).model_copy(update={"request_id": request_id()})
    return signed(f"finished-videos/{id}/preview.mp4")


@app.get("/api/finished-videos/{id}/download", response_model=c.SignedUrlResponse)
def finished_video_download(id: str) -> c.SignedUrlResponse:
    return finished_video_preview(id)


@app.delete("/api/finished-videos/{id}", response_model=c.OkResponse)
def delete_finished_video(id: str, request: Request) -> c.OkResponse:
    require_role(request, c.UserRole.admin)
    if production_repository() is not None:
        production_repository().delete_finished_video(id)
        return c.OkResponse(request_id=request_id())
    repository().finished_videos.pop(id, None)
    return c.OkResponse(request_id=request_id())


@app.post(
    "/api/finished-videos/{id}/editor-handoff",
    response_model=c.EditorHandoffPackageArtifact,
    status_code=201,
)
def editor_handoff(
    id: str, payload: c.CreateEditorHandoffRequest, request: Request
) -> c.EditorHandoffPackageArtifact:
    require_role(request, c.UserRole.operator)
    if production_repository() is not None:
        return production_repository().create_editor_handoff(id, payload)
    artifact = repository().create_artifact(
        kind=c.ArtifactKind.editor_handoff,
        payload_schema="EditorHandoffPackageArtifact.v1",
        payload={"finished_video_id": id, "format": payload.format},
        uri=f"sandbox://handoff/{id}.zip",
    )
    return c.EditorHandoffPackageArtifact(
        package_artifact=repository().artifact_ref(artifact.id),
        manifest={"finished_video_id": id, "format": payload.format},
    )


@app.post(
    "/api/finished-videos/{id}/jianying-draft",
    response_model=c.JianyingDraftPackageArtifact,
    status_code=201,
)
def jianying_draft(
    id: str, payload: c.CreateJianyingDraftRequest, request: Request
) -> c.JianyingDraftPackageArtifact:
    require_role(request, c.UserRole.operator)
    if production_repository() is not None:
        return production_repository().create_jianying_draft(id, payload)
    artifact = repository().create_artifact(
        kind=c.ArtifactKind.jianying_draft,
        payload_schema="JianyingDraftPackageArtifact.v1",
        payload={"finished_video_id": id, "template_id": payload.template_id},
        uri=f"sandbox://jianying/{id}.zip",
    )
    return c.JianyingDraftPackageArtifact(
        package_artifact=repository().artifact_ref(artifact.id),
        draft_manifest={"finished_video_id": id, "template_id": payload.template_id or "default"},
    )


@app.get("/api/publish/packages", response_model=c.PageResponse[c.PublishPackage])
def publish_packages(limit: int = 50) -> c.PageResponse[c.PublishPackage]:
    if publishing_repository() is not None:
        values = publishing_repository().list_packages(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository().publish_packages.values(), limit)


@app.post("/api/publish/packages", response_model=c.PublishPackage, status_code=201)
def create_publish_package(payload: c.CreatePublishPackageRequest, request: Request) -> c.PublishPackage:
    require_role(request, c.UserRole.operator)
    if publishing_repository() is not None:
        return publishing_repository().create_package(payload)
    if payload.source_finished_video_id:
        return repository().create_publish_package_from_finished_video(
            repository().finished_videos[payload.source_finished_video_id],
            title=payload.title,
            description=payload.description,
        )
    if not payload.upload_artifact_id:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Upload artifact is required.")
    package = c.PublishPackage(
        id=new_id("pkg"),
        upload_artifact_id=payload.upload_artifact_id,
        video_artifact=ensure_artifact_ref(payload.upload_artifact_id),
        platform_defaults=c.PublishDefaults(title=payload.title, description=payload.description),
    )
    repository().publish_packages[package.id] = package
    return package


@app.get("/api/publish/batches", response_model=c.PageResponse[c.PublishBatchVm])
def publish_batches(limit: int = 50) -> c.PageResponse[c.PublishBatchVm]:
    if publishing_repository() is not None:
        values = publishing_repository().list_batches(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository().publish_batches.values(), limit)


@app.post("/api/publish/batches", response_model=c.PublishBatchVm, status_code=201)
def create_publish_batch(payload: c.CreatePublishBatchRequest, request: Request) -> c.PublishBatchVm:
    require_role(request, c.UserRole.operator)
    if publishing_repository() is not None:
        return publishing_repository().create_batch(payload)
    return repository().create_publish_batch(payload.publish_package_ids, payload.platform_targets)


@app.get("/api/publish/batches/{batch_id}", response_model=c.PublishBatchVm)
def publish_batch_detail(batch_id: str) -> c.PublishBatchVm | JSONResponse:
    if publishing_repository() is not None:
        batch = publishing_repository().get_batch(batch_id)
        if batch is None:
            return not_found_response("Publish batch not found")
        return batch
    batch = repository().publish_batches.get(batch_id)
    if batch is None:
        return not_found_response("Publish batch not found")
    return batch


@app.post("/api/publish/batches/{batch_id}/submit", response_model=c.PublishBatchVm, status_code=202)
def submit_publish_batch(
    batch_id: str, payload: c.SubmitPublishBatchRequest, request: Request
) -> c.PublishBatchVm | JSONResponse:
    require_role(request, c.UserRole.operator)
    if publishing_repository() is not None:
        batch = publishing_repository().submit_batch(batch_id, payload)
        if batch is None:
            return not_found_response("Publish batch not found")
        return batch
    batch = repository().publish_batches.get(batch_id)
    if batch is None:
        return not_found_response("Publish batch not found")
    new_items = []
    selected_count = 0
    for item in batch.items:
        if not item.selected:
            new_items.append(item)
            continue
        selected_count += 1
        current_item_status = item.status
        for next_status in ["normalizing", "asr_running", "copy_running", "cover_running", "review_ready"]:
            assert_transition("publish_item", current_item_status, next_status)
            current_item_status = next_status
        if not payload.dry_run:
            assert_transition("publish_item", current_item_status, "publishing")
            current_item_status = "publishing"
            assert_transition("publish_item", current_item_status, "published")
            current_item_status = "published"
        new_items.append(
            item.model_copy(
                update={"status": c.PublishItemStatus(current_item_status), "updated_at": c.utcnow()}
            )
        )
        attempt_status = "manual_review_ready" if payload.dry_run else "published"
        assert_transition("publish_attempt", "created", attempt_status)
        attempt = c.PublishAttempt(
            id=new_id("pub_attempt"),
            batch_id=batch.id,
            item_id=item.id,
            platforms=[item.platform],
            manual_review=payload.dry_run,
            status=c.PublishAttemptStatus(attempt_status),
            adapter_id="sandbox.publish",
            results=[],
            finished_at=c.utcnow() if attempt_status == "published" else None,
        )
        repository().publish_attempts[attempt.id] = attempt
    if selected_count == 0:
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "At least one publish item must be selected.")
    assert_transition("publish_batch", batch.status, "processing")
    next_batch_status = "review_ready" if payload.dry_run else "publishing"
    assert_transition("publish_batch", "processing", next_batch_status)
    if not payload.dry_run:
        assert_transition("publish_batch", next_batch_status, "completed")
        next_batch_status = "completed"
    batch = batch.model_copy(
        update={"status": c.PublishBatchStatus(next_batch_status), "items": new_items, "updated_at": c.utcnow()}
    )
    repository().publish_batches[batch.id] = batch
    return batch


@app.patch("/api/publish/items/{item_id}", response_model=c.PublishBatchItemVm)
def patch_publish_item(
    item_id: str, payload: c.PatchPublishItemRequest, request: Request
) -> c.PublishBatchItemVm | JSONResponse:
    require_role(request, c.UserRole.operator)
    if publishing_repository() is not None:
        item = publishing_repository().patch_item(item_id, payload)
        if item is None:
            return not_found_response("Publish item not found")
        return item
    for batch in repository().publish_batches.values():
        for index, item in enumerate(batch.items):
            if item.id == item_id:
                updated = item.model_copy(update={**payload.model_dump(exclude_none=True), "updated_at": c.utcnow()})
                items = list(batch.items)
                items[index] = updated
                repository().publish_batches[batch.id] = batch.model_copy(update={"items": items})
                return updated
    return not_found_response("Publish item not found")


@app.get("/api/publish/attempts/{attempt_id}", response_model=c.PublishAttemptDetail)
def publish_attempt(attempt_id: str) -> c.PublishAttemptDetail | JSONResponse:
    if publishing_repository() is not None:
        detail = publishing_repository().attempt_detail(attempt_id)
        if detail is None:
            return not_found_response("Publish attempt not found")
        return detail
    attempt = repository().publish_attempts.get(attempt_id)
    if attempt is None:
        return not_found_response("Publish attempt not found")
    return c.PublishAttemptDetail(attempt=attempt, record=None)


@app.get("/api/ops/dashboard", response_model=c.OpsDashboardVm)
def ops_dashboard(
    window_start: datetime | None = None, window_end: datetime | None = None
) -> c.OpsDashboardVm:
    if ops_repository() is not None:
        return ops_repository().dashboard(window_start=window_start, window_end=window_end)
    usage = provider_usage(window_start, window_end)
    funnel = yield_funnel(window_start, window_end)
    return c.OpsDashboardVm(
        usage=usage,
        yield_funnel=funnel,
        alerts=list(repository().alerts.values()),
        cost_rollups=list(repository().cost_rollups.values()),
    )


@app.get("/api/ops/cost-rollups", response_model=c.PageResponse[c.CostRollup])
def cost_rollups(
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    group_by: str | None = None,
    limit: int = 50,
) -> c.PageResponse[c.CostRollup]:
    if ops_repository() is not None:
        values = ops_repository().list_cost_rollups(
            window_start=window_start,
            window_end=window_end,
            group_by=group_by,
            limit=limit,
        )
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    rollup = c.CostRollup(
        id="cost_current",
        group_key=group_by or "all",
        group_by=group_by,
        estimated_cost=c.Money(
            amount=sum(
                (item.estimated_cost.amount for item in repository().provider_invocations.values() if item.estimated_cost),
                c.Decimal("0"),
            ),
            currency="CNY",
        ),
        invocations=len(repository().provider_invocations),
    )
    repository().cost_rollups[rollup.id] = rollup
    return page(repository().cost_rollups.values(), limit)


@app.get("/api/ops/yield-funnel", response_model=c.YieldFunnelResponse)
def yield_funnel(
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    case_id: str | None = None,
) -> c.YieldFunnelResponse:
    if ops_repository() is not None:
        return ops_repository().yield_funnel(
            window_start=window_start,
            window_end=window_end,
            case_id=case_id,
        )
    events = [
        c.YieldFunnelEvent(
            id=f"yield_{run.id}",
            case_id=run.case_id,
            run_id=run.id,
            event_name=f"workflow_{run.status.value}",
            affects_true_yield=True,
        )
        for run in repository().runs.values()
        if case_id is None or run.case_id == case_id
    ]
    success = len([event for event in events if event.event_name == "workflow_succeeded"])
    rate = success / len(events) if events else None
    return c.YieldFunnelResponse(events=events, true_yield_rate=rate)


@app.get("/api/ops/budgets", response_model=c.PageResponse[c.Budget])
def budgets(limit: int = 50) -> c.PageResponse[c.Budget]:
    if ops_repository() is not None:
        values = ops_repository().list_budgets(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository().budgets.values(), limit)


@app.post("/api/ops/budgets", response_model=c.Budget, status_code=201)
def upsert_budget(payload: c.UpsertBudgetRequest, request: Request) -> c.Budget:
    require_role(request, c.UserRole.admin)
    if ops_repository() is not None:
        return ops_repository().upsert_budget(payload)
    repository().budgets[payload.budget.id] = payload.budget
    return payload.budget


@app.patch("/api/ops/budgets/{budget_id}", response_model=c.Budget)
def patch_budget(budget_id: str, payload: c.PatchBudgetRequest, request: Request) -> c.Budget:
    require_role(request, c.UserRole.admin)
    if ops_repository() is not None:
        return ops_repository().patch_budget(budget_id, payload)
    return repository().patch(repository().budgets, budget_id, payload.model_dump(exclude_none=True))


@app.post("/api/ops/alerts/{event_id}/ack", response_model=c.OpsAlertEvent)
def ack_alert(event_id: str, payload: c.AcknowledgeAlertRequest, request: Request) -> c.OpsAlertEvent:
    require_role(request, c.UserRole.operator)
    if ops_repository() is not None:
        return ops_repository().patch_alert_status(event_id, "acknowledged")
    return repository().patch(repository().alerts, event_id, {"status": "acknowledged"})


@app.post("/api/ops/alerts/{event_id}/resolve", response_model=c.OpsAlertEvent)
def resolve_alert(event_id: str, payload: c.ResolveAlertRequest, request: Request) -> c.OpsAlertEvent:
    require_role(request, c.UserRole.operator)
    if ops_repository() is not None:
        return ops_repository().patch_alert_status(event_id, "resolved")
    return repository().patch(repository().alerts, event_id, {"status": "resolved"})


@app.post("/api/runs/{run_id}/quality-checks", response_model=c.ProductionQualityCheck, status_code=201)
def run_quality_check(
    run_id: str, payload: c.CreateQualityCheckRequest, request: Request
) -> c.ProductionQualityCheck:
    require_role(request, c.UserRole.operator)
    if ops_repository() is not None:
        return ops_repository().create_quality_check(
            target_type="run",
            target_id=run_id,
            payload=payload,
        )
    check = c.ProductionQualityCheck(id=new_id("qc"), target_type="run", target_id=run_id, **payload.model_dump())
    repository().quality_checks[check.id] = check
    return check


@app.post(
    "/api/finished-videos/{id}/quality-checks",
    response_model=c.ProductionQualityCheck,
    status_code=201,
)
def finished_video_quality_check(
    id: str, payload: c.CreateQualityCheckRequest, request: Request
) -> c.ProductionQualityCheck:
    require_role(request, c.UserRole.operator)
    if ops_repository() is not None:
        return ops_repository().create_quality_check(
            target_type="finished_video",
            target_id=id,
            payload=payload,
        )
    check = c.ProductionQualityCheck(
        id=new_id("qc"),
        target_type="finished_video",
        target_id=id,
        **payload.model_dump(),
    )
    repository().quality_checks[check.id] = check
    return check


@app.post("/api/approval-requests/{id}/approve", response_model=c.ApprovalRequest)
def approve_request(id: str, payload: c.ApprovalDecisionRequest, request: Request) -> c.ApprovalRequest:
    require_role(request, c.UserRole.operator)
    if ops_repository() is not None:
        return ops_repository().decide_approval(id, "approved", payload)
    approval = c.ApprovalRequest(
        id=id,
        resource_type="unknown",
        resource_id=None,
        status="approved",
        reason=payload.reason,
    )
    repository().approvals[id] = approval
    return approval


@app.post("/api/approval-requests/{id}/reject", response_model=c.ApprovalRequest)
def reject_request(id: str, payload: c.ApprovalDecisionRequest, request: Request) -> c.ApprovalRequest:
    require_role(request, c.UserRole.operator)
    if ops_repository() is not None:
        return ops_repository().decide_approval(id, "rejected", payload)
    approval = c.ApprovalRequest(
        id=id,
        resource_type="unknown",
        resource_id=None,
        status="rejected",
        reason=payload.reason,
    )
    repository().approvals[id] = approval
    return approval


@app.get("/api/audit/events", response_model=c.PageResponse[c.AuditEvent])
def audit_events(request: Request, limit: int = 50) -> c.PageResponse[c.AuditEvent]:
    require_role(request, c.UserRole.admin)
    if ops_repository() is not None:
        values = ops_repository().list_audit_events(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository().audit_events.values(), limit)


@app.post("/api/import/batches", response_model=c.ImportBatchReport, status_code=202)
def import_batch(payload: c.CreateImportBatchRequest, request: Request) -> c.ImportBatchReport:
    require_role(request, c.UserRole.operator)
    if production_repository() is not None:
        report = production_repository().create_import_batch(payload, request_id())
        if report is not None:
            return report
    rows = payload.rows or []
    results = []
    created = 0
    failed = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            failed += 1
            results.append(
                c.ImportRowResult(
                    row_index=index,
                    status="failed",
                    error=c.NodeError(
                        code=c.ErrorCode.validation_invalid_options,
                        message="Import row must be an object.",
                    ),
                )
            )
            continue
        internal_id = new_id(payload.import_type)
        if not payload.dry_run:
            if payload.import_type == "case":
                case = c.CaseDetail(
                    id=internal_id,
                    name=str(row.get("name", "Imported case")),
                    owner_user_id="usr_admin",
                    description=str(row.get("description", "")),
                )
                repository().cases[case.id] = case
            elif payload.import_type == "script":
                script = c.ScriptVersion(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    title=str(row.get("title", "Imported script")),
                    script=str(row.get("script", "")),
                )
                repository().scripts[script.id] = script
            elif payload.import_type == "media":
                asset = c.MediaAssetRecord(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    title=str(row.get("title", "Imported media")),
                    kind=str(row.get("kind", "other")),
                    annotation_status="pending",
                )
                repository().media_assets[asset.id] = asset
            elif payload.import_type == "finished_video":
                artifact = repository().create_artifact(
                    kind=c.ArtifactKind.video_finished,
                    payload_schema="ImportedFinishedVideoArtifact.v1",
                    payload={"external_id": row.get("external_id")},
                    uri=str(row.get("uri", f"sandbox://import/{internal_id}.mp4")),
                )
                finished = c.FinishedVideo(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    title=str(row.get("title", "Imported finished video")),
                    video_artifact=repository().artifact_ref(artifact.id),
                    duration_sec=float(row.get("duration_sec", 0)),
                    qc_status=str(row.get("qc_status", "pending")),
                )
                repository().finished_videos[finished.id] = finished
            elif payload.import_type == "video_version":
                version = c.VideoVersion(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    script_version_id=str(row.get("script_version_id")) if row.get("script_version_id") else None,
                    finished_video_id=str(row.get("finished_video_id")) if row.get("finished_video_id") else None,
                    timeline_plan_artifact_id=str(row.get("timeline_plan_artifact_id", "imported")),
                    style_plan_artifact_id=str(row.get("style_plan_artifact_id", "imported")),
                )
                repository().video_versions[version.id] = version
            elif payload.import_type == "publish_record":
                record = c.PublishRecord(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    video_version_id=str(row.get("video_version_id")) if row.get("video_version_id") else None,
                    platform=str(row.get("platform", "manual")),
                    status=str(row.get("status", "published")),
                    published_at=c.utcnow(),
                )
                repository().publish_records[record.id] = record
            elif payload.import_type == "performance":
                obs = c.PerformanceObservation(
                    id=internal_id,
                    case_id=str(row.get("case_id", "case_demo")),
                    publish_record_id=str(row.get("publish_record_id", "manual")),
                    metric_name=str(row.get("metric_name", "views")),
                    metric_value=float(row.get("metric_value", 0)),
                )
                repository().performance_observations[obs.id] = obs
            elif payload.import_type == "prompt_seed":
                template = c.PromptTemplate(
                    id=internal_id,
                    name=str(row.get("name", "Imported prompt")),
                    purpose=str(row.get("purpose", "imported")),
                    variables_schema_ref=c.PromptSchemaRef(schema_id=str(row.get("variables_schema_id", "imported.variables"))),
                    output_schema_ref=c.PromptSchemaRef(schema_id=str(row.get("output_schema_id", "imported.output"))),
                    status="active",
                )
                version = c.PromptVersion(
                    id=new_id("pver"),
                    prompt_template_id=template.id,
                    content=str(row.get("content", "")),
                    status="published",
                    approved_at=c.utcnow(),
                    published_at=c.utcnow(),
                )
                repository().prompt_templates[template.id] = template
                repository().prompt_versions[version.id] = version
            elif payload.import_type == "provider_price":
                catalog = c.ProviderPriceCatalog(
                    id=internal_id,
                    provider_id=str(row.get("provider_id", "sandbox")),
                    status="published",
                    currency=str(row.get("currency", "CNY")),
                )
                repository().price_catalogs[catalog.id] = catalog
        created += 1
        results.append(
            c.ImportRowResult(
                row_index=index,
                status="created",
                external_id=str(row.get("external_id")) if row.get("external_id") else None,
                internal_id=internal_id,
            )
        )
    report = c.ImportBatchReport(
        batch_id=new_id("imp"),
        import_type=payload.import_type,
        status=c.ImportBatchStatus.completed if failed == 0 else c.ImportBatchStatus.partially_failed,
        created_count=created,
        skipped_count=0,
        failed_count=failed,
        results=results,
        request_id=request_id(),
    )
    repository().import_reports[report.batch_id] = report
    return report


@app.get("/api/import/batches/{batch_id}", response_model=c.ImportBatchReport)
def import_batch_detail(batch_id: str) -> c.ImportBatchReport:
    if production_repository() is not None:
        report = production_repository().get_import_batch(batch_id)
        if report is not None:
            return report
    return repository().import_reports[batch_id]
