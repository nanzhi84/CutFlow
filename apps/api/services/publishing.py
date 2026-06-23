from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from apps.api.common import (
    accounts_repository,
    ensure_artifact_ref,
    object_store,
    page,
    publishing_repository,
    repository,
    request_id,
)
from apps.api.dependencies import not_found_response
from apps.api.services import publishing_nodes as nodes
from packages.core import contracts as c
from packages.core.contracts.state_machines import assert_transition
from packages.core.storage.object_store import parse_object_uri
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.core.observability import record_funnel_event
from packages.media.video import FfmpegCommandError
from packages.publishing import MemoryAccountsRepository, normalize_publish_tags, normalize_scheduled_at, select_adapter
from packages.publishing.platform_adapter import PublishOutcome, PublishPayload
from packages.publishing.publish_executor import run_item_publish


def _publish_run_ids(repo, package_id: str | None) -> tuple[str | None, str | None]:
    """Best-effort resolution of (run_id, job_id) for a publish package so funnel
    events stay linked to the originating run/job. Returns (None, None) when the
    package is detached from a finished video (e.g. raw upload-artifact packages)."""

    package = repo.publish_packages.get(package_id) if package_id else None
    finished_video_id = getattr(package, "source_finished_video_id", None) if package else None
    finished = repo.finished_videos.get(finished_video_id) if finished_video_id else None
    run_id = getattr(finished, "run_id", None) if finished else None
    run = repo.runs.get(run_id) if run_id else None
    job_id = getattr(run, "job_id", None) if run else None
    return run_id, job_id


def _record_publish_attempt_funnel(repo, batch, item, attempt) -> None:
    """Emit the §9.5 publish-stage funnel events for one publish attempt.

    Always records ``publish_started`` (the attempt was submitted); then records
    the terminal §9.5 stage (``published`` for a published attempt,
    ``publish_failed`` for a failed one). Dry-run / manual-review-ready attempts
    emit only ``publish_started``. All writes are best-effort.

    ``published`` is the load-bearing true-yield success string (spec §9.5); the
    read side keys ``true_yield_rate`` on it (run-scoped, excluding qc_failed /
    manual_rejected runs)."""

    run_id, job_id = _publish_run_ids(repo, getattr(item, "publish_package_id", None))
    record_funnel_event(
        repo,
        event_type="publish_started",
        job_id=job_id,
        run_id=run_id,
        publish_attempt_id=attempt.id,
        dedupe_key=f"{attempt.id}:publish_started",
        event_time=attempt.created_at,
    )
    status_value = attempt.status.value if hasattr(attempt.status, "value") else str(attempt.status)
    if status_value == "published":
        record_funnel_event(
            repo,
            event_type="published",
            job_id=job_id,
            run_id=run_id,
            publish_attempt_id=attempt.id,
            dedupe_key=f"{attempt.id}:published",
            event_time=attempt.finished_at or attempt.updated_at,
        )
    elif status_value == "failed":
        record_funnel_event(
            repo,
            event_type="publish_failed",
            job_id=job_id,
            run_id=run_id,
            publish_attempt_id=attempt.id,
            dedupe_key=f"{attempt.id}:publish_failed",
            event_time=attempt.finished_at or attempt.updated_at,
        )


