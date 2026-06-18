from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import (
    AnnotationEditorVm,
    AnnotationRunResponse,
    ArtifactRef,
    ArtifactKind,
    CloneVoiceRequest,
    CreateMediaAssetFromUploadRequest,
    DesignVoiceRequest,
    ErrorCode,
    MediaInfo,
    MediaAssetCard,
    MediaAssetDetail,
    MediaAssetRecord,
    MaterialUsageRankingReport,
    PatchAnnotationRequest,
    PatchVoiceRequest,
    RerunAnnotationRequest,
    SelectionLedgerEntry,
    SelectionMedium,
    VoicePreviewRequest,
    VoicePreviewResponse,
    VoiceProfile,
    utcnow,
)
from packages.core.storage.database import (
    AnnotationRow,
    ArtifactRow,
    MediaAssetRow,
    SelectionLedgerRow,
    UploadSessionRow,
    VoiceProfileRow,
)
from packages.core.storage.object_store import ObjectStore
from packages.core.storage.repository import new_id
from packages.core.storage.selection_ledger import material_usage_ranking_from_entries
from packages.core.workflow import NodeExecutionError


def media_asset_row_to_contract(
    row: MediaAssetRow, *, thumbnail_url: str | None = None
) -> MediaAssetRecord:
    return MediaAssetRecord(
        id=row.id,
        case_id=row.case_id,
        title=row.title,
        kind=row.kind,
        source_artifact_id=row.source_artifact_id,
        tags=list(row.tags or []),
        annotation_status=row.annotation_status,
        usable=row.usable,
        thumbnail_url=thumbnail_url,
        duration_sec=row.duration_sec,
        width=row.width,
        height=row.height,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _sign_evidence_images(canonical, object_store: ObjectStore | None):
    """Return a copy of ``canonical`` with ``evidence_frame_images[].image_url``
    signed into fetchable URLs (stored as ``s3://`` / ``objectstore://`` uris).
    No object store or no images => the canonical is returned unchanged."""
    if not isinstance(canonical, dict) or object_store is None:
        return canonical
    images = canonical.get("evidence_frame_images")
    if not isinstance(images, list) or not images:
        return canonical
    signed_images = []
    changed = False
    for image in images:
        url = image.get("image_url") if isinstance(image, dict) else None
        if isinstance(url, str) and url.startswith(("s3://", "objectstore://")):
            try:
                signed_images.append({**image, "image_url": object_store.signed_url(url).url})
                changed = True
                continue
            except Exception:
                pass
        signed_images.append(image)
    if not changed:
        return canonical
    return {**canonical, "evidence_frame_images": signed_images}


def annotation_row_to_editor(
    row: AnnotationRow, asset: MediaAssetRow, *, object_store: ObjectStore | None = None
) -> AnnotationEditorVm:
    return AnnotationEditorVm(
        asset=media_asset_row_to_contract(asset),
        etag=row.etag,
        canonical=_sign_evidence_images(row.canonical, object_store),
        projection=row.projection,
        editable_paths=list(row.editable_paths or []),
    )


def voice_row_to_contract(row: VoiceProfileRow) -> VoiceProfile:
    return VoiceProfile(
        id=row.id,
        display_name=row.display_name,
        source=row.source,
        provider_profile_id=row.provider_profile_id,
        preview_artifact_id=row.preview_artifact_id,
        enabled=row.enabled,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def artifact_ref_from_row(row: ArtifactRow) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=row.id,
        kind=ArtifactKind(row.kind),
        uri=row.uri or f"artifact://{row.id}",
        schema_version=row.schema_version,
        sha256=row.sha256,
    )


def asset_record_is_v4(canonical: dict) -> bool:
    """A canonical dict is a real AnnotationV4 only when it carries a V4 meta layer."""
    meta = canonical.get("meta") if isinstance(canonical, dict) else None
    return isinstance(meta, dict) and "asset_id" in meta


class SqlAlchemyMediaRepository:
    def __init__(
        self, session_factory: sessionmaker[Session], object_store: ObjectStore | None = None
    ) -> None:
        self.session_factory = session_factory
        self.object_store = object_store

    def _signed_thumbnail_url(self, thumbnail_uri: str | None) -> str | None:
        # Sign the stored thumbnail object into a fetchable URL for the card.
        # No object store (unit tests / legacy wiring) or no stored uri => None.
        if not thumbnail_uri or self.object_store is None:
            return None
        try:
            return self.object_store.signed_url(thumbnail_uri).url
        except Exception:
            return None

    def list_assets(
        self,
        *,
        limit: int = 50,
        case_id: str | None = None,
        kind: str | None = None,
        annotation_status: str | None = None,
    ) -> list[MediaAssetCard]:
        with self.session_factory() as session:
            statement = select(MediaAssetRow)
            if case_id:
                statement = statement.where(MediaAssetRow.case_id == case_id)
            if kind:
                statement = statement.where(MediaAssetRow.kind == kind)
            if annotation_status:
                statement = statement.where(MediaAssetRow.annotation_status == annotation_status)
            statement = statement.order_by(MediaAssetRow.updated_at.desc()).limit(limit)
            cards: list[MediaAssetCard] = []
            for row in session.scalars(statement):
                thumbnail_url = self._signed_thumbnail_url(row.thumbnail_uri)
                cards.append(
                    MediaAssetCard(
                        asset=media_asset_row_to_contract(row, thumbnail_url=thumbnail_url),
                        preview_url=f"local://media/{row.id}",
                        thumbnail_url=thumbnail_url,
                        duration_sec=row.duration_sec,
                        width=row.width,
                        height=row.height,
                    )
                )
            return cards

    def get_asset_detail(self, asset_id: str) -> MediaAssetDetail | None:
        with self.session_factory() as session:
            row = session.get(MediaAssetRow, asset_id)
            if row is None:
                return None
            thumbnail_url = self._signed_thumbnail_url(row.thumbnail_uri)
            return MediaAssetDetail(
                asset=media_asset_row_to_contract(row, thumbnail_url=thumbnail_url),
                preview_url=f"local://media/{row.id}",
            )

    def delete_asset(self, asset_id: str) -> bool:
        """Delete the media-asset row (e.g. a retired cover_template). Returns
        ``False`` when the asset does not exist. The backing source artifact/object
        is left in place — artifacts are append-only audit records and may be shared;
        only the asset registration is removed."""
        with self.session_factory() as session:
            row = session.get(MediaAssetRow, asset_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def material_usage_ranking(
        self,
        *,
        kind: SelectionMedium,
        case_id: str | None = None,
        top_n: int = 20,
    ) -> MaterialUsageRankingReport:
        with self.session_factory() as session:
            statement = select(SelectionLedgerRow).where(SelectionLedgerRow.medium == kind)
            if case_id:
                statement = statement.where(SelectionLedgerRow.case_id == case_id)
            rows = list(session.scalars(statement.order_by(SelectionLedgerRow.created_at.desc())))
            entries = [
                SelectionLedgerEntry(
                    id=row.id,
                    case_id=row.case_id,
                    run_id=row.run_id,
                    medium=row.medium,
                    asset_id=row.asset_id,
                    clip_id=row.clip_id,
                    slot_phase=row.slot_phase,
                    diversity_key=row.diversity_key,
                    created_at=row.created_at,
                )
                for row in rows
            ]
            assets = {
                row.id: media_asset_row_to_contract(row)
                for row in session.scalars(
                    select(MediaAssetRow).where(MediaAssetRow.id.in_({entry.asset_id for entry in entries}))
                )
            } if entries else {}
        return material_usage_ranking_from_entries(
            entries=entries,
            assets=assets,
            kind=kind,
            case_id=case_id,
            top_n=top_n,
        )

    def create_asset_from_upload(self, payload: CreateMediaAssetFromUploadRequest) -> MediaAssetRecord:
        with self.session_factory() as session:
            upload = session.get(UploadSessionRow, payload.upload_session_id)
            if upload is None or upload.status != "completed":
                raise NodeExecutionError(ErrorCode.upload_invalid_state, "Upload must be completed first.")
            artifact = session.scalar(
                select(ArtifactRow)
                .where(ArtifactRow.kind == ArtifactKind.uploaded_file.value)
                .where(ArtifactRow.payload.contains({"id": upload.id}))
                .order_by(ArtifactRow.created_at.desc())
                .limit(1)
            )
            if artifact is None:
                raise NodeExecutionError(ErrorCode.artifact_missing, "Completed upload artifact is missing.")
            thumbnails = list(
                session.scalars(
                    select(ArtifactRow)
                    .where(ArtifactRow.kind == ArtifactKind.cover_image.value)
                    .where(ArtifactRow.payload.contains({"source_artifact_id": artifact.id}))
                    .order_by(ArtifactRow.created_at.desc())
                )
            )
            thumbnail = next(
                (
                    row
                    for row in thumbnails
                    if isinstance(row.payload, dict) and row.payload.get("thumbnail_label") == "mid"
                ),
                thumbnails[0] if thumbnails else None,
            )
            media_info = (
                MediaInfo.model_validate(artifact.media_info)
                if isinstance(artifact.media_info, dict)
                else None
            )
            row = MediaAssetRow(
                id=new_id("asset"),
                case_id=payload.case_id,
                title=payload.title,
                kind=payload.kind,
                source_artifact_id=artifact.id,
                tags=payload.tags,
                annotation_status="pending",
                usable=True,
                thumbnail_uri=thumbnail.uri if thumbnail is not None else None,
                duration_sec=media_info.duration_sec if media_info is not None else None,
                width=media_info.width if media_info is not None else None,
                height=media_info.height if media_info is not None else None,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return media_asset_row_to_contract(
                row,
                thumbnail_url=self._signed_thumbnail_url(row.thumbnail_uri),
            )

    def media_source_for_asset(self, asset_id: str) -> tuple[str, MediaInfo | None] | None:
        # Returns the source artifact's (uri, media_info) so the preview endpoint can
        # surface content_type/playable. None => asset missing; ("", None) => asset
        # exists but has no source artifact yet (caller falls back to a local stub).
        with self.session_factory() as session:
            asset = session.get(MediaAssetRow, asset_id)
            if asset is None:
                return None
            if not asset.source_artifact_id:
                return "", None
            artifact = session.get(ArtifactRow, asset.source_artifact_id)
            if artifact is None:
                return "", None
            media_info = (
                MediaInfo.model_validate(artifact.media_info)
                if isinstance(artifact.media_info, dict)
                else None
            )
            return artifact.uri or "", media_info

    def artifact_ref_for_asset(self, asset_id: str) -> ArtifactRef | None:
        with self.session_factory() as session:
            asset = session.get(MediaAssetRow, asset_id)
            if asset is None or not asset.source_artifact_id:
                return None
            artifact = session.get(ArtifactRow, asset.source_artifact_id)
            return artifact_ref_from_row(artifact) if artifact is not None else None

    def replace_asset_source_artifact(
        self, asset_id: str, *, kind: ArtifactKind, uri: str, size_bytes: int, sha256: str, media_info: MediaInfo, payload: dict, tag: str | None = None
    ) -> ArtifactRef | None:
        with self.session_factory() as session:
            asset = session.get(MediaAssetRow, asset_id)
            if asset is None:
                return None
            artifact = ArtifactRow(
                id=new_id("art"), kind=kind.value, uri=uri, size_bytes=size_bytes, sha256=sha256,
                media_info=media_info.model_dump(mode="json"),
                payload_schema="ProcessedMediaArtifact.v1",
                payload=payload,
            )
            artifact_ref = ArtifactRef(artifact_id=artifact.id, kind=kind, uri=uri, sha256=sha256)
            session.add(artifact)
            session.flush()
            asset.source_artifact_id = artifact.id
            tags = list(asset.tags or [])
            if tag and tag not in tags:
                tags.append(tag)
            asset.tags = tags
            asset.updated_at = utcnow()
            session.commit()
            return artifact_ref

    def get_or_create_annotation(self, asset_id: str) -> AnnotationEditorVm | None:
        with self.session_factory() as session:
            asset = session.get(MediaAssetRow, asset_id)
            if asset is None:
                return None
            row = self._annotation_row(session, asset_id)
            if row is None:
                row = AnnotationRow(
                    id=new_id("ann"),
                    asset_id=asset_id,
                    etag=new_id("etag"),
                    canonical_schema="MediaAnnotationCanonical.v1",
                    canonical={"labels": list(asset.tags or []), "kind": asset.kind},
                    projection_schema="MediaAnnotationProjection.v1",
                    projection={"title": asset.title, "usable": asset.usable},
                    editable_paths=["/labels", "/usable", "/title"],
                )
                session.add(row)
                session.commit()
                session.refresh(row)
                session.refresh(asset)
            return annotation_row_to_editor(row, asset, object_store=self.object_store)

    def patch_annotation(self, asset_id: str, payload: PatchAnnotationRequest) -> AnnotationEditorVm | None:
        # Import here to avoid an apps.api <-> packages.media import cycle at module load.
        from apps.api.services.annotation_patch import apply_patch

        with self.session_factory() as session:
            asset = session.get(MediaAssetRow, asset_id)
            if asset is None:
                return None
            row = self._annotation_row(session, asset_id)
            if row is None:
                row = AnnotationRow(
                    id=new_id("ann"),
                    asset_id=asset_id,
                    etag=new_id("etag"),
                    canonical_schema="MediaAnnotationCanonical.v1",
                    canonical={"labels": list(asset.tags or []), "kind": asset.kind},
                    projection_schema="MediaAnnotationProjection.v1",
                    projection={"title": asset.title, "usable": asset.usable},
                    editable_paths=["/labels", "/usable", "/title"],
                )
                session.add(row)
                session.flush()
            # PatchService merges structural edits (segments / quality_events) into the
            # canonical AnnotationV4 -> new canonical version, then rebuilds the projection
            # from canonical (Spec §12.2). Invalid edits raise artifact.schema_mismatch (400).
            canonical, projection = apply_patch(
                canonical=dict(row.canonical or {}),
                projection=dict(row.projection or {}),
                asset=media_asset_row_to_contract(asset),
                operations=payload.patch.operations,
            )
            if asset_record_is_v4(canonical):
                row.canonical_schema = "AnnotationV4.v1"
            row.canonical = canonical
            row.projection = projection
            row.etag = new_id("etag")
            row.updated_at = utcnow()
            asset.annotation_status = "annotated"
            asset.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            session.refresh(asset)
            return annotation_row_to_editor(row, asset, object_store=self.object_store)

    def rerun_annotation(self, asset_id: str, payload: RerunAnnotationRequest) -> AnnotationRunResponse | None:
        with self.session_factory() as session:
            asset = session.get(MediaAssetRow, asset_id)
            if asset is None:
                return None
            row = self._annotation_row(session, asset_id)
            if row is None:
                row = AnnotationRow(
                    id=new_id("ann"),
                    asset_id=asset_id,
                    etag=new_id("etag"),
                    canonical_schema="MediaAnnotationCanonical.v1",
                    canonical={"labels": list(asset.tags or []), "kind": asset.kind},
                    projection_schema="MediaAnnotationProjection.v1",
                    projection={"title": asset.title, "usable": asset.usable},
                    editable_paths=["/labels", "/usable", "/title"],
                )
                session.add(row)
            asset.annotation_status = "annotated"
            asset.updated_at = utcnow()
            session.commit()
            return AnnotationRunResponse(asset_id=asset_id, run_id=None, status="completed")

    def asset_record(self, asset_id: str) -> MediaAssetRecord | None:
        with self.session_factory() as session:
            row = session.get(MediaAssetRow, asset_id)
            return media_asset_row_to_contract(row) if row is not None else None

    def set_annotation_canonical(self, asset_id: str, canonical: dict) -> None:
        """Overwrite the latest annotation's canonical (replace-source re-clip)."""
        with self.session_factory() as session:
            row = self._annotation_row(session, asset_id)
            if row is None:
                return
            row.canonical = canonical
            if asset_record_is_v4(canonical):
                row.canonical_schema = "AnnotationV4.v1"
            row.etag = new_id("etag")
            row.updated_at = utcnow()
            session.commit()

    def invalidate_annotation(self, asset_id: str) -> None:
        """Drop the annotation + mark the asset pending (re-annotation required)."""
        with self.session_factory() as session:
            asset = session.get(MediaAssetRow, asset_id)
            row = self._annotation_row(session, asset_id)
            if row is not None:
                session.delete(row)
            if asset is not None:
                asset.annotation_status = "pending"
                asset.usable = False
                asset.updated_at = utcnow()
            session.commit()

    def asset_source_duration(self, asset_id: str) -> float:
        """Best-effort source duration (sec) from the asset row / source artifact media_info."""
        with self.session_factory() as session:
            asset = session.get(MediaAssetRow, asset_id)
            if asset is None:
                return 0.0
            if asset.duration_sec:
                try:
                    return max(0.0, float(asset.duration_sec))
                except (TypeError, ValueError):
                    pass
            if not asset.source_artifact_id:
                return 0.0
            artifact = session.get(ArtifactRow, asset.source_artifact_id)
            media_info = artifact.media_info if artifact is not None else None
            duration = media_info.get("duration_sec") if isinstance(media_info, dict) else None
            try:
                return max(0.0, float(duration)) if duration is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

    def persist_annotation_v4(
        self,
        asset_id: str,
        *,
        canonical: dict,
        projection: dict,
        annotation_status: str,
        usable: bool,
        case_id: str | None = None,
    ) -> AnnotationEditorVm | None:
        """Write a fresh AnnotationV4 canonical + projection + artifact for one asset.

        Mirrors the in-memory ``asset_annotation._persist``: the AnnotationV4 canonical
        is the single source of truth (Spec §12.2) so material planning reads it via
        ``annotation_v4_for_asset``; the projection is rebuilt from canonical. A
        ``material_annotation`` ArtifactRow (schema ``AnnotationV4.v1``) is recorded too.
        Returns ``None`` when the asset is missing.
        """
        with self.session_factory() as session:
            asset = session.get(MediaAssetRow, asset_id)
            if asset is None:
                return None
            artifact = ArtifactRow(
                id=new_id("art"),
                case_id=case_id or asset.case_id,
                kind=ArtifactKind.material_annotation.value,
                uri=None,
                payload_schema="AnnotationV4.v1",
                payload=canonical,
            )
            session.add(artifact)
            session.flush()
            projection = dict(projection)
            projection.setdefault("annotation_artifact_id", artifact.id)
            row = self._annotation_row(session, asset_id)
            if row is None:
                row = AnnotationRow(
                    id=new_id("ann"),
                    asset_id=asset_id,
                    etag=new_id("etag"),
                    canonical_schema="AnnotationV4.v1",
                    canonical=canonical,
                    projection_schema="MediaAnnotationProjection.v1",
                    projection=projection,
                    editable_paths=["/labels", "/usable", "/title"],
                )
                session.add(row)
            else:
                row.canonical_schema = "AnnotationV4.v1"
                row.canonical = canonical
                row.projection = projection
                row.etag = new_id("etag")
                row.updated_at = utcnow()
            asset.annotation_status = annotation_status
            asset.usable = usable
            asset.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            session.refresh(asset)
            return annotation_row_to_editor(row, asset, object_store=self.object_store)

    def _annotation_row(self, session: Session, asset_id: str) -> AnnotationRow | None:
        return session.scalar(
            select(AnnotationRow)
            .where(AnnotationRow.asset_id == asset_id)
            .order_by(AnnotationRow.updated_at.desc())
            .limit(1)
        )

    def list_voices(
        self,
        *,
        source: str | None = None,
        enabled: bool | None = None,
        limit: int = 50,
    ) -> list[VoiceProfile]:
        with self.session_factory() as session:
            statement = select(VoiceProfileRow)
            if source:
                statement = statement.where(VoiceProfileRow.source == source)
            if enabled is not None:
                statement = statement.where(VoiceProfileRow.enabled == enabled)
            statement = statement.order_by(VoiceProfileRow.updated_at.desc()).limit(limit)
            return [voice_row_to_contract(row) for row in session.scalars(statement)]

    def clone_voice(self, payload: CloneVoiceRequest) -> VoiceProfile:
        with self.session_factory() as session:
            upload = session.get(UploadSessionRow, payload.reference_upload_session_id)
            if upload is None or upload.status != "completed":
                raise NodeExecutionError(ErrorCode.upload_invalid_state, "Reference upload must be completed first.")
            row = VoiceProfileRow(
                id=new_id("voice"),
                display_name=payload.display_name,
                source="cloned",
                provider_profile_id=payload.provider_profile_id or "sandbox.tts.default",
                enabled=True,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return voice_row_to_contract(row)

    def design_voice(self, payload: DesignVoiceRequest) -> VoiceProfile:
        with self.session_factory() as session:
            row = VoiceProfileRow(
                id=new_id("voice"),
                display_name=payload.display_name,
                source="designed",
                provider_profile_id=payload.provider_profile_id or "sandbox.tts.default",
                enabled=True,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return voice_row_to_contract(row)

    def upsert_voice(
        self,
        *,
        voice_id: str,
        display_name: str,
        source: str,
        provider_profile_id: str,
    ) -> tuple[VoiceProfile, bool]:
        """Insert a provider-side voice keyed by its external voice_id.

        Returns (voice, created). On an existing row only the display name is
        refreshed (when the provider now reports one) so a re-sync stays idempotent.
        """
        with self.session_factory() as session:
            row = session.get(VoiceProfileRow, voice_id)
            created = row is None
            if row is None:
                row = VoiceProfileRow(
                    id=voice_id,
                    display_name=display_name,
                    source=source,
                    provider_profile_id=provider_profile_id,
                    enabled=True,
                )
                session.add(row)
            elif display_name and display_name != row.display_name:
                row.display_name = display_name
                row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return voice_row_to_contract(row), created

    def preview_voice(self, voice_id: str, payload: VoicePreviewRequest) -> VoicePreviewResponse | None:
        with self.session_factory() as session:
            voice = session.get(VoiceProfileRow, voice_id)
            if voice is None:
                return None
            artifact = ArtifactRow(
                id=new_id("art"),
                kind=ArtifactKind.audio_tts.value,
                uri=f"sandbox://voice-preview/{voice_id}.wav",
                payload_schema="VoicePreviewArtifact.v1",
                payload={
                    "voice_id": voice_id,
                    "text": payload.text,
                    "provider_profile_id": payload.provider_profile_id,
                },
            )
            session.add(artifact)
            session.flush()
            voice.preview_artifact_id = artifact.id
            voice.updated_at = utcnow()
            session.commit()
            session.refresh(artifact)
            return VoicePreviewResponse(
                voice_id=voice_id,
                audio_artifact=artifact_ref_from_row(artifact),
                duration_sec=max(1, len(payload.text) / 6),
            )

    def patch_voice(self, voice_id: str, payload: PatchVoiceRequest) -> VoiceProfile | None:
        with self.session_factory() as session:
            voice = session.get(VoiceProfileRow, voice_id)
            if voice is None:
                return None
            for key, value in payload.model_dump(exclude_none=True).items():
                setattr(voice, key, value)
            voice.updated_at = utcnow()
            session.commit()
            session.refresh(voice)
            return voice_row_to_contract(voice)

    def delete_voice(self, voice_id: str) -> None:
        with self.session_factory() as session:
            voice = session.get(VoiceProfileRow, voice_id)
            if voice is not None:
                session.delete(voice)
                session.commit()
