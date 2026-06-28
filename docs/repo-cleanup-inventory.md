# Repository Cleanup Inventory

Candidates start as observations. They move to actionable cleanup only after at least two independent evidence sources support the change.

## Dead Files

- Deleted frontend compatibility wrappers now superseded by canonical `apps/web/src/components/ui/*` modules:
  - `apps/web/src/components/Modal.tsx`
  - `apps/web/src/components/State.tsx`
  - `apps/web/src/components/Status.tsx`
  - `apps/web/src/components/Toast.tsx`
- Deleted unused frontend compile-time contract probes after `knip --production`, repo-wide reference search, and `tsc`/build validation:
  - `apps/web/src/contracts/caseEdit.typecheck.ts`
  - `apps/web/src/contracts/m6aA.typecheck.ts`
  - `apps/web/src/contracts/m6aR3.typecheck.ts`
  - `apps/web/src/contracts/m6aR4.typecheck.ts`
  - `apps/web/src/contracts/m6aR5.typecheck.ts`
  - `apps/web/src/contracts/m6aR6.typecheck.ts`
  - `apps/web/src/contracts/m6eB.typecheck.ts`

## Dead Functions

- Removed unused frontend payload/type helpers from:
  - `apps/web/src/components/modals/CaseModal.tsx`
  - `apps/web/src/components/studio-create/batchModel.ts`
  - `apps/web/src/components/studio-create/studioCreateModel.ts`
  - `apps/web/src/hooks/notificationModel.ts`
  - `apps/web/src/hooks/useTaskNotifications.ts`
  - `apps/web/src/utils/annotationV4.ts`
- Removed duplicate private performance observation/score row mapper methods from `packages/creative/cases/sqlalchemy_rubric.py` and `packages/production/sqlalchemy_repository.py`.
- Removed duplicate `_artifact_row` helper from `packages/production/sqlalchemy_repository.py`.

## Unused Exports

- Removed production-unused frontend exports surfaced by `knip --production`, except where tests were moved to a public entrypoint instead of retaining a production-unused export.
- Removed duplicate/unused API type exports from `apps/web/src/api/client.ts` and `apps/web/src/api/r6.ts`.

## Unused Dependencies

No accepted candidates yet.

## Duplicate Utilities

- Consolidated the Case reusable-material kind allowlist into `packages/core/contracts/media.py::CASE_MATERIAL_ASSET_KINDS`; API and SQLAlchemy case-count paths now import it instead of carrying duplicate literals.
- Consolidated planning/material person-centric subject terms into `packages/planning/material/subject_terms.py::PERSON_SUBJECT_TERMS`; b-roll filtering and portrait lip-sync source detection now share one tuple.
- Removed frontend UI compatibility re-export wrappers and pointed callers at canonical `components/ui/*` modules.
- Consolidated import-metadata parsing into `packages/core/storage/import_metadata.py`; API import handling, production import persistence, and production mappers now use one implementation.
- Consolidated loudness JSON extraction/probing into `packages/media/audio/loudness.py`; BGM annotation and ffmpeg pipeline code now use the shared helper.
- Consolidated performance observation/score row mappers into `packages/core/storage/performance_mappers.py`; production mappers re-export the canonical functions.
- Added `packages/core/storage/sqlalchemy_uploads.py::artifact_to_row` beside `artifact_row_to_contract` and replaced duplicate Artifact contract-to-row mapping.
- Consolidated timeline-planning `NodeOutput` artifact construction into `packages/production/pipeline/nodes/_timeline_output.py`.
- Consolidated finished-video/Seedance publish package artifact creation in `packages/production/pipeline/nodes/export_finished_video.py`.
- Consolidated digital-human run-running transition/event/funnel recording into one private helper.

## Duplicate Schemas or Types

No accepted candidates yet.

## Duplicate Validation Logic

No accepted candidates yet.

## Stale Tests

- Cleaned vulture-detected test-only unused variables in fake context managers/callbacks while preserving public fake signatures where callers may pass keyword args.
- Converted the Settings `clean_env` fixture to autouse and removed unused fixture parameters from Settings contract tests.

## Stale Docs