def _build_publish_runner(
    payload: c.SubmitPublishBatchRequest,
    request: Request,
) -> tuple[Callable[[object, object], PublishOutcome], str]:
    adapter = select_adapter(payload.adapter_id)
    runtime_repo = repository(request)
    target_repo = accounts_repository(request) or MemoryAccountsRepository(runtime_repo)
    objects = object_store(request)
    scheduled_at = normalize_scheduled_at(payload.mode, payload.scheduled_at)

    def runner(item: object, package: object) -> PublishOutcome:
        temp_dir: tempfile.TemporaryDirectory[str] | None = None
        downloaded_path: Path | None = None

        def resolve_video() -> str | None:
            nonlocal downloaded_path, temp_dir
            if downloaded_path is not None:
                return str(downloaded_path)
            video_uri = _artifact_uri(_get_field(package, "video_artifact"))
            if not video_uri:
                return None
            temp_dir = tempfile.TemporaryDirectory(prefix="cutagent-publish-")
            downloaded_path = objects.download_file(parse_object_uri(video_uri), Path(temp_dir.name) / "video")
            return str(downloaded_path)

        try:
            case_id = _get_field(package, "case_id")
            case = runtime_repo.cases.get(case_id) if case_id else None
            outcome, _per_account_results = run_item_publish(
                adapter,
                _build_runner_payload(
                    item,
                    package,
                    case_name=getattr(case, "name", None),
                    scheduled_at=scheduled_at,
                    simulate_failure=payload.simulate_publish_failure,
                ),
                targets=_active_publish_targets(target_repo, case_id, _get_field(item, "platform")),
                resolve_video=resolve_video,
            )
            return outcome
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()

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

    if publishing_repository(request) is not None:
        values = publishing_repository(request).list_packages(limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    return page(repository(request).publish_packages.values(), limit)


def create_publish_package(payload: c.CreatePublishPackageRequest, request: Request) -> c.PublishPackage:
    if publishing_repository(request) is not None:
        return publishing_repository(request).create_package(payload)
    repo = repository(request)
    if payload.source_finished_video_id:
        package = repo.create_publish_package_from_finished_video(
            repo.finished_videos[payload.source_finished_video_id],
            title=payload.title,
            description=payload.description,
        )
    elif not payload.upload_artifact_id:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Upload artifact is required.")
    else:
        package = c.PublishPackage(
            id=new_id("pkg"),
            upload_artifact_id=payload.upload_artifact_id,
            video_artifact=ensure_artifact_ref(request, payload.upload_artifact_id),
            platform_defaults=c.PublishDefaults(title=payload.title, description=payload.description),
        )
        repo.publish_packages[package.id] = package
    # Package creation is not a §9.5 funnel stage; the publish lifecycle is
    # tracked via publish_started / published / publish_failed at submit time.
    return package


def patch_publish_package(
    package_id: str, payload: c.PatchPublishPackageRequest, request: Request
) -> c.PublishPackage | JSONResponse:
    if publishing_repository(request) is not None:
        package = publishing_repository(request).patch_package(package_id, payload)
        if package is None:
            return not_found_response("Publish package not found")
        return package
    package = repository(request).publish_packages.get(package_id)
    if package is None:
        return not_found_response("Publish package not found")
    updates = {}
    if {"title", "description"} & payload.model_fields_set:
        defaults_updates = {}
        if "title" in payload.model_fields_set and payload.title is not None:
            defaults_updates["title"] = payload.title
        if "description" in payload.model_fields_set and payload.description is not None:
            defaults_updates["description"] = payload.description
        if defaults_updates:
            updates["platform_defaults"] = package.platform_defaults.model_copy(update=defaults_updates)
    if "cover_artifact_id" in payload.model_fields_set:
        updates["cover_artifact"] = (
            ensure_artifact_ref(request, payload.cover_artifact_id) if payload.cover_artifact_id else None
        )
    if updates:
        updates["updated_at"] = c.utcnow()
    updated = package.model_copy(update=updates)
    repository(request).publish_packages[package_id] = updated
    return updated


def publish_batches(
    request: Request, limit: int = 50, case_id: str | None = None
) -> c.PageResponse[c.PublishBatchVm]:

    if publishing_repository(request) is not None:
        values = publishing_repository(request).list_batches(limit=limit, case_id=case_id)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    values = list(repository(request).publish_batches.values())
    if case_id:
        values = [
            batch
            for batch in values
            if _publish_batch_matches_case(repository(request), batch, case_id)
        ]
    return page(values, limit)


def create_publish_batch(payload: c.CreatePublishBatchRequest, request: Request) -> c.PublishBatchVm:
    if publishing_repository(request) is not None:
        return publishing_repository(request).create_batch(payload)
    return repository(request).create_publish_batch(payload.publish_package_ids, payload.platform_targets)


def _publish_batch_matches_case(repo, batch: c.PublishBatchVm, case_id: str) -> bool:
    for item in batch.items:
        package = repo.publish_packages.get(item.publish_package_id)
        if package is not None and package.case_id == case_id:
            return True
    return False


def publish_batch_detail(request: Request, batch_id: str) -> c.PublishBatchVm | JSONResponse:

    if publishing_repository(request) is not None:
        batch = publishing_repository(request).get_batch(batch_id)
        if batch is None:
            return not_found_response("Publish batch not found")
        return batch
    batch = repository(request).publish_batches.get(batch_id)
    if batch is None:
        return not_found_response("Publish batch not found")
    return batch


def publish_batch_attempts(
    request: Request, batch_id: str, limit: int = 50
) -> c.PageResponse[c.PublishAttempt] | JSONResponse:
    if publishing_repository(request) is not None:
        if publishing_repository(request).get_batch(batch_id) is None:
            return not_found_response("Publish batch not found")
        values = publishing_repository(request).list_attempts(batch_id, limit=limit)
        return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())
    if batch_id not in repository(request).publish_batches:
        return not_found_response("Publish batch not found")
    values = [
        attempt
        for attempt in repository(request).publish_attempts.values()
        if attempt.batch_id == batch_id
    ]
    values.sort(key=lambda item: item.created_at, reverse=True)
    return c.PageResponse(items=values[:limit], total_hint=len(values), request_id=request_id())


