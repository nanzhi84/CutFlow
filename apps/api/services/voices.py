from __future__ import annotations


from fastapi import Request

from apps.api.common import (
    media_repository,
    object_store,
    provider_repository,
    repository,
    request_id,
)
from packages.core import contracts as c
from packages.core.config.settings import sandbox_fallback_allowed
from packages.core.storage.repository import new_id
from packages.core.workflow import NodeExecutionError
from packages.ai.gateway import ProviderCall
from packages.media.voice_provider_bridge import (
    hydrate_voice_reference_upload,
    load_voice,
    persist_provider_preview,
    persist_provider_voice,
)


def _voice_tts_profile_id(provider_profile_id: str | None) -> str:
    if provider_profile_id:
        return provider_profile_id
    if sandbox_fallback_allowed():
        return "sandbox.tts.default"
    raise NodeExecutionError(
        c.ErrorCode.provider_unsupported_option,
        "未指定真实 TTS 供应商配置。请先在「设置」中配置并启用真实 TTS 供应商，再克隆音色。",
    )


def _vendor_from_provider_id(provider_id: str | None) -> str:
    """Derive a vendor tag from a profile/provider id (e.g. 'volcengine.tts.prod'
    -> 'volcengine'). Sandbox maps to '' so it groups under '未指定厂商'.

    Mirrors ``packages.media.sqlalchemy_repository._vendor_from_profile_id`` (the
    repo backfill path); keep the two in sync if the vendor/sandbox rule changes."""
    if not provider_id:
        return ""
    head = provider_id.split(".", 1)[0]
    return "" if head == "sandbox" else head


def list_voices(
    request: Request,
    limit: int = 50,
    source: str | None = None,
    vendor: str | None = None,
    enabled: bool | None = None,
) -> c.PageResponse[c.VoiceProfile]:
    values = media_repository(request).list_voices(
        source=source, vendor=vendor, enabled=enabled, limit=limit
    )
    return c.PageResponse(items=values, total_hint=len(values), request_id=request_id())


def _resolve_sync_profiles(
    provider_profile_id: str | None, request: Request
) -> list[c.ProviderProfile]:
    """Resolve the TTS profiles to sync.

    An explicit profile id syncs just that vendor; otherwise ALL enabled,
    non-sandbox ``tts.speech`` profiles with a registered plugin and an active
    secret are synced, so a one-click sync pulls every vendor's cloned voices.
    """
    gateway = request.app.state.provider_gateway
    if provider_profile_id:
        profile = _tts_provider_profile(provider_profile_id, request, missing_ok=False)
        if profile is None:
            raise NodeExecutionError(
                c.ErrorCode.provider_unsupported_option,
                "所选 TTS 供应商配置不可用（未启用或缺少有效密钥）。",
            )
        return [profile]
    provider_repo = provider_repository(request)
    candidates = provider_repo.list_profiles(capability="tts.speech", limit=200)
    resolved: list[c.ProviderProfile] = []
    for profile in candidates:
        if not profile.enabled or profile.provider_id == "sandbox":
            continue
        if profile.provider_id not in gateway.plugins:
            continue
        if profile.secret_ref and not gateway._secret_is_active(profile.secret_ref):
            continue
        resolved.append(profile)
    if not resolved:
        raise NodeExecutionError(
            c.ErrorCode.provider_unsupported_option,
            "未配置真实 TTS 供应商，无法同步音色。请先在「设置」中配置并启用真实 TTS 供应商及密钥。",
        )
    return resolved


def sync_voices(payload: c.SyncVoicesRequest, request: Request) -> c.SyncVoicesResponse:
    profiles = _resolve_sync_profiles(payload.provider_profile_id, request)
    media_repo = media_repository(request)
    imported = 0
    updated = 0
    saved: list[c.VoiceProfile] = []
    for profile in profiles:
        invocation, result = request.app.state.provider_gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="tts.speech",
                input={"operation": "voice_list"},
            )
        )
        if result is None or invocation.error:
            # A vendor that does not support voice_list (or transiently fails) is
            # skipped so one bad vendor never blocks syncing the others.
            continue
        remote = result.output.get("voices")
        remote = remote if isinstance(remote, list) else []
        vendor = _vendor_from_provider_id(profile.id)
        for item in remote:
            if not isinstance(item, dict):
                continue
            voice_id = str(item.get("voice_id") or "").strip()
            if not voice_id:
                continue
            source = str(item.get("source") or "cloned")
            if source not in ("cloned", "designed", "builtin"):
                source = "cloned"
            raw_status = str(item.get("status") or "ready")
            status = raw_status if raw_status in ("ready", "training", "failed") else "ready"
            display_name = str(item.get("display_name") or "").strip() or voice_id
            voice, created = media_repo.upsert_voice(
                voice_id=voice_id,
                display_name=display_name,
                source=source,
                provider_profile_id=profile.id,
                vendor=vendor,
                status=status,
            )
            saved.append(voice)
            if created:
                imported += 1
            else:
                updated += 1
    return c.SyncVoicesResponse(
        imported=imported,
        updated=updated,
        total=len(saved),
        voices=saved,
        request_id=request_id(),
    )


