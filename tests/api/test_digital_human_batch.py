"""Server-side batch digital-human-video endpoint (plan Task 5).

Covers:
- N items -> N independent job/run pairs (each succeeds end-to-end on the
  seeded sandbox path).
- Merge precedence ``item.overrides > my defaults > system default`` lands in
  the persisted job request.
- Per-item fault tolerance: a deliberately invalid item is reported ``failed``
  while the rest are ``created``.
- ``> 50`` items -> 422 (contract ``max_length`` guard).
- Every created job has ``created_by == current user``.
- Item-level idempotency: re-submitting the same batch (same ``Idempotency-Key``)
  does not double-create jobs.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core import contracts as c
from packages.core.auth.sqlalchemy_service import hash_session_token
from packages.core.storage.database import SessionRow, UserGenerationDefaultsRow, UserRow
from packages.core.storage.repository import new_id

SESSION_COOKIE = "cutagent_session"


def _make_user(app, *, role: c.UserRole) -> tuple[c.AuthUser, str]:
    """Create a real user + session row in Postgres and return its session token
    (the cookie value). Auth reads the SQL ``users``/``sessions`` tables."""
    user = c.AuthUser(
        id=new_id("usr"),
        email=f"{new_id('u')}@local.test",
        display_name="Batch User",
        role=role,
    )
    token = new_id("sess")
    with app.state.sqlalchemy_session_factory() as session:
        session.add(
            UserRow(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                password_hash="unused-session-auth",
                role=role.value,
                status="active",
            )
        )
        session.flush()
        session.add(
            SessionRow(
                id=hash_session_token(token),
                user_id=user.id,
                expires_at=c.utcnow() + timedelta(days=7),
            )
        )
        session.commit()
    return user, token


def _save_generation_defaults(app, user_id: str, defaults: c.UserGenerationDefaults) -> None:
    """Persist a user's saved generation defaults into Postgres so the batch
    endpoint's ``use_my_defaults`` merge reads them from the SQL backend."""
    with app.state.sqlalchemy_session_factory() as session:
        session.add(
            UserGenerationDefaultsRow(
                id=new_id("ugd"),
                user_id=user_id,
                preset_name="default",
                settings=defaults.model_dump(mode="json"),
            )
        )
        session.commit()


def _cookie(client: TestClient, token: str) -> None:
    client.cookies.set(SESSION_COOKIE, token)


def _item(script: str, **kwargs) -> dict:
    return {"script": script, **kwargs}


def _batch_body(items: list[dict], **kwargs) -> dict:
    body = {"case_id": "case_demo", "items": items}
    body.update(kwargs)
    return body


_VOICE = {"voice_id": "voice_sandbox"}