def delete_publish_batch(batch_id: str, request: Request) -> c.OkResponse | JSONResponse:
    if publishing_repository(request) is not None:
        deleted = publishing_repository(request).delete_batch(batch_id)
        if not deleted:
            return not_found_response("Publish batch not found")
        return c.OkResponse(request_id=request_id())
    if batch_id not in repository(request).publish_batches:
        return not_found_response("Publish batch not found")
    repository(request).publish_batches.pop(batch_id, None)
    repository(request).publish_attempts = {
        attempt_id: attempt
        for attempt_id, attempt in repository(request).publish_attempts.items()
        if attempt.batch_id != batch_id
    }
    return c.OkResponse(request_id=request_id())


def submit_publish_batch(
    batch_id: str,
    payload: c.SubmitPublishBatchRequest,
    request: Request,
    publish_runner: Callable[[object, object], PublishOutcome] | None = None,
) -> c.PublishBatchVm | JSONResponse:
    default_publish_runner, adapter_id = _build_publish_runner(payload, request)
    active_publish_runner = publish_runner or default_publish_runner
    if publishing_repository(request) is not None:
        batch = publishing_repository(request).submit_batch(
            batch_id,
            payload,
            publish_runner=active_publish_runner,
        )
        if batch is None:
            return not_found_response("Publish batch not found")
        return batch
    repo = repository(request)
    batch = repo.publish_batches.get(batch_id)
    if batch is None:
        return not_found_response("Publish batch not found")
    # Resolve the publish adapter (小V猫 CDP by default; sandbox only when explicitly
    # selected via payload/env) and normalize the Asia/Shanghai schedule (§23.7).
    # A 'scheduled' submit yields scheduled attempts that have not yet published.
    scheduled_at = normalize_scheduled_at(payload.mode, payload.scheduled_at)
    is_scheduled = scheduled_at is not None

    new_items = []
    selected_count = 0
    any_failed = False
    for item in batch.items:
        if not item.selected:
            new_items.append(item)
            continue
        selected_count += 1
        package = repo.publish_packages.get(item.publish_package_id)

        # Stage 1 (normalizing) + Stage 3 (copy_running): the copy node produces
        # real publish copy (title / publish_content / cover_title / cover_subtitle)
        # for the item when not already operator-edited. Uses the armed llm.chat
        # provider when available, else the deterministic derivation — never a no-op.
        copy_updates: dict = {}
        if not item.publish_content or not item.cover_title:
            copy, _source, _inv = nodes.run_copy_node(
                repo,
                package,
                item,
                gateway=request.app.state.provider_gateway,
                prompt_registry=request.app.state.prompt_registry,
            )
            copy_updates = {
                "title": item.title or copy.title,
                "publish_content": item.publish_content or copy.publish_content,
                "cover_title": item.cover_title or copy.cover_title,
                "cover_subtitle": item.cover_subtitle or copy.cover_subtitle,
            }
        normalize_disposition = _normalize_disposition(request, package)

        current_item_status = item.status
        for next_status in ["normalizing", "asr_running", "copy_running", "cover_running", "review_ready"]:
            assert_transition("publish_item", current_item_status, next_status)
            current_item_status = next_status

        # Stage: publish via the resolved adapter.
        outcome = None
        if not payload.dry_run:
            assert_transition("publish_item", current_item_status, "publishing")
            current_item_status = "publishing"
            outcome = active_publish_runner(
                item.model_copy(update=copy_updates) if copy_updates else item,
                package,
            )
            if not outcome.success:
                any_failed = True
                assert_transition("publish_item", current_item_status, "publish_failed")
                current_item_status = "publish_failed"
            else:
                assert_transition("publish_item", current_item_status, "published")
                current_item_status = "published"

        item_updates = {"status": c.PublishItemStatus(current_item_status), "updated_at": c.utcnow()}
        item_updates.update(copy_updates)
        if is_scheduled and not payload.dry_run:
            item_updates["scheduled_at"] = scheduled_at
        new_items.append(item.model_copy(update=item_updates))

        if payload.dry_run:
            attempt_status = "manual_review_ready"
        elif outcome is not None and not outcome.success:
            attempt_status = "failed"
        elif is_scheduled:
            attempt_status = "scheduled"
        else:
            attempt_status = "published"
        assert_transition("publish_attempt", "created", attempt_status)
        attempt_results = list(outcome.results) if outcome is not None else []
        attempt_results.append({"normalize": normalize_disposition})
        attempt = c.PublishAttempt(
            id=new_id("pub_attempt"),
            batch_id=batch.id,
            item_id=item.id,
            platforms=[item.platform],
            manual_review=payload.dry_run,
            status=c.PublishAttemptStatus(attempt_status),
            adapter_id=outcome.adapter_id if outcome is not None else adapter_id,
            external_task_id=outcome.external_task_id if outcome is not None else None,
            results=attempt_results,
            error=(
                c.NodeError(
                    code=c.ErrorCode.publish_failed,
                    message=(outcome.error_message if outcome else "Publish failed."),
                    retryable=True,
                )
                if attempt_status == "failed"
                else None
            ),
            finished_at=c.utcnow() if attempt_status == "published" else None,
        )
        repo.publish_attempts[attempt.id] = attempt
        _record_publish_attempt_funnel(repo, batch, item, attempt)
    if selected_count == 0:
        raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "At least one publish item must be selected.")
    assert_transition("publish_batch", batch.status, "processing")
    next_batch_status = "review_ready" if payload.dry_run else "publishing"
    assert_transition("publish_batch", "processing", next_batch_status)
    if not payload.dry_run:
        if any_failed:
            assert_transition("publish_batch", next_batch_status, "partial_failed")
            next_batch_status = "partial_failed"
        else:
            assert_transition("publish_batch", next_batch_status, "completed")
            next_batch_status = "completed"
    batch = batch.model_copy(
        update={"status": c.PublishBatchStatus(next_batch_status), "items": new_items, "updated_at": c.utcnow()}
    )
    repo.publish_batches[batch.id] = batch
    return batch


