"""G3: publish-batch submit fanout over the SQLAlchemy publishing repository.

Drives ``POST /api/publish/batches/{id}/submit`` against real Postgres with a
fake platform adapter, asserting the batch fans out to every publish target
account and records one result row per account.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app
import packages.publishing.platform_adapter as platform_adapter
from packages.core.contracts import (
    ArtifactKind,
    ArtifactRef,
    CreatePublishBatchRequest,
    PublishDefaults,
)
from packages.core.storage.database import PublishPackageRow
from packages.core.storage.repository import new_id
from packages.publishing.platform_adapter import PublishOutcome, PublishPayload


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


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
