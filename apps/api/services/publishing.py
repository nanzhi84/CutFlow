from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from apps.api.common import (
    accounts_repository,
    object_store,
    publishing_repository,
    repository,
    request_id,
)
from apps.api.dependencies import not_found_response
from apps.api.services import publishing_nodes as nodes
from packages.core import contracts as c
from packages.core.storage.database import (
    CaseRow,
    FinishedVideoRow,
    PublishBatchItemRow,
    PublishPackageRow,
    ScriptVersionRow,
    WorkflowRunRow,
)
from packages.core.storage.object_store import parse_object_uri
from packages.core.storage.repository import Repository, new_id
from packages.creative.cases.sqlalchemy_learning_mappers import script_version_row_to_contract
from packages.production.sqlalchemy_mappers import (
    case_row_to_contract,
    finished_video_row_to_contract,
    workflow_run_row_to_contract,
)
from packages.core.workflow import NodeExecutionError
from packages.publishing import normalize_publish_tags, normalize_scheduled_at, select_adapter
from packages.publishing.platform_adapter import PublishOutcome, PublishPayload
from packages.publishing.publish_executor import run_item_publish

logger = logging.getLogger(__name__)

# Root for locally-materialized publish media (video/cover downloaded for 小V猫 CDP).
PUBLISH_SPOOL_DIR = Path(".data/publish-spool")
# Scheduled publishes keep the local videoPath so 小V猫 can read it at the scheduled
# moment (the desktop app opens the file then, not at submit time). We therefore must
# NOT delete spool files right after a publish. 7 days comfortably covers the
# scheduling window while bounding otherwise-unbounded growth of the spool.
PUBLISH_SPOOL_RETENTION_SECONDS = 7 * 24 * 60 * 60


def _sweep_publish_spool(root: Path = PUBLISH_SPOOL_DIR) -> None:
    """Best-effort age-based cleanup of the publish spool.

    Deletes entry directories whose mtime is older than
    ``PUBLISH_SPOOL_RETENTION_SECONDS``. Runs once per submit and never raises — a
    failed sweep is logged but must not block a publish."""
    try:
        entries = list(root.iterdir()) if root.exists() else []
    except OSError as exc:
        logger.warning("publish spool sweep could not list %s: %s", root, exc)
        return
    cutoff = time.time() - PUBLISH_SPOOL_RETENTION_SECONDS
    for entry in entries:
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("publish spool sweep could not remove %s: %s", entry, exc)


def _build_publish_runner(
    payload: c.SubmitPublishBatchRequest,
    request: Request,
) -> tuple[Callable[[object, object], PublishOutcome], str]:
    adapter = select_adapter(payload.adapter_id)
    runtime_repo = repository(request)
    target_repo = accounts_repository(request)
    objects = object_store(request)
    scheduled_at = normalize_scheduled_at(payload.mode, payload.scheduled_at)
    # Best-effort prune of stale spool entries on every submit/retry (see retention note).
    _sweep_publish_spool()

    def runner(item: object, package: object) -> PublishOutcome:
        spool_dir: Path | None = None
        materialized_paths: dict[str, str] = {}

        def ensure_spool_dir() -> Path:
            nonlocal spool_dir
            if spool_dir is None:
                spool_dir = (PUBLISH_SPOOL_DIR / new_id("pub_files")).resolve()
                spool_dir.mkdir(parents=True, exist_ok=True)
            return spool_dir

        def materialize_file_uri(uri: str | None, stem: str) -> str | None:
            if not uri:
                return None
            if uri in materialized_paths:
                return materialized_paths[uri]
            if uri.startswith("file://"):
                path = uri.removeprefix("file://")
                materialized_paths[uri] = path
                return path
            if uri.startswith(("/", "http://", "https://")):
                materialized_paths[uri] = uri
                return uri
            ref = parse_object_uri(uri)
            suffix = Path(ref.key).suffix
            downloaded_path = objects.download_file(ref, ensure_spool_dir() / f"{stem}{suffix}")
            materialized_paths[uri] = str(downloaded_path)
            return str(downloaded_path)

        def resolve_video() -> str | None:
            return materialize_file_uri(_artifact_uri(_get_field(package, "video_artifact")), "video")

        case_id = _get_field(package, "case_id")
        case = runtime_repo.cases.get(case_id) if case_id else None
        base_payload = _build_runner_payload(
            item,
            package,
            case_name=getattr(case, "name", None),
            scheduled_at=scheduled_at,
            simulate_failure=payload.simulate_publish_failure,
        )
        cover_path = materialize_file_uri(base_payload.cover_uri, "cover")
        if cover_path:
            base_payload = replace(base_payload, cover_uri=cover_path)
        outcome, _per_account_results = run_item_publish(
            adapter,
            base_payload,
            targets=_active_publish_targets(target_repo, case_id, _get_field(item, "platform")),
            resolve_video=resolve_video,
        )
        return outcome

    return runner, adapter.adapter_id