def _normalize_disposition(request: Request, package) -> str:
    """Honest disposition for the §13 Normalize stage (no silent no-op).

    When the source video is a resolvable local object, probe it and transcode to
    platform-safe codecs only when needed (H.264/AAC/yuv420p/mp4). Synthetic /
    non-local sources (e.g. sandbox URIs) are reported as ``skipped_unresolvable_source``
    rather than silently doing nothing.
    """
    video_uri = getattr(getattr(package, "video_artifact", None), "uri", None)
    if not video_uri or not str(video_uri).startswith("local://"):
        return "skipped_unresolvable_source"
    try:
        from apps.api.common import object_store
        from packages.core.storage.object_store import parse_object_uri
        from packages.media.video import needs_normalize_for_upload
        import tempfile
        from pathlib import Path

        store = object_store(request)
        ref = parse_object_uri(video_uri)
        with tempfile.TemporaryDirectory(prefix="cutagent-normalize-") as directory:
            local = store.download_file(ref, Path(directory) / "source")
            return "needs_normalize" if needs_normalize_for_upload(local) else "already_compliant"
    except (FfmpegCommandError, ValueError, OSError):
        return "skipped_unresolvable_source"


def retry_publish_item(batch_id: str, item_id: str, request: Request) -> c.PublishBatchItemVm | JSONResponse:
    if publishing_repository(request) is not None:
        return not_found_response("retry-publish is only implemented by the local sandbox adapter")
    batch = repository(request).publish_batches.get(batch_id)
    if batch is None:
        return not_found_response("Publish batch not found")
    for index, item in enumerate(batch.items):
        if item.id != item_id:
            continue
        if item.status != c.PublishItemStatus.publish_failed:
            raise NodeExecutionError(c.ErrorCode.validation_invalid_options, "Publish item is not failed.")
        # Re-run through the sandbox adapter (this path is memory-backend only; the
        # production SQL backend returns 404 above). Honest: only advance to
        # published when the adapter actually reports success — never hard-code it.
        retry_outcome = select_adapter("sandbox.publish").publish(
            PublishPayload(
                title=item.title,
                description=getattr(item, "description", "") or "",
                platforms=(item.platform,),
            )
        )
        if not retry_outcome.success:
            raise NodeExecutionError(
                c.ErrorCode.validation_invalid_options,
                retry_outcome.error_message or "Retry publish failed.",
            )
        current_item_status = item.status
        assert_transition("publish_item", current_item_status, "publishing")
        current_item_status = "publishing"
        assert_transition("publish_item", current_item_status, "published")
        updated = item.model_copy(update={"status": c.PublishItemStatus.published, "updated_at": c.utcnow()})
        items = list(batch.items)
        items[index] = updated
        next_batch_status = batch.status
        if next_batch_status == c.PublishBatchStatus.partial_failed:
            assert_transition("publish_batch", next_batch_status, "publishing")
            next_batch_status = c.PublishBatchStatus.publishing
        if all(existing.status == c.PublishItemStatus.published for existing in items):
            assert_transition("publish_batch", next_batch_status, "completed")
            next_batch_status = c.PublishBatchStatus.completed
        repository(request).publish_batches[batch.id] = batch.model_copy(
            update={"status": next_batch_status, "items": items, "updated_at": c.utcnow()}
        )
        attempt = c.PublishAttempt(
            id=new_id("pub_attempt"),
            batch_id=batch.id,
            item_id=item.id,
            platforms=[item.platform],
            manual_review=False,
            status=c.PublishAttemptStatus.published,
            adapter_id=retry_outcome.adapter_id,
            results=retry_outcome.results or [{"retry": True}],
            finished_at=c.utcnow(),
        )
        repository(request).publish_attempts[attempt.id] = attempt
        _record_publish_attempt_funnel(repository(request), batch, item, attempt)
        return updated
    return not_found_response("Publish item not found")