def clone_voice(payload: c.CloneVoiceRequest, request: Request) -> c.VoiceProfile:
    media_repo = media_repository(request)
    provider_voice = _provider_voice_build(
        payload.provider_profile_id,
        request,
        operation="clone",
        display_name=payload.display_name,
        source="cloned",
        input_payload={"reference_upload_session_id": payload.reference_upload_session_id},
        before_invoke=lambda repo: hydrate_voice_reference_upload(
            media_repo, repo, payload.reference_upload_session_id
        ),
    )
    if provider_voice is not None:
        return persist_provider_voice(media_repo, provider_voice)
    resolved = _voice_tts_profile_id(payload.provider_profile_id)
    return media_repo.clone_voice(payload.model_copy(update={"provider_profile_id": resolved}))


def _provider_voice_build(
    provider_profile_id: str | None,
    request: Request,
    *,
    operation: str,
    display_name: str,
    source: str,
    input_payload: dict,
    before_invoke=None,
) -> c.VoiceProfile | None:
    if not provider_profile_id:
        return None
    repo = repository(request)
    profile = _tts_provider_profile(provider_profile_id, request, missing_ok=False)
    if profile is None:
        return None
    if profile.capability != "tts.speech":
        raise NodeExecutionError(c.ErrorCode.provider_unsupported_option, "Voice provider profile is invalid.")
    if before_invoke is not None:
        before_invoke(repo)
    invocation, result = request.app.state.provider_gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"operation": operation, "display_name": display_name, **input_payload},
        )
    )
    if result is None or invocation.error:
        raise NodeExecutionError(
            invocation.error.code if invocation.error else c.ErrorCode.provider_remote_failed,
            invocation.error.message if invocation.error else "Voice provider failed.",
        )
    voice_id = str(result.output.get("voice_id") or new_id("voice"))
    preview_artifact_id = result.output.get("preview_audio_artifact_id")
    raw_status = str(result.output.get("status") or "")
    status = "training" if raw_status == "training" else ("failed" if raw_status == "failed" else "ready")
    voice = c.VoiceProfile(
        id=voice_id,
        display_name=display_name,
        source=source,
        vendor=_vendor_from_provider_id(profile.id),
        provider_profile_id=profile.id,
        preview_artifact_id=preview_artifact_id if isinstance(preview_artifact_id, str) else None,
        status=status,
    )
    repo.voices[voice.id] = voice
    return voice


def voice_preview(voice_id: str, payload: c.VoicePreviewRequest, request: Request) -> c.VoicePreviewResponse:
    response = _resolve_voice_preview(voice_id, payload, request)
    return _sign_preview_audio(response, request)


def _sign_preview_audio(response: c.VoicePreviewResponse, request: Request) -> c.VoicePreviewResponse:
    """Replace a storage URI (s3://, oss://, local://) with a browser-playable
    presigned HTTPS URL so the library试听 player can load the audio directly."""
    uri = response.audio_artifact.uri
    if not uri or uri.startswith(("http://", "https://")) or "://" not in uri:
        return response
    try:
        signed = object_store(request).signed_url(uri).url
    except Exception:
        return response
    return response.model_copy(
        update={"audio_artifact": response.audio_artifact.model_copy(update={"uri": signed})}
    )


def _resolve_voice_preview(voice_id: str, payload: c.VoicePreviewRequest, request: Request) -> c.VoicePreviewResponse:
    media_repo = media_repository(request)
    voice = load_voice(media_repo, voice_id)
    if voice is not None:
        response = _provider_voice_preview(voice, payload, request)
        if response is not None:
            artifact = repository(request).artifacts.get(response.audio_artifact.artifact_id)
            if artifact is not None:
                artifact_ref = persist_provider_preview(media_repo, voice_id, artifact)
                return response.model_copy(update={"audio_artifact": artifact_ref})
            return response
        if not sandbox_fallback_allowed():
            raise NodeExecutionError(
                c.ErrorCode.provider_unsupported_option,
                "未配置真实 TTS 供应商，无法生成音色试听。请先在「设置」中配置并启用真实 TTS 供应商及密钥。",
            )
    response = media_repo.preview_voice(voice_id, payload)
    if response is None:
        raise NodeExecutionError(c.ErrorCode.validation_missing_voice, "Voice not found.")
    return response


