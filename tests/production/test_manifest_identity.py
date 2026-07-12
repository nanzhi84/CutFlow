"""The input manifest pins artifact IDENTITY, and that is what separates retry from resume.

Both re-drive the same job under the same Job-scoped provider-call key (issue #202). A
RESUME reuses the prefix artifact rows verbatim, so the manifest — and the key — is
unchanged and the paid call is recovered rather than re-bought. A RETRY re-runs the chain
from the top, so its prefix artifacts are NEW rows: the manifest changes, the key changes,
and the vendor is paid again, which is exactly what a retry is for.

That distinction rests entirely on ``artifact_refs`` being artifact ids. Swap them for a
content digest and a deterministic chain's retry would hash identically, land on the old
key, and hand the operator back the very result they asked to recompute. These are pure
functions over a RunState — no storage.
"""

from __future__ import annotations

from packages.core import contracts as c
from packages.production.pipeline._run_state import RunState
from packages.production.pipeline.digital_human import input_manifest_hash


def _request() -> c.DigitalHumanVideoRequest:
    return c.DigitalHumanVideoRequest(
        case_id="case_demo", script="测试脚本。", voice={"voice_id": "voice_sandbox"}
    )


def _artifact(artifact_id: str) -> c.Artifact:
    # Same kind, same bytes, same payload — only the row id differs. This is precisely a
    # deterministic node re-run under a retry.
    return c.Artifact(
        id=artifact_id,
        run_id="run_source",
        kind=c.ArtifactKind.audio_tts,
        uri="s3://bucket/tts.wav",
        sha256="0" * 64,
        payload_schema="uri-only",
    )


def _state(*artifacts: c.Artifact) -> RunState:
    state = RunState(request=_request())
    for artifact in artifacts:
        state.artifacts[artifact.kind] = artifact
    return state


def test_manifest_tracks_artifact_id_not_artifact_content():
    same_content_new_row = input_manifest_hash(
        "LipSync", _request(), _state(_artifact("art_2"))
    )
    original = input_manifest_hash("LipSync", _request(), _state(_artifact("art_1")))

    assert original != same_content_new_row, (
        "artifact_refs collapsed to a content fingerprint: a retry would now hash to the "
        "old manifest, hit the old provider-call key and replay the result the operator "
        "explicitly asked to re-compute"
    )


def test_manifest_is_unchanged_when_the_very_same_artifact_rows_are_reused():
    # The resume side of the same invariant: reuse hands the new run the ORIGINAL rows, so
    # a re-run node recomputes the identical manifest and recovers its durable call.
    artifact = _artifact("art_1")
    assert input_manifest_hash("LipSync", _request(), _state(artifact)) == input_manifest_hash(
        "LipSync", _request(), _state(artifact)
    )


def test_node_id_and_request_are_manifest_coordinates():
    state = _state(_artifact("art_1"))
    baseline = input_manifest_hash("LipSync", _request(), state)
    other_node = input_manifest_hash("TTS", _request(), state)
    other_request = input_manifest_hash(
        "LipSync", _request().model_copy(update={"script": "别的脚本"}), state
    )
    assert baseline != other_node
    assert baseline != other_request
