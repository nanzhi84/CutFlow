from __future__ import annotations

from datetime import timedelta

import pytest

from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    MediaAssetRecord,
    NodeRun,
    NodeStatus,
    RunStatus,
    SelectionLedgerEntry,
    WorkflowRun,
    utcnow,
)
from packages.core.storage.repository import Repository
from packages.production.pipeline import digital_human
from packages.production.pipeline.digital_human import LocalRuntimeAdapter, RunState


class NoopDeleteStore:
    def delete(self, uri: str) -> None:
        return None


def _artifact(kind: ArtifactKind, payload: dict) -> Artifact:
    return Artifact(
        id=f"art_{kind.value.replace('.', '_')}",
        kind=kind,
        run_id="run_ledger",
        payload_schema=f"{kind.value}.v1",
        payload=payload,
    )


def _workflow(repository: Repository) -> LocalRuntimeAdapter:
    workflow = object.__new__(LocalRuntimeAdapter)
    workflow.repository = repository
    return workflow


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_ledger",
        job_id="job_ledger",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run(run_id: str) -> NodeRun:
    return NodeRun(
        id="nr_finalize",
        run_id=run_id,
        node_id="FinalizeRunReport",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def test_finalize_success_records_selected_assets_once(monkeypatch: pytest.MonkeyPatch):
    repository = Repository()
    workflow = _workflow(repository)
    run = _run()
    node_run = _node_run(run.id)
    repository.runs[run.id] = run
    repository.node_runs[run.id] = [node_run]
    state = RunState(
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="hello",
            voice={"voice_id": "voice_sandbox"},
        ),
        artifacts={
            ArtifactKind.plan_portrait: _artifact(
                ArtifactKind.plan_portrait,
                {"asset_id": "asset_portrait_demo", "segments": []},
            ),
            ArtifactKind.plan_broll: _artifact(
                ArtifactKind.plan_broll,
                {
                    "enabled": True,
                    "segments": [],
                    "overlays": [
                        {
                            "overlay_id": "broll_1",
                            "asset_id": "asset_broll_demo",
                            "timeline_start": 0,
                            "timeline_end": 2,
                        },
                        {
                            "overlay_id": "broll_2",
                            "asset_id": "asset_broll_demo",
                            "timeline_start": 3,
                            "timeline_end": 5,
                        },
                    ],
                },
            ),
            ArtifactKind.plan_style: _artifact(
                ArtifactKind.plan_style,
                {
                    "bgm_asset_id": "asset_bgm_demo",
                    "font_asset_id": "asset_font_demo",
                },
            ),
        },
    )
    monkeypatch.setattr(digital_human, "get_object_store", lambda: NoopDeleteStore())

    workflow._finalize_run_report(run, node_run, state)
    workflow._finalize_run_report(run, node_run, state)

    entries = sorted(
        repository.selection_ledger.values(),
        key=lambda entry: (entry.medium, entry.slot_phase, entry.asset_id),
    )
    assert [(entry.medium, entry.asset_id, entry.slot_phase) for entry in entries] == [
        ("bgm", "asset_bgm_demo", "bgm"),
        ("broll", "asset_broll_demo", "broll_1"),
        ("broll", "asset_broll_demo", "broll_2"),
        ("font", "asset_font_demo", "font"),
        ("portrait", "asset_portrait_demo", "portrait_main"),
    ]
    assert {entry.run_id for entry in entries} == {run.id}
    assert {entry.case_id for entry in entries} == {"case_demo"}


def test_finalize_records_opening_segment_distinctly_and_commits_reservation(
    monkeypatch: pytest.MonkeyPatch,
):
    repository = Repository()
    workflow = _workflow(repository)
    run = _run()
    node_run = _node_run(run.id)
    repository.runs[run.id] = run
    repository.node_runs[run.id] = [node_run]
    # Planning reserved the portrait shortlist; finalize must commit the shipped pick
    # and release the rest (§6.6 commit -> release).
    repository.reserve_selections(
        case_id="case_demo",
        run_id=run.id,
        medium="portrait",
        asset_ids=["asset_portrait_demo", "asset_portrait_alt"],
    )
    state = RunState(
        request=DigitalHumanVideoRequest(
            case_id="case_demo",
            script="hello",
            voice={"voice_id": "voice_sandbox"},
        ),
        artifacts={
            ArtifactKind.plan_portrait: _artifact(
                ArtifactKind.plan_portrait,
                {
                    "asset_id": "asset_portrait_demo",
                    "segments": [
                        {"asset_id": "asset_portrait_demo", "slot_phase": "portrait_opening"},
                        {"asset_id": "asset_portrait_demo", "slot_phase": "portrait_main"},
                    ],
                },
            ),
        },
    )
    monkeypatch.setattr(digital_human, "get_object_store", lambda: NoopDeleteStore())

    workflow._finalize_run_report(run, node_run, state)

    portrait_entries = sorted(
        (e for e in repository.selection_ledger.values() if e.medium == "portrait"),
        key=lambda e: e.slot_phase,
    )
    # The opening segment is recorded distinctly so the next run's opening guard sees it.
    assert {e.slot_phase for e in portrait_entries} == {"portrait_main", "portrait_opening"}
    # The shipped pick is committed; the other shortlist member is released.
    reservations = {
        (r.asset_id): r.status for r in repository.selection_reservations.values()
    }
    assert reservations["asset_portrait_demo"] == "committed"
    assert reservations["asset_portrait_alt"] == "released"


def test_usage_ranking_aggregates_distinct_runs_and_recent_score():
    repository = Repository()
    repository.media_assets["asset_broll_alt"] = MediaAssetRecord(
        id="asset_broll_alt",
        case_id="case_demo",
        title="Alternate b-roll",
        kind="broll",
        tags=["alt"],
        annotation_status="annotated",
        usable=True,
    )
    old = utcnow() - timedelta(days=2)
    recent = utcnow()
    repository.record_selection_ledger_entries(
        [
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_old",
                medium="broll",
                asset_id="asset_broll_demo",
                slot_phase="broll_1",
                created_at=old,
            ),
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_old",
                medium="broll",
                asset_id="asset_broll_demo",
                slot_phase="broll_2",
                created_at=old,
            ),
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_old",
                medium="broll",
                asset_id="asset_broll_alt",
                slot_phase="broll_3",
                created_at=old,
            ),
            SelectionLedgerEntry(
                case_id="case_demo",
                run_id="run_recent",
                medium="broll",
                asset_id="asset_broll_demo",
                slot_phase="broll_1",
                created_at=recent,
            ),
        ]
    )

    report = repository.material_usage_ranking(kind="broll", case_id="case_demo", top_n=10)

    assert [item.asset_id for item in report.items] == ["asset_broll_demo", "asset_broll_alt"]
    top = report.items[0]
    assert top.task_use_count == 2
    assert top.segment_use_count == 3
    assert top.recent_score == 1.5
    assert top.last_used_at == recent
    assert top.asset and top.asset.title == "Demo b-roll clip"
    assert report.items[1].task_use_count == 1
    assert report.items[1].segment_use_count == 1
    assert report.items[1].recent_score == 0.5
