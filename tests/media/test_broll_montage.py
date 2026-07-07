from __future__ import annotations

from pathlib import Path

from packages.core.contracts import Artifact, ArtifactKind
from packages.media.rendering import render_broll_montage, validate_rendered_output
from packages.media.video.ffmpeg import probe_stream_types


def _artifact(asset_id: str, path: Path) -> Artifact:
    return Artifact(
        id=f"art_{asset_id}",
        kind=ArtifactKind.uploaded_file,
        local_path=str(path),
        payload_schema="UploadedFileArtifact.v1",
        payload=None,
    )


def test_render_broll_montage_outputs_exact_frame_count_and_shape(
    tmp_path,
    media_fixture_factory,
):
    source_a = media_fixture_factory.video(
        duration_sec=2.0,
        width=320,
        height=180,
        fps=24,
        filename="broll_montage_a.mp4",
    )
    source_b = media_fixture_factory.video(
        duration_sec=2.5,
        width=180,
        height=320,
        fps=24,
        filename="broll_montage_b.mp4",
    )
    artifacts = {
        "asset_a": _artifact("asset_a", source_a),
        "asset_b": _artifact("asset_b", source_b),
    }
    output_path = tmp_path / "montage.mp4"
    total_frames = 72

    render_broll_montage(
        segments=[
            {
                "asset_id": "asset_a",
                "source_start": 0.0,
                "source_end": 1.25,
                "timeline_start": 0.0,
                "timeline_end": 1.25,
            },
            {
                "asset_id": "asset_b",
                "source_start": 0.25,
                "source_end": 2.0,
                "timeline_start": 1.25,
                "timeline_end": 3.0,
            },
        ],
        output_path=output_path,
        total_frames=total_frames,
        width=160,
        height=90,
        fps=24,
        source_artifact_for_asset=lambda asset_id: artifacts[asset_id],
        artifact_path=lambda artifact: Path(artifact.local_path),
    )

    validate_rendered_output(
        output_path,
        expected_frames=total_frames,
        expected_width=160,
        expected_height=90,
        expected_fps=24,
    )
    assert probe_stream_types(output_path) == {"video"}


def test_render_broll_montage_pads_non_frame_aligned_segments_to_exact_frames(
    tmp_path,
    media_fixture_factory,
):
    # Real TTS narration durations are almost never frame-aligned. Each segment is
    # fps-resampled then concatenated; sub-frame tails floor away, so the raw concat
    # can land one frame short of total_frames = round(duration * fps). The final
    # trim/-frames:v can only truncate, never pad, so without an exact-frame backstop
    # the montage renders short and validate_rendered_output hard-fails with a
    # misleading render_invalid_timeline. Durations below reproduce that shortfall:
    # 3.256 + 3.130 + 3.430 = 9.816s @24fps -> 236 frames, raw concat yields 235.
    sources = {
        f"asset_{i}": media_fixture_factory.video(
            duration_sec=4.0,
            width=320,
            height=180,
            fps=30,
            filename=f"broll_pad_{i}.mp4",
        )
        for i in range(3)
    }
    artifacts = {asset_id: _artifact(asset_id, path) for asset_id, path in sources.items()}

    fps = 24
    durations = [3.256, 3.130, 3.430]
    total_frames = round(sum(durations) * fps)  # 236
    segments = []
    cursor = 0.0
    for index, duration in enumerate(durations):
        segments.append(
            {
                "asset_id": f"asset_{index}",
                "source_start": 0.0,
                "source_end": round(duration, 3),
                "timeline_start": round(cursor, 3),
                "timeline_end": round(cursor + duration, 3),
            }
        )
        cursor += duration

    output_path = tmp_path / "montage_non_aligned.mp4"
    render_broll_montage(
        segments=segments,
        output_path=output_path,
        total_frames=total_frames,
        width=160,
        height=90,
        fps=fps,
        source_artifact_for_asset=lambda asset_id: artifacts[asset_id],
        artifact_path=lambda artifact: Path(artifact.local_path),
    )

    # Exact frame count, or the B-roll montage validation would raise.
    validate_rendered_output(
        output_path,
        expected_frames=total_frames,
        expected_width=160,
        expected_height=90,
        expected_fps=fps,
    )