def patch_publish_item(
    item_id: str, payload: c.PatchPublishItemRequest, request: Request
) -> c.PublishBatchItemVm | JSONResponse:
    if publishing_repository(request) is not None:
        item = publishing_repository(request).patch_item(item_id, payload)
        if item is None:
            return not_found_response("Publish item not found")
        return item
    for batch in repository(request).publish_batches.values():
        for index, item in enumerate(batch.items):
            if item.id == item_id:
                item_updates = payload.model_dump(exclude_none=True)
                if "tags" in item_updates:
                    item_updates["tags"] = normalize_publish_tags(item_updates["tags"])
                updated = item.model_copy(update={**item_updates, "updated_at": c.utcnow()})
                items = list(batch.items)
                items[index] = updated
                repository(request).publish_batches[batch.id] = batch.model_copy(update={"items": items})
                return updated
    return not_found_response("Publish item not found")


def _find_item_in_memory(repo, batch_id: str, item_id: str):
    batch = repo.publish_batches.get(batch_id)
    if batch is None:
        return None, None, None
    for index, item in enumerate(batch.items):
        if item.id == item_id:
            return batch, index, item
    return batch, None, None


def _replace_item_in_memory(repo, batch, index: int, updated) -> None:
    items = list(batch.items)
    items[index] = updated
    repo.publish_batches[batch.id] = batch.model_copy(update={"items": items, "updated_at": c.utcnow()})


def generate_publish_copy(
    batch_id: str, item_id: str, payload: c.GeneratePublishCopyRequest, request: Request
) -> c.PublishCopyResult | JSONResponse:
    """Publishing Copy Node endpoint (§28.3 generate-copy)."""
    if publishing_repository(request) is not None:
        return JSONResponse(
            status_code=501,
            content={"detail": "generate-copy is implemented by the local sandbox backend."},
        )
    repo = repository(request)
    batch, index, item = _find_item_in_memory(repo, batch_id, item_id)
    if item is None:
        return not_found_response("Publish item not found")
    package = repo.publish_packages.get(item.publish_package_id)
    copy, source, invocation_id = nodes.run_copy_node(
        repo,
        package,
        item,
        title_limit=payload.title_limit,
        gateway=request.app.state.provider_gateway,
        prompt_registry=request.app.state.prompt_registry,
    )
    updates = {
        "publish_content": copy.publish_content,
        "cover_title": copy.cover_title,
        "cover_subtitle": copy.cover_subtitle,
        "updated_at": c.utcnow(),
    }
    if payload.overwrite or not item.title:
        updates["title"] = copy.title
    _replace_item_in_memory(repo, batch, index, item.model_copy(update=updates))
    return c.PublishCopyResult(
        title=updates.get("title", item.title),
        publish_content=copy.publish_content,
        cover_title=copy.cover_title,
        cover_subtitle=copy.cover_subtitle,
        source=source,
        prompt_invocation_id=invocation_id,
    )