def test_batch_creates_one_job_run_per_item() -> None:
    app = create_app()
    with TestClient(app) as client:
        user, token = _make_user(app, role=c.UserRole.operator)
        _cookie(client, token)
        resp = client.post(
            "/api/jobs/digital-human-video/batch",
            json=_batch_body(
                [
                    _item("第一条脚本。", overrides={"voice": _VOICE}),
                    _item("第二条脚本。", title="第二", overrides={"voice": _VOICE}),
                    _item("第三条脚本。", overrides={"voice": _VOICE}),
                ],
                use_my_defaults=False,
            ),
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert len(results) == 3
        assert all(r["status"] == "created" for r in results), results
        job_ids = {r["job_id"] for r in results}
        run_ids = {r["run_id"] for r in results}
        assert len(job_ids) == 3
        assert len(run_ids) == 3
        # Each created job is owned by the current user.
        for r in results:
            job = app.state.repository.jobs[r["job_id"]]
            assert job.created_by == user.id


def test_batch_merge_precedence_overrides_over_defaults_over_system() -> None:
    app = create_app()
    with TestClient(app) as client:
        _admin, token = _make_user(app, role=c.UserRole.operator)
        _cookie(client, token)
        # Save my defaults: a custom voice + custom output width.
        _save_generation_defaults(
            app,
            _admin.id,
            c.UserGenerationDefaults(
                voice=c.VoiceOptions(voice_id="voice_my_default", speed=1.5),
                output=c.OutputOptions(width=720, height=1280, fps=24),
            ),
        )
        resp = client.post(
            "/api/jobs/digital-human-video/batch",
            json=_batch_body(
                [
                    # Item 0: no overrides -> uses my defaults.
                    _item("脚本零。"),
                    # Item 1: overrides voice -> wins over my defaults; output still mine.
                    _item(
                        "脚本一。",
                        overrides={"voice": {"voice_id": "voice_item_override"}},
                    ),
                ],
                use_my_defaults=True,
            ),
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert all(r["status"] == "created" for r in results), results

        job0 = app.state.repository.jobs[results[0]["job_id"]]
        job1 = app.state.repository.jobs[results[1]["job_id"]]
        # Item 0: my-default voice + my-default output width.
        assert job0.request.voice.voice_id == "voice_my_default"
        assert job0.request.voice.speed == 1.5
        assert job0.request.output.width == 720
        # Item 1: override voice wins; my-default output still applies.
        assert job1.request.voice.voice_id == "voice_item_override"
        assert job1.request.output.width == 720
        # System default for an unset block stays system default.
        assert job0.request.bgm.enabled is c.BgmOptions().enabled


def test_batch_per_item_fault_tolerance() -> None:
    app = create_app()
    with TestClient(app) as client:
        _user, token = _make_user(app, role=c.UserRole.operator)
        _cookie(client, token)
        resp = client.post(
            "/api/jobs/digital-human-video/batch",
            json=_batch_body(
                [
                    _item("有效脚本一。", overrides={"voice": _VOICE}),
                    # Invalid: an unknown workflow_template_id makes _start_submitted_run
                    # raise while building the template, so this single item fails.
                    _item(
                        "有效脚本三。",
                        overrides={"voice": _VOICE, "workflow_template_id": "no_such_template"},
                    ),
                    _item("有效脚本二。", overrides={"voice": _VOICE}),
                ],
                use_my_defaults=False,
            ),
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert [r["status"] for r in results] == ["created", "failed", "created"]
        assert results[1]["error"]
        assert results[1]["job_id"] is None
        # The failed item leaves no orphan job: only the two created ones exist.
        assert len(app.state.repository.jobs) == 2


def test_batch_over_limit_returns_422() -> None:
    app = create_app()
    with TestClient(app) as client:
        _user, token = _make_user(app, role=c.UserRole.operator)
        _cookie(client, token)
        resp = client.post(
            "/api/jobs/digital-human-video/batch",
            json=_batch_body([_item(f"脚本{i}。") for i in range(51)], use_my_defaults=False),
        )
        assert resp.status_code == 422, resp.text


def test_batch_item_idempotency_no_double_create() -> None:
    app = create_app()
    with TestClient(app) as client:
        _user, token = _make_user(app, role=c.UserRole.operator)
        _cookie(client, token)
        body = _batch_body(
            [
                _item("幂等脚本一。", overrides={"voice": _VOICE}),
                _item("幂等脚本二。", overrides={"voice": _VOICE}),
            ],
            use_my_defaults=False,
        )
        first = client.post(
            "/api/jobs/digital-human-video/batch",
            json=body,
            headers={"Idempotency-Key": "batch-idem-1"},
        )
        assert first.status_code == 200, first.text
        first_jobs = {r["job_id"] for r in first.json()["results"]}

        jobs_after_first = len(app.state.repository.jobs)

        second = client.post(
            "/api/jobs/digital-human-video/batch",
            json=body,
            headers={"Idempotency-Key": "batch-idem-1"},
        )
        assert second.status_code == 200, second.text
        second_jobs = {r["job_id"] for r in second.json()["results"]}
        # Same batch key + same item indices -> same jobs, no duplicates created.
        assert first_jobs == second_jobs
        assert len(app.state.repository.jobs) == jobs_after_first
