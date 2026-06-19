from apps.api.main import app


EXPECTED_PATHS = {
    "/api/auth/register",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/session",
    "/api/auth/me",
    "/api/auth/me/change-password",
    "/api/auth/users",
    "/api/auth/users/{user_id}",
    "/api/auth/registration-codes",
    "/api/auth/registration-codes/{code_id}",
    "/api/uploads/prepare",
    "/api/uploads/{upload_session_id}/file",
    "/api/uploads/complete",
    "/api/uploads/{upload_session_id}/cancel",
    "/api/uploads/{upload_session_id}",
    "/api/secrets",
    "/api/secrets/{secret_id}/rotate",
    "/api/secrets/{secret_id}/disable",
    "/api/cases",
    "/api/cases/{case_id}",
    "/api/jobs/digital-human-video",
    "/api/jobs/{job_id}",
    "/api/jobs/{job_id}/runs",
    "/api/runs/{run_id}",
    "/api/runs/{run_id}/cancel",
    "/api/runs/{run_id}/retry",
    "/api/runs/{run_id}/resume",
    "/api/runs/{run_id}/report",
    "/api/runs/{run_id}/artifacts",
    "/api/runs/{run_id}/events",
    "/api/media/assets",
    "/api/media/assets/{asset_id}",
    "/api/media/assets/{asset_id}/preview-url",
    "/api/media/assets/{asset_id}/content",
    "/api/annotations/{asset_id}",
    "/api/annotations/{asset_id}/rerun",
    "/api/voices",
    "/api/voices/clone",
    "/api/voices/design",
    "/api/voices/{voice_id}/preview",
    "/api/voices/{voice_id}",
    "/api/prompts",
    "/api/prompts/{template_id}/versions",
    "/api/prompts/{template_id}/versions/{version_id}/approve",
    "/api/prompts/{template_id}/versions/{version_id}/publish",
    "/api/prompts/{template_id}/rollback",
    "/api/prompts/bindings",
    "/api/prompts/bindings/{binding_id}",
    "/api/prompts/experiments",
    "/api/prompts/experiments/{experiment_id}",
    "/api/providers/profiles",
    "/api/providers/profiles/{profile_id}",
    "/api/providers/profiles/{profile_id}/test",
    "/api/providers/capabilities",
    "/api/providers/price-catalogs",
    "/api/providers/price-catalogs/{catalog_id}/approve",
    "/api/providers/price-catalogs/{catalog_id}/publish",
    "/api/providers/price-catalogs/{catalog_id}/deprecate",
    "/api/providers/usage",
    "/api/providers/balances",
    "/api/providers/balances/refresh",
    "/api/providers/reconcile-billing",
    "/api/cases/{case_id}/agent/drafts",
    "/api/cases/{case_id}/agent/drafts/{draft_id}/adopt",
    "/api/cases/{case_id}/performance",
    "/api/cases/{case_id}/metrics/import",
    "/api/cases/{case_id}/scripts/generate-with-memory",
    "/api/cases/{case_id}/rubric",
    "/api/cases/{case_id}/rubric/calibration",
    "/api/cases/{case_id}/rubric/bump-proposal",
    "/api/cases/{case_id}/rubric/bump-proposal/{proposal_id}/accept",
    "/api/cases/{case_id}/rubric/bump-proposal/{proposal_id}/reject",
    "/api/cases/{case_id}/predictions",
    "/api/cases/{case_id}/finished-videos/{finished_video_id}/metrics",
    "/api/cases/{case_id}/pending-retro",
    "/api/videos/{video_version_id}/performance-attribution",
    "/api/cases/{case_id}/finished-videos",
    "/api/finished-videos/{id}",
    "/api/finished-videos/{id}/preview-url",
    "/api/finished-videos/{id}/download",
    "/api/finished-videos/{id}/editor-handoff",
    "/api/finished-videos/{id}/jianying-draft",
    "/api/publish/packages",
    "/api/publish/batches",
    "/api/publish/batches/{batch_id}",
    "/api/publish/batches/{batch_id}/submit",
    "/api/publish/batches/{batch_id}/items/{item_id}/retry-publish",
    "/api/publish/items/{item_id}",
    "/api/publish/attempts/{attempt_id}",
    "/api/ops/dashboard",
    "/api/ops/cost-rollups",
    "/api/ops/yield-funnel",
    "/api/ops/provider-usage-metrics",
    "/api/ops/budgets",
    "/api/ops/budgets/{budget_id}",
    "/api/ops/alerts/{event_id}/ack",
    "/api/ops/alerts/{event_id}/resolve",
    "/api/runs/{run_id}/quality-checks",
    "/api/finished-videos/{id}/quality-checks",
    "/api/approval-requests/{id}/approve",
    "/api/approval-requests/{id}/reject",
    "/api/audit/events",
    "/api/import/batches",
    "/api/import/batches/{batch_id}",
}


def test_spec_34_paths_are_registered_in_openapi():
    paths = set(app.openapi()["paths"])
    missing = EXPECTED_PATHS - paths
    assert not missing


# Endpoints that intentionally have no 2xx success: deliberate "Gone"/not-supported stubs.
NO_SUCCESS_RESPONSE_EXEMPTIONS = {
    ("POST", "/api/creative/reference-extractor/refresh-cookies"): (
        "browser-profile auto-refresh is not supported; endpoint is a 410/Gone stub"
    ),
}


def test_every_write_endpoint_has_a_declared_success_response():
    spec = app.openapi()
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            if method.lower() in {"post", "patch", "put", "delete"}:
                if (method.upper(), path) in NO_SUCCESS_RESPONSE_EXEMPTIONS:
                    continue
                statuses = set(operation["responses"])
                assert statuses & {"200", "201", "202"}, f"{method.upper()} {path}"
