"""End-to-end API test for the case_rubric_v1 loop on the SQL (Postgres) backend.

create case → generate-with-memory (blind prediction) → adopt (reward + prediction
linkage) → manual metrics backfill (settles the blind prediction) → calibration →
pending-retro. Finished-video lineage is seeded directly into Postgres (there is no
public API to mint finished videos here) and the learning reads/writes go through the
SQL case-rubric repository.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.services import case_rubric
from packages.core import contracts as c
from packages.core.storage.database import (
    FinishedVideoRow,
    PublishRecordRow,
    VideoVersionRow,
)


def _login(client) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert resp.status_code == 200, resp.text


def _create_case(client) -> str:
    resp = client.post("/api/cases", json={"name": "Rubric loop case"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _seed_published_lineage(
    client, case_id: str, script_version_id: str, *, days_ago: int
) -> tuple[str, str, str]:
    """Seed FinishedVideo -> VideoVersion -> published PublishRecord into Postgres."""
    finished_id, version_id = _seed_finished_lineage(client, case_id, script_version_id)
    record_id = "pr_rubric"
    with client.app.state.sqlalchemy_session_factory() as session:
        session.add(
            PublishRecordRow(
                id=record_id,
                case_id=case_id,
                video_version_id=version_id,
                platform="douyin",
                status="published",
                published_at=c.utcnow() - timedelta(days=days_ago),
            )
        )
        session.commit()
    return finished_id, version_id, record_id


def _seed_finished_lineage(client, case_id: str, script_version_id: str) -> tuple[str, str]:
    """Persist a FinishedVideo + its VideoVersion lineage directly into Postgres."""
    finished_id = "fv_rubric"
    version_id = "vv_rubric"
    video_artifact = c.ArtifactRef(
        artifact_id="art_video",
        kind=c.ArtifactKind.video_final,
        uri="local://cutagent-local/seed.mp4",
    )
    with client.app.state.sqlalchemy_session_factory() as session:
        session.add(
            FinishedVideoRow(
                id=finished_id,
                case_id=case_id,
                run_id=None,
                owner_user_id=None,
                title="Seeded finished video",
                video_artifact=video_artifact.model_dump(mode="json"),
                duration_sec=12.0,
                qc_status="passed",
            )
        )
        session.flush()
        session.add(
            VideoVersionRow(
                id=version_id,
                case_id=case_id,
                script_version_id=script_version_id,
                finished_video_id=finished_id,
                timeline_plan_artifact_id="art_timeline",
                style_plan_artifact_id="art_style",
            )
        )
        session.commit()
    return finished_id, version_id


def test_case_rubric_loop_end_to_end():
    with TestClient(create_app()) as client:
        _login(client)
        case_id = _create_case(client)

        # Cold-start rubric is available immediately.
        rubric = client.get(f"/api/cases/{case_id}/rubric")
        assert rubric.status_code == 200, rubric.text
        assert rubric.json()["cold_start"] is True
        assert rubric.json()["status"] == "active"

        # Generate a draft → a blind ScorePrediction is produced & queryable.
        gen = client.post(
            f"/api/cases/{case_id}/scripts/generate-with-memory",
            json={"brief": "强痛点开场，三十秒内说清卖点，结尾引导下单。"},
        )
        assert gen.status_code == 202, gen.text
        draft_id = gen.json()["id"]

        predictions = client.get(f"/api/cases/{case_id}/predictions")
        assert predictions.status_code == 200, predictions.text
        items = predictions.json()["items"]
        prediction = next(p for p in items if p["script_draft_id"] == draft_id)
        assert prediction["script_version_id"] is None
        assert prediction["settled_reward"] is None
        assert prediction["band"] in {"top", "ok", "low"}

        # Adopt the draft → a draft_adopted reward + the prediction is linked to the
        # new script version (still blind: composite/band unchanged).
        adopt = client.post(
            f"/api/cases/{case_id}/agent/drafts/{draft_id}/adopt", json={}
        )
        assert adopt.status_code == 201, adopt.text
        script_version_id = adopt.json()["id"]

        rewards = client.app.state.sqlalchemy_case_rubric_repository.list_rewards(case_id)
        assert any(
            r.source_kind == "draft_adopted"
            and r.case_id == case_id
            and r.script_version_id == script_version_id
            for r in rewards
        ), "expected a draft_adopted RewardSignal"

        linked = client.get(f"/api/cases/{case_id}/predictions").json()["items"]
        relinked = next(p for p in linked if p["script_draft_id"] == draft_id)
        assert relinked["script_version_id"] == script_version_id
        # Blind invariant: the locked fields did not change after linkage.
        assert relinked["composite"] == prediction["composite"]
        assert relinked["band"] == prediction["band"]

        # Seed a published finished video (5 days old) tied to the adopted script.
        finished_id, _, _ = _seed_published_lineage(
            client, case_id, script_version_id, days_ago=5
        )

        # Before backfill: it shows up in the pending-retro list (window elapsed,
        # no observation yet) and a published reward is lazily derived on read.
        pending = client.get(f"/api/cases/{case_id}/pending-retro")
        assert pending.status_code == 200, pending.text
        pending_items = pending.json()["items"]
        assert any(item["finished_video_id"] == finished_id for item in pending_items)

        # Backfill raw counts → observation + score, settling the blind prediction.
        backfill = client.post(
            f"/api/cases/{case_id}/finished-videos/{finished_id}/metrics",
            json={
                "window": "7d",
                "platform": "douyin",
                "views": 20000,
                "impressions": 25000,
                "likes": 3000,
                "comments": 200,
                "shares": 100,
            },
        )
        assert backfill.status_code == 202, backfill.text
        observation = backfill.json()
        assert observation["video_version_id"] == "vv_rubric"
        assert observation["like_rate"] is not None

        # The reward sync should have settled the linked prediction (observation is
        # later than the prediction's lock).
        settled = client.get(f"/api/cases/{case_id}/predictions").json()["items"]
        settled_prediction = next(p for p in settled if p["script_draft_id"] == draft_id)
        assert settled_prediction["settled_reward"] is not None
        assert settled_prediction["settled_at"] is not None

        # Calibration reflects the settled sample.
        calibration = client.get(f"/api/cases/{case_id}/rubric/calibration")
        assert calibration.status_code == 200, calibration.text
        report = calibration.json()
        assert report["sample_size"] >= 1
        assert report["case_id"] == case_id

        # After backfill the video has an observation → no longer pending.
        pending_after = client.get(f"/api/cases/{case_id}/pending-retro").json()["items"]
        assert not any(item["finished_video_id"] == finished_id for item in pending_after)

        # A bump proposal endpoint is reachable (one settled sample is below the
        # min-samples gate, so no proposal is forced).
        bump = client.get(f"/api/cases/{case_id}/rubric/bump-proposal")
        assert bump.status_code == 200, bump.text


def _prediction(locked_at):
    return c.ScorePrediction(
        id="pred_blind",
        case_id="case_demo",
        script_version_id="sv_1",
        rubric_version=1,
        composite=8.0,
        band="top",
        locked_at=locked_at,
    )


def _score():
    return c.PerformanceScore(
        id="score_1",
        observation_id="obs_1",
        case_id="case_demo",
        video_version_id="vv_1",
        normalized_score=0.9,
        confidence=0.8,
    )


def _observation(observed_at):
    return c.PerformanceObservation(
        id="obs_1",
        case_id="case_demo",
        publish_record_id="pr_1",
        video_version_id="vv_1",
        metric_name="views",
        metric_value=100,
        observed_at=observed_at,
    )


def test_blind_invariant_settles_only_after_lock():
    now = c.utcnow()
    # Observation AFTER the lock → settle.
    after = case_rubric._settle_prediction_for_score(
        [_prediction(now - timedelta(hours=1))],
        _score(),
        _observation(now),
        "sv_1",
    )
    assert after is not None
    assert after.settled_reward == 0.9
    assert after.settled_at is not None

    # Observation BEFORE the lock → never settle (would break the blind invariant).
    before = case_rubric._settle_prediction_for_score(
        [_prediction(now)],
        _score(),
        _observation(now - timedelta(hours=1)),
        "sv_1",
    )
    assert before is None


def test_calibration_uses_adopted_draft_reward_before_metrics():
    with TestClient(create_app()) as client:
        _login(client)
        case_id = _create_case(client)
        gen = client.post(
            f"/api/cases/{case_id}/scripts/generate-with-memory",
            json={"brief": "痛点开场，结尾引导下单。"},
        )
        assert gen.status_code == 202, gen.text
        draft_id = gen.json()["id"]

        adopt = client.post(f"/api/cases/{case_id}/agent/drafts/{draft_id}/adopt", json={})
        assert adopt.status_code == 201, adopt.text

        calibration = client.get(f"/api/cases/{case_id}/rubric/calibration")
        assert calibration.status_code == 200, calibration.text
        assert calibration.json()["sample_size"] == 1


def test_published_reward_is_idempotent_across_syncs():
    with TestClient(create_app()) as client:
        _login(client)
        case_id = _create_case(client)
        gen = client.post(
            f"/api/cases/{case_id}/scripts/generate-with-memory",
            json={"brief": "痛点开场"},
        )
        draft_id = gen.json()["id"]
        adopt = client.post(f"/api/cases/{case_id}/agent/drafts/{draft_id}/adopt", json={})
        script_version_id = adopt.json()["id"]
        _seed_published_lineage(client, case_id, script_version_id, days_ago=5)

        # Two read paths both run sync_rewards; rewards must not duplicate.
        client.get(f"/api/cases/{case_id}/rubric/calibration")
        client.get(f"/api/cases/{case_id}/pending-retro")
        client.get(f"/api/cases/{case_id}/rubric/calibration")

        rewards = client.app.state.sqlalchemy_case_rubric_repository.list_rewards(case_id)
        published = [r for r in rewards if r.source_kind == "published"]
        produced = [r for r in rewards if r.source_kind == "video_produced"]
        assert len(published) == 1
        assert len(produced) == 1


def test_performance_reward_uses_observation_time_for_blind_gate():
    with TestClient(create_app()) as client:
        _login(client)
        case_id = _create_case(client)
        gen = client.post(
            f"/api/cases/{case_id}/scripts/generate-with-memory",
            json={"brief": "痛点开场"},
        )
        draft_id = gen.json()["id"]
        adopt = client.post(f"/api/cases/{case_id}/agent/drafts/{draft_id}/adopt", json={})
        script_version_id = adopt.json()["id"]

        rubric_repo = client.app.state.sqlalchemy_case_rubric_repository
        prediction = rubric_repo.get_prediction_by_draft(draft_id)
        assert prediction is not None
        old_observed_at = prediction.locked_at - timedelta(hours=1)
        with client.app.state.sqlalchemy_session_factory() as session:
            session.add(
                VideoVersionRow(
                    id="vv_old_metric",
                    case_id=case_id,
                    script_version_id=script_version_id,
                    finished_video_id=None,
                    timeline_plan_artifact_id="art_timeline",
                    style_plan_artifact_id="art_style",
                )
            )
            session.commit()
        rubric_repo.add_performance(
            c.PerformanceObservation(
                id="obs_old_metric",
                case_id=case_id,
                publish_record_id="pr_old_metric",
                video_version_id="vv_old_metric",
                metric_name="views",
                metric_value=1000,
                observed_at=old_observed_at,
            ),
            c.PerformanceScore(
                id="score_old_metric",
                observation_id="obs_old_metric",
                case_id=case_id,
                video_version_id="vv_old_metric",
                normalized_score=0.9,
                confidence=0.8,
            ),
        )

        calibration = client.get(f"/api/cases/{case_id}/rubric/calibration")
        assert calibration.status_code == 200, calibration.text

        performance_rewards = [
            r
            for r in rubric_repo.list_rewards(case_id)
            if r.source_kind == "performance_scored"
        ]
        assert len(performance_rewards) == 1
        assert performance_rewards[0].occurred_at == old_observed_at
        labeled = case_rubric._reward_labeled_predictions(SimpleNamespace(app=client.app), case_id)
        labeled_prediction = next(p for p in labeled if p.id == prediction.id)
        assert labeled_prediction.settled_reward == 0.2


def test_backfill_rejects_missing_finished_video():
    with TestClient(create_app()) as client:
        _login(client)
        case_id = _create_case(client)

        response = client.post(
            f"/api/cases/{case_id}/finished-videos/missing_fv/metrics",
            json={"window": "7d", "views": 100},
        )

        assert response.status_code == 404, response.text
        assert not client.app.state.repository.performance_observations


def test_missing_finished_video_read_endpoints_return_404():
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        _login(client)
        for path in (
            "/api/finished-videos/missing_fv",
            "/api/finished-videos/missing_fv/preview-url",
            "/api/finished-videos/missing_fv/download",
        ):
            response = client.get(path)
            assert response.status_code == 404, response.text


def test_missing_finished_video_delete_returns_404():
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        _login(client)
        response = client.delete("/api/finished-videos/missing_fv")
        assert response.status_code == 404, response.text


def test_backfill_unpublished_finished_video_preserves_lineage():
    with TestClient(create_app()) as client:
        _login(client)
        case_id = _create_case(client)
        gen = client.post(
            f"/api/cases/{case_id}/scripts/generate-with-memory",
            json={"brief": "痛点开场"},
        )
        draft_id = gen.json()["id"]
        adopt = client.post(f"/api/cases/{case_id}/agent/drafts/{draft_id}/adopt", json={})
        script_version_id = adopt.json()["id"]
        finished_id, version_id = _seed_finished_lineage(client, case_id, script_version_id)

        response = client.post(
            f"/api/cases/{case_id}/finished-videos/{finished_id}/metrics",
            json={"window": "7d", "views": 1000, "likes": 50},
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["publish_record_id"] == finished_id
        assert body["video_version_id"] == version_id