- `AGENTS.md`: Alembic range still said `0001…0011`; actual tracked migrations now reach `0022_drop_publish_hashtags`.
- `AGENTS.md`: child-directory guidance pointed to `AGENTS.md`; actual child guides in this repo are `CLAUDE.md`.
- `AGENTS.md`: package map lagged behind current root `CLAUDE.md` wording for core storage/secret handling and production workflow variants.
- `README.md`: Alembic range still said `0001…0011`; actual tracked migrations now reach `0022_drop_publish_hashtags`.
- `README.md`: Jobs/Runs row referenced nonexistent `apps/api/routers/cost_estimate.py`; actual router is `apps/api/routers/jobs_runs.py`.
- `README.md`: `scripts/dev_up.sh up` example claimed web default `5176`; actual script default is `8001`.
- `README.md`: storage backend list omitted `postgres`, which `packages/core/storage/bootstrap.py` accepts as a SQLAlchemy backend alias.
- `README.md`: DB/Temporal test env examples repeated the full manual setup block; test section now references the manual SQLAlchemy/Temporal/MinIO env and only adds the opt-in test switches.
- `README.md`: contract regeneration used a bare `python scripts/export_openapi.py` command that fails on this host; updated the live command to `uv run --extra dev python scripts/export_openapi.py`.
- `README.md`: prose referred to `ci_gate.sh` without the `scripts/` prefix even though the root file does not exist.
- `apps/web/CLAUDE.md`: referenced stale `src/contracts/*.typecheck.ts` frontend probe files after those probes were removed.
- `AGENTS.md`, `CLAUDE.md`, `apps/api/CLAUDE.md`, `apps/web/CLAUDE.md`: live contract-regeneration guidance now points at the working `uv run --extra dev python scripts/export_openapi.py` path.
- `docs/ROADMAP.md`: current milestone discipline assigned Codex worktrees to `.claude/worktrees`; current Codex desktop worktrees use `.codex/worktrees/<id>/<repo>`.
- `.env.example`: preamble said every variable maps to `Settings`; render/ephemeral debug switches are valid but read directly by their runtime consumers.
- `.env.example`: missed live `Settings` env groups including DB pool, auth limiter/cookie policy, provider host policy, publishing CDP endpoint, upload, balance, learning, and motion guard. Completed sample coverage and added a contract guard.

## Obsolete Configs

- `scripts/dev_up.sh`: main-checkout inference only recognized `.claude/worktrees`; current Codex desktop worktrees live under `.codex/worktrees/<id>/<repo>`. Replaced with Git common-dir resolution plus the legacy fallback.
- `tests/contract/test_settings_config.py`: `_INFRA_ENV_VARS` was stale versus actual `Settings` env reads, allowing external shell env to leak into default assertions. Expanded it and added `.env.example` coverage check.
- `apps/web/package.json`: `export:openapi` used bare `python`, which failed on this host and also surfaced as a Knip unlisted-binary issue. Replaced it with `uv run --extra dev python` and added a narrow `ignoreBinaries` entry for the repo-level `uv` tool.
- `scripts/export_openapi.py`: OpenAPI export inherited local proxy env and could fail during app import when `httpx` attempted SOCKS support. The export script now clears proxy env before importing the app.

## Unused Scripts

- `scripts/clean_dangling_materials.py` and `scripts/sync_materials.py`: repository references are limited to self-documentation. Classified as high-risk manual ops scripts, kept and lint-cleaned instead of deleted.

## Unused Environment Variables

No accepted candidates yet.

## Generated or Build Artifacts That Should Not Be Committed

- Local ignored artifacts observed after baseline validation: `.venv/`, `.pytest_cache/`, `.ruff_cache/`, `apps/web/node_modules/`, `apps/web/dist/`, `apps/web/tsconfig.tsbuildinfo`, Python `__pycache__/`, `cutagent_cleanslate.egg-info/`.
- No tracked generated/build artifact candidate accepted yet.

## High Risk Items Intentionally Left Untouched

- Database migrations under `packages/core/storage/alembic/versions/`.
- Generated OpenAPI client files: `apps/web/src/api/openapi.json`, `apps/web/src/api/schema.d.ts`.
- Temporal workflow/activity registration paths.
- Seed scripts, fixtures, provider registries, object-store settings, and external deployment hooks.
- Manual DB/OSS operational scripts that may be used outside repository references.
- `normalize_dsn` duplicated in two manual DB/OSS operational scripts; intentionally left local because extracting a shared helper would widen a high-risk manual ops surface for little gain.
- Duplicate VolcEngine auth error code tuples in AI provider and ops balance provider; left untouched because they sit on separate provider-facing error classification surfaces.
- Duplicate publishing copy field tuple in deterministic and LLM copy paths; left untouched because it is schema/validation alignment, not dead duplication.
- Funnel taxonomy tuple duplicated in tests as a spec guard; intentionally not consolidated into production code.
- Deptry-reported `fontTools` is intentionally optional in `packages/production/pipeline/_fonts.py`; current environment lacks it and the built-in font-name parser fallback covers the normal path.