def _active_publish_targets(
    accounts_repo, case_id: str | None, platform: str | None
) -> list[tuple[str, str | None, str | None]]:
    if not case_id or not platform:
        return []
    targets: list[tuple[str, str | None, str | None]] = []
    for target in accounts_repo.list_targets(case_id):
        if not target.enabled:
            continue
        account = accounts_repo.get_account(target.account_id)
        if account is None:
            continue
        if account.platform != platform or account.status != "active":
            continue
        # Thread the exact 小V猫 account uid so multi-account-per-platform routes to
        # the right account (not just the first platform match).
        targets.append((account.id, account.account_name, getattr(account, "xiaovmao_uid", None)))
    return targets


def _build_runner_payload(
    item: object,
    package: object,
    *,
    case_name: str | None,
    scheduled_at,
    simulate_failure: bool,
) -> PublishPayload:
    return PublishPayload(
        title=_get_field(item, "title", ""),
        description=_get_field(item, "publish_content", "") or _get_field(item, "description", ""),
        platforms=(_get_field(item, "platform"),),
        tags=tuple(normalize_publish_tags(_get_field(item, "tags", []) or [])),
        location=_get_field(item, "location"),
        account_group=_get_field(item, "account_group"),
        case_name=case_name,
        scheduled_at=scheduled_at,
        video_uri=_artifact_uri(_get_field(package, "video_artifact")),
        cover_uri=_artifact_uri(_get_field(package, "cover_artifact")),
        manual_review=False,
        simulate_failure=simulate_failure,
    )


def _artifact_uri(artifact: object) -> str | None:
    if artifact is None:
        return None
    if isinstance(artifact, dict):
        uri = artifact.get("uri")
        return uri if isinstance(uri, str) else None
    return getattr(artifact, "uri", None)


