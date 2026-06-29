from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core import contracts as c
from packages.core.storage.database import (
    CaseRow,
    PublishBatchItemRow,
    PublishBatchRow,
    PublishPackageRow,
)
from packages.core.storage.repository import new_id


def _login_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _publish_batch_for_case(app, case_id: str) -> str:
    """Persist a package + single-item batch for ``case_id`` in Postgres (the only
    storage backend) and return the batch id. ``list_batches(case_id=...)`` resolves
    the case via the item -> package.case_id join."""
    package_id = new_id("pkg")
    batch_id = new_id("pub_batch")
    item_id = new_id("pub_item")
    title = f"{case_id} package"
    with app.state.sqlalchemy_session_factory() as session:
        if session.get(CaseRow, case_id) is None:
            session.add(CaseRow(id=case_id, name=case_id, owner_user_id="usr_admin", status="active"))
        session.add(
            PublishPackageRow(
                id=package_id,
                case_id=case_id,
                video_artifact=c.ArtifactRef(
                    artifact_id=new_id("art"),
                    kind=c.ArtifactKind.video_final,
                    uri=f"local://cutagent-local/{case_id}/final.mp4",
                    schema_version="artifact.ref.v1",
                ).model_dump(mode="json"),
                platform_defaults=c.PublishDefaults(title=title).model_dump(mode="json"),
            )
        )
        session.add(PublishBatchRow(id=batch_id, status="draft"))
        session.flush()
        session.add(
            PublishBatchItemRow(
                id=item_id,
                batch_id=batch_id,
                publish_package_id=package_id,
                platform="douyin",
                title=title,
                status="uploaded",
            )
        )
        session.commit()
    return batch_id


def test_publish_batches_query_filters_by_case_id() -> None:
    app = create_app()
    with TestClient(app) as client:
        _login_admin(client)
        demo_batch_id = _publish_batch_for_case(app, "case_demo")
        other_batch_id = _publish_batch_for_case(app, "case_other")

        all_batches = client.get("/api/publish/batches")
        assert all_batches.status_code == 200, all_batches.text
        assert {item["id"] for item in all_batches.json()["items"]} >= {
            demo_batch_id,
            other_batch_id,
        }

        filtered = client.get("/api/publish/batches", params={"case_id": "case_demo"})
        assert filtered.status_code == 200, filtered.text

    assert [item["id"] for item in filtered.json()["items"]] == [demo_batch_id]