def _provider_voice_preview(
    voice: c.VoiceProfile,
    payload: c.VoicePreviewRequest,
    request: Request,
) -> c.VoicePreviewResponse | None:
    repo = repository(request)
    voice_id = voice.id
    provider_profile_id = payload.provider_profile_id or voice.provider_profile_id
    profile = _tts_provider_profile(provider_profile_id, request, missing_ok=True)
    if profile is not None:
        invocation, result = request.app.state.provider_gateway.invoke(
            ProviderCall(
                provider_profile_id=profile.id,
                capability_id="tts.speech",
                input={"text": payload.text, "voice_id": voice_id},
            )
        )
        if result is None or invocation.error:
            raise NodeExecutionError(
                invocation.error.code if invocation.error else c.ErrorCode.provider_remote_failed,
                invocation.error.message if invocation.error else "Voice preview provider failed.",
            )
        artifact_id = result.output.get("audio_artifact_id")
        if isinstance(artifact_id, str) and artifact_id in repo.artifacts:
            repo.voices[voice_id] = voice.model_copy(
                update={"preview_artifact_id": artifact_id, "updated_at": c.utcnow()}
            )
            artifact = repo.artifacts[artifact_id]
            duration = (
                float(artifact.media_info.duration_sec)
                if artifact.media_info and artifact.media_info.duration_sec
                else float(result.output.get("duration_sec") or result.audio_seconds or 0)
            )
            return c.VoicePreviewResponse(
                voice_id=voice_id,
                audio_artifact=repo.artifact_ref(artifact_id),
                duration_sec=duration,
            )
    return None


def _tts_provider_profile(provider_profile_id: str | None, request: Request, *, missing_ok: bool):
    if not provider_profile_id:
        return None
    gateway = request.app.state.provider_gateway
    profile = None
    reader = getattr(gateway, "provider_reader", None)
    if reader is not None:
        profile = reader.get_profile(provider_profile_id)
    if profile is None:
        profile = repository(request).provider_profiles.get(provider_profile_id)
    if profile is None:
        if missing_ok:
            return None
        raise NodeExecutionError(c.ErrorCode.provider_unsupported_option, "Voice provider profile is invalid.")
    if profile.capability != "tts.speech":
        raise NodeExecutionError(c.ErrorCode.provider_unsupported_option, "Voice provider profile is invalid.")
    if not profile.enabled:
        return None
    if profile.provider_id not in gateway.plugins:
        return None
    if profile.secret_ref and not gateway._secret_is_active(profile.secret_ref):
        return None
    return profile


def refresh_voice_status(voice_id: str, request: Request) -> c.VoiceProfile:
    """Poll a training (Volcengine clone) voice's status and persist any change.

    Only ``training`` voices need refreshing; others return as-is. Queries the
    provider's ``train_status`` operation; on Success the voice flips to ready, on
    failure to failed. Provider/transient errors leave it ``training`` to retry.
    """
    media_repo = media_repository(request)
    voice = load_voice(media_repo, voice_id)
    if voice is None:
        raise NodeExecutionError(c.ErrorCode.validation_missing_voice, "Voice not found.")
    if voice.status != "training":
        return voice
    profile = _tts_provider_profile(voice.provider_profile_id, request, missing_ok=True)
    if profile is None:
        return voice
    invocation, result = request.app.state.provider_gateway.invoke(
        ProviderCall(
            provider_profile_id=profile.id,
            capability_id="tts.speech",
            input={"operation": "train_status", "voice_id": voice.id},
        )
    )
    if result is None or invocation.error:
        return voice
    new_status = str(result.output.get("status") or "training")
    if new_status == voice.status:
        return voice
    updated, _ = media_repo.upsert_voice(
        voice_id=voice.id,
        display_name=voice.display_name,
        source=voice.source,
        provider_profile_id=voice.provider_profile_id or "",
        vendor=voice.vendor,
        status=new_status,
    )
    return updated


def patch_voice(voice_id: str, payload: c.PatchVoiceRequest, request: Request) -> c.VoiceProfile:
    voice = media_repository(request).patch_voice(voice_id, payload)
    if voice is None:
        raise NodeExecutionError(c.ErrorCode.validation_missing_voice, "Voice not found.")
    return voice


def delete_voice(voice_id: str, request: Request) -> c.OkResponse:
    media_repository(request).delete_voice(voice_id)
    return c.OkResponse(request_id=request_id())