def _get_field(value: object, name: str, default=None):
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def publish_packages(request: Request, limit: int = 50) -> c.PageResponse[c.PublishPackage]:
    values = publishing_repository(request).list_packages(limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def create_publish_package(payload: c.CreatePublishPackageRequest, request: Request) -> c.PublishPackage:
    return publishing_repository(request).create_package(payload)


def patch_publish_package(
    package_id: str, payload: c.PatchPublishPackageRequest, request: Request
) -> c.PublishPackage | JSONResponse:
    package = publishing_repository(request).patch_package(package_id, payload)
    if package is None:
        return not_found_response("Publish package not found")
    return package


def publish_batches(
    request: Request, limit: int = 50, case_id: str | None = None
) -> c.PageResponse[c.PublishBatchVm]:
    values = publishing_repository(request).list_batches(limit=limit, case_id=case_id)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def create_publish_batch(payload: c.CreatePublishBatchRequest, request: Request) -> c.PublishBatchVm:
    return publishing_repository(request).create_batch(payload)


def publish_batch_detail(request: Request, batch_id: str) -> c.PublishBatchVm | JSONResponse:
    batch = publishing_repository(request).get_batch(batch_id)
    if batch is None:
        return not_found_response("Publish batch not found")
    return batch


def publish_batch_attempts(
    request: Request, batch_id: str, limit: int = 50
) -> c.PageResponse[c.PublishAttempt] | JSONResponse:
    if publishing_repository(request).get_batch(batch_id) is None:
        return not_found_response("Publish batch not found")
    values = publishing_repository(request).list_attempts(batch_id, limit=limit)
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def delete_publish_batch(batch_id: str, request: Request) -> c.OkResponse | JSONResponse:
    deleted = publishing_repository(request).delete_batch(batch_id)
    if not deleted:
        return not_found_response("Publish batch not found")
    return c.OkResponse(request_id=request_id())


def submit_publish_batch(
    batch_id: str,
    payload: c.SubmitPublishBatchRequest,
    request: Request,
    publish_runner: Callable[[object, object], PublishOutcome] | None = None,
) -> c.PublishBatchVm | JSONResponse:
    default_publish_runner, adapter_id = _build_publish_runner(payload, request)
    active_publish_runner = publish_runner or default_publish_runner
    batch = publishing_repository(request).submit_batch(
        batch_id,
        payload,
        publish_runner=active_publish_runner,
    )
    if batch is None:
        return not_found_response("Publish batch not found")
    return batch


def retry_publish_item(batch_id: str, item_id: str, request: Request) -> c.PublishBatchItemVm | JSONResponse:
    # Re-run the failed item through the resolved publish adapter (小V猫 CDP in
    # production; sandbox.publish under the test env). The runner is the same one
    # submit_batch uses, so retry honors targets/video-resolution and only reaches
    # published when the adapter actually succeeds.
    runner, _adapter_id = _build_publish_runner(c.SubmitPublishBatchRequest(), request)
    item = publishing_repository(request).retry_item(batch_id, item_id, publish_runner=runner)
    if item is None:
        return not_found_response("Publish item not found")
    return item


def patch_publish_item(
    item_id: str, payload: c.PatchPublishItemRequest, request: Request
) -> c.PublishBatchItemVm | JSONResponse:
    item = publishing_repository(request).patch_item(item_id, payload)
    if item is None:
        return not_found_response("Publish item not found")
    return item


def _generate_publish_copy_sql(
    batch_id: str,
    item_id: str,
    payload: c.GeneratePublishCopyRequest,
    request: Request,
) -> c.PublishCopyResult | JSONResponse:
    """SQL-backed generate-copy: hydrate the copy context (case + finished video +
    run + adopted script) from Postgres into a run-state Repository, run the
    Publishing Copy Node, and persist the derived copy back onto the batch item."""
    session_factory = getattr(request.app.state, "sqlalchemy_session_factory", None)
    if session_factory is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Publishing repository is unavailable.")
    runtime = Repository()
    with session_factory() as session:
        item_row = session.scalar(
            select(PublishBatchItemRow).where(
                PublishBatchItemRow.batch_id == batch_id,
                PublishBatchItemRow.id == item_id,
            )
        )
        if item_row is None:
            return not_found_response("Publish item not found")
        package_row = session.get(PublishPackageRow, item_row.publish_package_id)
        if package_row is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Publish package is missing.")
        item = SimpleNamespace(title=item_row.title, description=item_row.description)
        package = SimpleNamespace(
            id=package_row.id,
            case_id=package_row.case_id,
            source_finished_video_id=package_row.source_finished_video_id,
        )
        # Hydrate exactly what resolve_copy_context reads: case (name/description),
        # the source finished video -> its run -> the case's adopted ScriptVersion.
        if package_row.case_id:
            case_row = session.get(CaseRow, package_row.case_id)
            if case_row is not None:
                runtime.cases[case_row.id] = case_row_to_contract(case_row)
            for script_row in session.scalars(
                select(ScriptVersionRow).where(ScriptVersionRow.case_id == package_row.case_id)
            ):
                runtime.scripts[script_row.id] = script_version_row_to_contract(script_row)
        if package_row.source_finished_video_id:
            fv_row = session.get(FinishedVideoRow, package_row.source_finished_video_id)
            if fv_row is not None:
                runtime.finished_videos[fv_row.id] = finished_video_row_to_contract(fv_row)
                if fv_row.run_id:
                    run_row = session.get(WorkflowRunRow, fv_row.run_id)
                    if run_row is not None:
                        runtime.runs[run_row.id] = workflow_run_row_to_contract(run_row)
    copy, source, invocation_id = nodes.run_copy_node(
        runtime,
        package,
        item,
        title_limit=payload.title_limit,
        gateway=request.app.state.provider_gateway,
        prompt_registry=request.app.state.prompt_registry,
    )
    with session_factory() as session:
        item_row = session.scalar(
            select(PublishBatchItemRow).where(
                PublishBatchItemRow.batch_id == batch_id,
                PublishBatchItemRow.id == item_id,
            )
        )
        if item_row is None:
            return not_found_response("Publish item not found")
        item_row.publish_content = copy.publish_content
        item_row.cover_title = copy.cover_title
        item_row.cover_subtitle = copy.cover_subtitle
        final_title = item_row.title
        if payload.overwrite or not item_row.title:
            item_row.title = copy.title
            final_title = copy.title
        item_row.updated_at = c.utcnow()
        session.commit()
    return c.PublishCopyResult(
        title=final_title,
        publish_content=copy.publish_content,
        cover_title=copy.cover_title,
        cover_subtitle=copy.cover_subtitle,
        source=source,
        prompt_invocation_id=invocation_id,
    )


def generate_publish_copy(
    batch_id: str, item_id: str, payload: c.GeneratePublishCopyRequest, request: Request
) -> c.PublishCopyResult | JSONResponse:
    """Publishing Copy Node endpoint (§28.3 generate-copy)."""
    return _generate_publish_copy_sql(batch_id, item_id, payload, request)


def _sql_publish_cover_inputs(batch_id: str, item_id: str, request: Request):
    session_factory = getattr(request.app.state, "sqlalchemy_session_factory", None)
    if session_factory is None:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Publishing repository is unavailable.")
    with session_factory() as session:
        item = session.scalar(
            select(PublishBatchItemRow).where(
                PublishBatchItemRow.batch_id == batch_id,
                PublishBatchItemRow.id == item_id,
            )
        )
        if item is None:
            return None, None
        package = session.get(PublishPackageRow, item.publish_package_id)
        if package is None:
            raise NodeExecutionError(c.ErrorCode.artifact_missing, "Publish package is missing.")
        video_ref = c.ArtifactRef.model_validate(package.video_artifact)
        item_input = SimpleNamespace(
            title=item.title,
            description=item.description,
            publish_content=item.publish_content,
            cover_title=item.cover_title,
            cover_subtitle=item.cover_subtitle,
            tags=list(item.tags or []),
        )
        package_input = SimpleNamespace(
            id=package.id,
            case_id=package.case_id,
            video_uri=video_ref.uri,
        )
        return item_input, package_input


def _write_sql_cover_result(
    batch_id: str,
    item_id: str,
    cover_artifact: c.ArtifactRef,
    request: Request,
) -> None:
    session_factory = getattr(request.app.state, "sqlalchemy_session_factory", None)
    if session_factory is None:
        return
    with session_factory() as session:
        item = session.scalar(
            select(PublishBatchItemRow).where(
                PublishBatchItemRow.batch_id == batch_id,
                PublishBatchItemRow.id == item_id,
            )
        )
        if item is None:
            return
        package = session.get(PublishPackageRow, item.publish_package_id)
        item.cover_artifact_id = cover_artifact.artifact_id
        item.updated_at = c.utcnow()
        if package is not None:
            package.cover_artifact = cover_artifact.model_dump(mode="json")
            package.updated_at = c.utcnow()
        session.commit()


def _generate_publish_cover_sql(
    batch_id: str,
    item_id: str,
    payload: c.GeneratePublishCoverRequest,
    request: Request,
) -> c.PublishCoverResult | JSONResponse:
    item, package = _sql_publish_cover_inputs(batch_id, item_id, request)
    if item is None or package is None:
        return not_found_response("Publish item not found")
    if not package.video_uri:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Publish package has no source video.")
    cover = nodes.run_cover_node(
        request,
        video_uri=package.video_uri,
        mode=payload.mode,
        frame_time_sec=payload.frame_time_sec,
        item=item,
        case_id=package.case_id,
    )
    _write_sql_cover_result(batch_id, item_id, cover.artifact_ref, request)
    return c.PublishCoverResult(
        cover_artifact=cover.artifact_ref,
        source=cover.source,
        frame_fallback=cover.frame_fallback,
        degraded_reason=cover.degraded_reason,
    )


def generate_publish_cover(
    batch_id: str, item_id: str, payload: c.GeneratePublishCoverRequest, request: Request
) -> c.PublishCoverResult | JSONResponse:
    """Publishing Cover Node endpoint (§28.3 generate-cover)."""
    return _generate_publish_cover_sql(batch_id, item_id, payload, request)


def _preview_publish_cover_frame_sql(
    batch_id: str,
    item_id: str,
    payload: c.PreviewCoverFrameRequest,
    request: Request,
) -> c.PreviewCoverFrameResult | JSONResponse:
    item, package = _sql_publish_cover_inputs(batch_id, item_id, request)
    if item is None or package is None:
        return not_found_response("Publish item not found")
    if not package.video_uri:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Publish package has no source video.")
    frame_ref = nodes.run_preview_frame(
        request,
        video_uri=package.video_uri,
        frame_time_sec=payload.frame_time_sec,
        case_id=package.case_id,
    )
    return c.PreviewCoverFrameResult(frame_artifact=frame_ref, frame_time_sec=payload.frame_time_sec)


def preview_publish_cover_frame(
    batch_id: str, item_id: str, payload: c.PreviewCoverFrameRequest, request: Request
) -> c.PreviewCoverFrameResult | JSONResponse:
    """Operator source-frame preview endpoint (§28.3 preview-cover-frame)."""
    return _preview_publish_cover_frame_sql(batch_id, item_id, payload, request)


def platform_accounts(
    request: Request, account_group: str | None = None, case_name: str | None = None, adapter_id: str | None = None
) -> c.PlatformAccountList:
    """List publish accounts discoverable through the resolved platform adapter
    (§28.3 platform-accounts). Default is 小V猫 CDP; unavailable 小V猫 returns an
    explicit unavailable reason and an empty account list."""
    adapter = select_adapter(adapter_id)
    accounts, available, reason = adapter.probe_accounts(account_group=account_group, case_name=case_name)
    return c.PlatformAccountList(
        adapter_id=adapter.adapter_id,
        accounts=accounts,
        available=available,
        unavailable_reason=reason,
    )


def delete_publish_item(item_id: str, request: Request) -> c.OkResponse | JSONResponse:
    deleted = publishing_repository(request).delete_item(item_id)
    if not deleted:
        return not_found_response("Publish item not found")
    return c.OkResponse(request_id=request_id())


def publish_attempt(request: Request, attempt_id: str) -> c.PublishAttemptDetail | JSONResponse:
    detail = publishing_repository(request).attempt_detail(attempt_id)
    if detail is None:
        return not_found_response("Publish attempt not found")
    return detail
