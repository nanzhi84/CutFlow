"""Publishing Cover Node (§2.1 must-retain / §28.3 generate-cover + preview-cover-frame).

Generates a publish cover for a publish item:

- ``generate_publish_cover``: AI cover when requested + an image provider is armed
  (reusing ``packages.media.cover.build_cover_prompt`` + an injected ``ai_cover``
  port), otherwise an honest frame-based cover (§2.2 ``cover.frame_fallback``).
- ``preview_cover_frame``: extract a source frame at a chosen time for operator
  preview (§28.3 preview-cover-frame).

The frame cover/preview download the source video to a temp dir, extract a frame
with ffmpeg, store it via the object store, and register a ``cover.image`` artifact
in the runtime repository. The AI path is provider-agnostic: the service injects
the actual paid call; this module never reaches a real provider on its own and
gracefully falls back to the frame cover when AI is unavailable or fails.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from packages.core.contracts import ArtifactRef, ErrorCode, WarningCode
from packages.core.storage.object_store import ObjectStore, parse_object_uri
from packages.core.workflow import NodeExecutionError
from packages.media.assets import store_file
from packages.media.cover import CoverPromptInputs, build_cover_prompt
from packages.media.video import FfmpegCommandError, extract_frame_at_time


@dataclass(frozen=True)
class CoverArtifact:
    artifact_ref: ArtifactRef
    source: str  # "ai" | "frame"
    frame_fallback: bool = False
    degraded_reason: str | None = None


class AiCoverPort(Protocol):
    """Generate an AI cover from a rendered prompt. Returns an ``ArtifactRef`` on
    success or ``None`` on any provider failure (so the caller falls back to the
    frame cover). Must NOT raise for ordinary provider failures."""

    def __call__(self, *, prompt: str) -> ArtifactRef | None:
        ...


class CoverArtifactWriter(Protocol):
    """Register a stored cover image as an artifact and return its ref."""

    def __call__(self, *, uri: str, sha256: str, case_id: str | None) -> ArtifactRef:
        ...


def _download_video(object_store: ObjectStore, video_uri: str, work_dir: Path) -> Path:
    try:
        ref = parse_object_uri(video_uri)
    except ValueError as exc:
        raise NodeExecutionError(ErrorCode.artifact_missing, f"Unsupported video URI: {video_uri}") from exc
    target = work_dir / "source_video"
    return object_store.download_file(ref, target)


def _extract_and_store_frame(
    object_store: ObjectStore,
    video_path: Path,
    work_dir: Path,
    *,
    frame_time_sec: float,
    purpose: str,
) -> tuple[str, str]:
    frame_output = work_dir / "frame.png"
    try:
        thumbnail = extract_frame_at_time(video_path, frame_output, time_sec=frame_time_sec)
    except FfmpegCommandError as exc:
        raise NodeExecutionError(
            getattr(exc, "error_code", ErrorCode.render_failed),
            "Publish cover frame extraction failed.",
        ) from exc
    stored = store_file(object_store, thumbnail.path, purpose=purpose)
    return stored.ref.uri, stored.sha256


def preview_cover_frame(
    *,
    object_store: ObjectStore,
    video_uri: str,
    frame_time_sec: float,
    write_artifact: CoverArtifactWriter,
    case_id: str | None = None,
) -> ArtifactRef:
    """Extract a source frame at ``frame_time_sec`` for operator preview."""
    with tempfile.TemporaryDirectory(prefix="cutagent-cover-preview-") as directory:
        work_dir = Path(directory)
        video_path = _download_video(object_store, video_uri, work_dir)
        uri, sha256 = _extract_and_store_frame(
            object_store, video_path, work_dir, frame_time_sec=frame_time_sec, purpose="cover-previews"
        )
    return write_artifact(uri=uri, sha256=sha256, case_id=case_id)


def generate_publish_cover(
    *,
    object_store: ObjectStore,
    video_uri: str,
    write_artifact: CoverArtifactWriter,
    mode: str = "ai",
    frame_time_sec: float = 0.0,
    title: str = "",
    description: str = "",
    cover_subtitle: str = "",
    tags: tuple[str, ...] = (),
    case_name: str | None = None,
    case_id: str | None = None,
    ai_cover: AiCoverPort | None = None,
    ai_prompt_template: str | None = None,
) -> CoverArtifact:
    """Generate a publish cover.

    When ``mode='ai'`` and an ``ai_cover`` port is supplied, render the cover
    prompt and try the AI cover; on failure (or when AI is unavailable) fall back
    to a frame cover and flag ``cover.frame_fallback`` (§2.2).
    """
    wants_ai = mode == "ai" and ai_cover is not None
    if wants_ai:
        prompt = build_cover_prompt(
            CoverPromptInputs(
                title=title,
                description=description,
                subtitle=cover_subtitle or None,
                tags=tuple(tags),
                case_name=case_name,
                has_source_frame=True,
            ),
            template=ai_prompt_template,
        )
        ai_ref = ai_cover(prompt=prompt)
        if ai_ref is not None:
            return CoverArtifact(artifact_ref=ai_ref, source="ai")

    # Frame cover (default / fallback). Honest, non-fabricated cover.
    with tempfile.TemporaryDirectory(prefix="cutagent-cover-") as directory:
        work_dir = Path(directory)
        video_path = _download_video(object_store, video_uri, work_dir)
        uri, sha256 = _extract_and_store_frame(
            object_store, video_path, work_dir, frame_time_sec=frame_time_sec, purpose="covers"
        )
    ref = write_artifact(uri=uri, sha256=sha256, case_id=case_id)
    frame_fallback = mode == "ai"
    return CoverArtifact(
        artifact_ref=ref,
        source="frame",
        frame_fallback=frame_fallback,
        degraded_reason=(WarningCode.cover_frame_fallback.value if frame_fallback else None),
    )
