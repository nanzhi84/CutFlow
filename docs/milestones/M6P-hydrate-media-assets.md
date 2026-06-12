# M6P 施工简报：运行期 hydrate Case 媒体素材（真上传素材在 run 中可用）

负责：Codex（执行）/ Claude（架构 + 验收）
分支：`feat/m6p-hydrate-media-assets`
来源：真口播片验收抓到的核心 bug——演示用真人像跑 HeyGem 对口型，**成片却是合成测试图（testsrc2）不是真人脸**。根因：worker 运行期仓库快照 `hydrate_workflow_runtime_snapshot`（packages/production/sqlalchemy_repository.py:561）**完全不加载 media_assets**，也不加载非 run 关联的 uploaded_file 源 artifact（只加载 `artifacts.run_id IN run_ids`，line 600）。于是 pipeline 只能看到**基座内存 seed**（`Repository.__init__` 里 asset_portrait_demo 等，source=None→`_ensure_seed_media_assets` 在 worker 启动时生成合成 testsrc2 并把 source 指向合成 artifact）。DB 里真实 media_asset（含用户上传/注册的真人像 `art_realportrait001`）永远不被 run 看到。

**影响**：当前**任何 DB 持久化的上传媒体素材（portrait/broll/bgm/...）都无法在生产 run 中使用**，pipeline 永远用基座合成 seed。这是核心功能缺口，不止真口播片。

## 已勘定事实（勿推翻）

- `hydrate_workflow_runtime_snapshot`（sqlalchemy_repository.py:561-602）加载 case/provider_profiles/voices/jobs/runs/node_runs + **仅 run 关联 artifacts**；**无 media_assets、无 case 级 uploaded_file 源 artifact**。
- `_ensure_seed_media_assets`（digital_human.py:211, guard line 237 `if asset is None or asset.source_artifact_id: continue`）在 `DigitalHumanPipeline.__init__` 跑；worker 启动构造一次 → 基座 seed（source=None）→ 生成合成。
- 运行期顺序（temporal_adapter.py run_node:154-164）：每个 node activity 先 `hydrate_workflow_runtime_snapshot(ctx.repository, run_id)` 再 `ctx.local_runtime.run_node_activity(...)`。所以 hydrate 里加载 media_assets 会在每个节点跑前覆盖内存里的合成 seed。
- `media_asset_row_to_contract`（packages/media/sqlalchemy_repository.py:32）、`artifact_row_to_contract`、`MediaAssetRow`（packages/core/storage/database.py）均已存在，可复用。
- media_assets 表列：id/case_id/kind/title/tags/source_artifact_id/usable/annotation_status/...。
- PortraitTrackBuild/`_source_artifact_for_asset`（digital_human.py:1304）读 `asset.source_artifact_id` → `repository.artifacts.get(source_artifact_id)`，所以源 artifact 必须也在 repository.artifacts 里。

## 改动清单（仅 `packages/production/sqlalchemy_repository.py` + 测试）

### A. hydrate 加载 Case media_assets + 其源 artifact

- A1 在 `hydrate_workflow_runtime_snapshot` 里（加载 run/case 之后），当 `run.case_id` 非空时：
  - `select(MediaAssetRow).where(MediaAssetRow.case_id == run.case_id)` → `media_asset_row_to_contract(row)` → `repository.media_assets[asset.id] = asset`（**覆盖**基座内存 seed 的同 id 条目）。
  - 对每个 asset 的 `source_artifact_id`（非空且尚未在 repository.artifacts 里）：`session.get(ArtifactRow, source_artifact_id)` → `artifact_row_to_contract` → `repository.artifacts[contract.id] = contract`。注意这些源 artifact 的 `run_id` 多为 NULL（case 级 uploaded_file），不在现有 run 关联加载范围内。
- A2 import 复用：从 packages.media.sqlalchemy_repository import media_asset_row_to_contract（注意避免循环 import；若有循环就在函数内局部 import）。MediaAssetRow 从 packages.core.storage.database import（database.py 已导出）。
- A3 不改 `_ensure_seed_media_assets`、不改 pipeline；不改基座 seed。只让 hydrate 把 DB 真相覆盖进运行期仓库。
- A4 幂等：同一 asset 多次 hydrate 覆盖同 id 无副作用；source artifact 已存在则不重复加载。

### B. 测试

- B1 `hydrate_workflow_runtime_snapshot` 集成/单测：建一个 case + 一个 media_asset（kind=portrait，usable，source_artifact_id 指向一个 uploaded_file artifact，run_id=NULL）+ 一个 run；hydrate 后断言 `repository.media_assets[asset_id].source_artifact_id == 该 artifact` 且 `repository.artifacts[该 artifact_id]` 存在（uri 正确）。复用现有 sqlalchemy 集成测试夹具（CUTAGENT_RUN_DB_TESTS 门控，验收官在外面跑 DB）。
- B2 若纯 DB 集成不便，至少加一个针对 hydrate 的单测用 in-memory sqlite 或 mock session，验证 media_assets + 源 artifact 被填入 repository。
- B3 全量基线不回退（约 197 单测）。所有 pytest 包 `timeout -k 5 600`，用主仓 venv。

## 边界（Out of scope）

- 不做 seed 媒体 source_artifact_id 持久化优化（M6k-D，单列；与本批正交——本批让 DB 素材可用，启动慢另说）。
- 不改前端、不改 provider、不改 pipeline 选材逻辑。
- annotation/标注流程不动。

## 验收门（验收官，真 DB live）

1. DB 里给 case_demo 的 portrait 资产挂真人像源（art_realportrait001 已挂），重启 worker 后跑 lipsync run：PortraitTrackBuild 用**真人像**（抽帧是真人脸，非 testsrc2）→ HeyGem 对口型 → 真口播片成片是真人脸。
2. 全量 + DB + Temporal 三套绿。