def generate_publish_cover(
    batch_id: str, item_id: str, payload: c.GeneratePublishCoverRequest, request: Request
) -> c.PublishCoverResult | JSONResponse:
    """Publishing Cover Node endpoint (§28.3 generate-cover)."""
    if publishing_repository(request) is not None:
        return JSONResponse(
            status_code=501,
            content={"detail": "generate-cover is implemented by the local sandbox backend."},
        )
    repo = repository(request)
    batch, index, item = _find_item_in_memory(repo, batch_id, item_id)
    if item is None:
        return not_found_response("Publish item not found")
    package = repo.publish_packages.get(item.publish_package_id)
    video_uri = getattr(getattr(package, "video_artifact", None), "uri", None)
    if not video_uri:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Publish package has no source video.")
    case_id = getattr(package, "case_id", None)
    cover = nodes.run_cover_node(
        request,
        video_uri=video_uri,
        mode=payload.mode,
        frame_time_sec=payload.frame_time_sec,
        item=item,
        case_id=case_id,
    )
    _replace_item_in_memory(
        repo,
        batch,
        index,
        item.model_copy(update={"cover_artifact_id": cover.artifact_ref.artifact_id, "updated_at": c.utcnow()}),
    )
    return c.PublishCoverResult(
        cover_artifact=cover.artifact_ref,
        source=cover.source,
        frame_fallback=cover.frame_fallback,
        degraded_reason=cover.degraded_reason,
    )


def preview_publish_cover_frame(
    batch_id: str, item_id: str, payload: c.PreviewCoverFrameRequest, request: Request
) -> c.PreviewCoverFrameResult | JSONResponse:
    """Operator source-frame preview endpoint (§28.3 preview-cover-frame)."""
    if publishing_repository(request) is not None:
        return JSONResponse(
            status_code=501,
            content={"detail": "preview-cover-frame is implemented by the local sandbox backend."},
        )
    repo = repository(request)
    batch, index, item = _find_item_in_memory(repo, batch_id, item_id)
    if item is None:
        return not_found_response("Publish item not found")
    package = repo.publish_packages.get(item.publish_package_id)
    video_uri = getattr(getattr(package, "video_artifact", None), "uri", None)
    if not video_uri:
        raise NodeExecutionError(c.ErrorCode.artifact_missing, "Publish package has no source video.")
    case_id = getattr(package, "case_id", None)
    frame_ref = nodes.run_preview_frame(
        request,
        video_uri=video_uri,
        frame_time_sec=payload.frame_time_sec,
        case_id=case_id,
    )
    return c.PreviewCoverFrameResult(frame_artifact=frame_ref, frame_time_sec=payload.frame_time_sec)


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
    if publishing_repository(request) is not None:
        deleted = publishing_repository(request).delete_item(item_id)
        if not deleted:
            return not_found_response("Publish item not found")
        return c.OkResponse(request_id=request_id())
    for batch in repository(request).publish_batches.values():
        items = [item for item in batch.items if item.id != item_id]
        if len(items) == len(batch.items):
            continue
        repository(request).publish_batches[batch.id] = batch.model_copy(
            update={"items": items, "updated_at": c.utcnow()}
        )
        repository(request).publish_attempts = {
            attempt_id: attempt
            for attempt_id, attempt in repository(request).publish_attempts.items()
            if attempt.item_id != item_id
        }
        return c.OkResponse(request_id=request_id())
    return not_found_response("Publish item not found")


def publish_attempt(request: Request, attempt_id: str) -> c.PublishAttemptDetail | JSONResponse:

    if publishing_repository(request) is not None:
        detail = publishing_repository(request).attempt_detail(attempt_id)
        if detail is None:
            return not_found_response("Publish attempt not found")
        return detail
    attempt = repository(request).publish_attempts.get(attempt_id)
    if attempt is None:
        return not_found_response("Publish attempt not found")
    return c.PublishAttemptDetail(attempt=attempt, record=None)
