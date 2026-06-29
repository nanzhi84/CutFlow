"""G3: publish-attempt and publish-package funnel emission helpers.

Covers the funnel-write helpers in ``apps.api.services.publishing`` directly
against the in-memory ``Repository`` (no FastAPI ``Request`` / DB wiring needed).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.services.publishing import (
    _publish_run_ids,
    _record_publish_attempt_funnel,
)
import packages.publishing.platform_adapter as platform_adapter
from packages.core.contracts import (
    ArtifactKind,
    ArtifactRef,
    CreatePublishBatchRequest,
    FinishedVideo,
    PublishAttempt,
    PublishAttemptStatus,
    PublishBatchItemVm,
    PublishBatchVm,
    PublishDefaults,
)
from packages.core.storage.database import PublishPackageRow
from packages.core.storage.repository import Repository, new_id
from packages.publishing.platform_adapter import PublishOutcome, PublishPayload


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _seed_finished_video_and_package(repo: Repository) -> tuple[str, str]:
    video_artifact = ArtifactRef(
        artifact_id="art_video",
        kind=ArtifactKind.video_finished,
        uri="local://x/video.mp4",
    )
    finished = FinishedVideo(
        id="fv_1",
        case_id="case_demo",
        run_id="run_1",
        title="Funnel video",
        video_artifact=video_artifact,
    )
    repo.finished_videos[finished.id] = finished
    package = repo.create_publish_package_from_finished_video(finished, title="pkg")
    return finished.id, package.id


def test_publish_run_ids_resolves_run_and_job_from_package():
    repo = Repository()
    # Seed a run so job_id can be resolved off the finished video's run.
    finished_id, package_id = _seed_finished_video_and_package(repo)
    # No run row -> job_id is None but run_id still flows from the finished video.
    run_id, job_id = _publish_run_ids(repo, package_id)
    assert run_id == "run_1"
    assert job_id is None


def test_publish_run_ids_handles_detached_package():
    repo = Repository()
    assert _publish_run_ids(repo, None) == (None, None)
    assert _publish_run_ids(repo, "missing_pkg") == (None, None)


def _attempt(status: PublishAttemptStatus, package_id: str) -> tuple[PublishBatchVm, PublishBatchItemVm, PublishAttempt]:
    item = PublishBatchItemVm(id="item_1", publish_package_id=package_id, platform="douyin", title="t")
    batch = PublishBatchVm(id="batch_1", items=[item])
    attempt = PublishAttempt(
        id="att_1",
        batch_id=batch.id,
        item_id=item.id,
        platforms=["douyin"],
        status=status,
        adapter_id="sandbox.publish",
    )
    return batch, item, attempt


def test_record_attempt_funnel_emits_publish_started_and_published():
    repo = Repository()
    _, package_id = _seed_finished_video_and_package(repo)
    batch, item, attempt = _attempt(PublishAttemptStatus.published, package_id)
    _record_publish_attempt_funnel(repo, batch, item, attempt)
    types = {e.event_type for e in repo.yield_events.values()}
    # §9.5 spec strings: publish_started -> published (true-yield success).
    assert "publish_started" in types
    assert "published" in types
    assert "publish_failed" not in types
    # ``published`` must carry run linkage so it is run-scoped for true yield.
    published = next(e for e in repo.yield_events.values() if e.event_type == "published")
    assert published.run_id == "run_1"


def test_record_attempt_funnel_emits_publish_failed():
    repo = Repository()
    _, package_id = _seed_finished_video_and_package(repo)
    batch, item, attempt = _attempt(PublishAttemptStatus.failed, package_id)
    _record_publish_attempt_funnel(repo, batch, item, attempt)
    types = {e.event_type for e in repo.yield_events.values()}
    assert "publish_started" in types
    assert "publish_failed" in types
    assert "published" not in types


def test_record_attempt_funnel_dry_run_only_publish_started():
    repo = Repository()
    _, package_id = _seed_finished_video_and_package(repo)
    batch, item, attempt = _attempt(PublishAttemptStatus.manual_review_ready, package_id)
    _record_publish_attempt_funnel(repo, batch, item, attempt)
    types = {e.event_type for e in repo.yield_events.values()}
    assert types == {"publish_started"}


def test_qc_run_ids_returns_none_for_missing_run():
    # A quality-check posted against a run that is not in the repo must NOT
    # fabricate a run_id: doing so adds a phantom run to the true-yield denominator
    # (deflating the rate). The "run absent" branch must resolve to (None, None).
    from apps.api.services.ops import _qc_run_ids

    repo = Repository()
    run_id, job_id = _qc_run_ids(repo, target_type="run", target_id="run_missing")
    assert run_id is None
    assert job_id is None


def test_submit_publish_batch_records_per_account_results(monkeypatch):
    published_payloads: list[PublishPayload] = []

    class FakePublishAdapter:
        adapter_id = "fake.publish"

        def probe_accounts(self, *, account_group=None, case_name=None):
            return [], True, None

        def publish(self, payload: PublishPayload) -> PublishOutcome:
            published_payloads.append(payload)
            return PublishOutcome(
                success=True,
                adapter_id=self.adapter_id,
                external_task_id=f"task-{payload.account_id}",
            )

    monkeypatch.setitem(platform_adapter._PUBLISH_ADAPTERS, "fake.publish", FakePublishAdapter)

    with TestClient(create_app()) as client:
        _login(client)
        object_ref = client.app.state.object_store.prepare_upload("video.mp4", "publish-test")
        client.app.state.object_store.put_bytes(object_ref, b"video")

        # Persist the package (case_demo) + its video artifact ref in Postgres so the
        # submit fanout downloads the video and resolves the case's publish targets.
        package_id = new_id("pkg")
        with client.app.state.sqlalchemy_session_factory() as session:
            session.add(
                PublishPackageRow(
                    id=package_id,
                    case_id="case_demo",
                    video_artifact=ArtifactRef(
                        artifact_id=new_id("art"),
                        kind=ArtifactKind.video_finished,
                        uri=object_ref.uri,
                    ).model_dump(mode="json"),
                    platform_defaults=PublishDefaults(
                        title="Publish me", description=""
                    ).model_dump(mode="json"),
                )
            )
            session.commit()

        accounts = client.app.state.sqlalchemy_accounts_repository
        customer = accounts.create_client(name="ACME")
        first = accounts.create_account(
            client_id=customer.id,
            platform="douyin",
            account_name="first",
        )
        second = accounts.create_account(
            client_id=customer.id,
            platform="douyin",
            account_name="second",
        )
        accounts.set_targets("case_demo", [first.id, second.id])

        batch = client.app.state.sqlalchemy_publishing_repository.create_batch(
            CreatePublishBatchRequest(publish_package_ids=[package_id], platform_targets=["douyin"])
        )

        submitted = client.post(
            f"/api/publish/batches/{batch.id}/submit",
            json={"dry_run": False, "adapter_id": "fake.publish"},
        )
        assert submitted.status_code == 202, submitted.text
        attempts = client.app.state.sqlalchemy_publishing_repository.list_attempts(batch.id)

    assert [payload.account_id for payload in published_payloads] == [first.id, second.id]
    results = attempts[0].results
    account_results = [result for result in results if "account_id" in result]
    assert account_results == [
        {
            "account_id": first.id,
            "account_name": "first",
            "success": True,
            "external_task_id": f"task-{first.id}",
            "error": None,
        },
        {
            "account_id": second.id,
            "account_name": "second",
            "success": True,
            "external_task_id": f"task-{second.id}",
            "error": None,
        },
    ]
