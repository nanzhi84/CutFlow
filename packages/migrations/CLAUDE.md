# packages/migrations

一次性运维脚本用的迁移辅助包：把旧平台（digital-human-Cutagent）的资产索引（本地 case 元数据目录 + 旧 OSS 上的媒体索引）导入到 genesis 的 import batch。**不是 Alembic，不放任何 DB schema 迁移。**

## 职责
- 从 `case_meta_dir` 读本地 `cases.json` / `candidate_scripts.json`，并从旧 OSS 读各类媒体索引（bgm/broll/portrait/font/cover/templates），转换成 genesis 的 import 行。
- 通过 `ImportApiClient` 调 `POST /api/import/batches`（按 import_type 分批 case/script/media）落库；旧 case 按 name 映射到现有 genesis case。
- 默认 dry-run 只打印计划；`apply=True` 才真正写入，并用确定性 `Idempotency-Key` 保证可重跑。
- 校验 OSS object 存在性、推断 mime/duration_sec，缺失资产降级为 warning、映射失败记 failure。

## 关键文件 / 子目录
- `legacy_assets.py` — 核心 `LegacyAssetMigrator` / `run_migration` / `MigrationResult`，收集与 POST 逻辑。
- `legacy_asset_clients.py` — `LegacyOssClient`（boto3 读旧 OSS）与 `ImportApiClient`（httpx 调 import API / list_cases）。
- `legacy_asset_utils.py` — 纯函数：`DEFAULT_BUCKET`/`DEFAULT_UPLOAD_PREFIX`/`DEFAULT_KINDS`、name 映射、`guess_mime`/`idempotency_key`。
- 入口（不在本包）：`scripts/migrate_legacy_assets.py`（CLI，`--apply`/`--dry-run`/`--kinds`/`--api-base`）。

## 约定与要求
- **DB schema 迁移在别处**：真正的表结构迁移是 Alembic，位于 `packages/core/storage/alembic/versions/`（当前 `0001`…`0011`），与本包无关，别在这里加。
- idempotency key 由 `idempotency_key(import_type, rows)` 内容哈希派生，必须确定性、不得随机，重跑才幂等。
- 资产缺失 / case 映射不到一律显式上报（warning 或 failure 进 `MigrationResult`），不得静默吞掉。
- OSS 凭据 / bucket / prefix 走 `CUTAGENT_LEGACY_OBJECTSTORE_*`（回退 `CUTAGENT_OBJECTSTORE_*`）环境变量，不硬编码。
- `--kinds` 取值受 `DEFAULT_KINDS` 约束：case/script/bgm/broll/portrait/font/cover。

## 测试
- `pytest tests/scripts/test_migrate_legacy_assets.py tests/scripts/test_legacy_asset_templates.py`（用 Fake OSS / import client，无需真实 OSS）。

## 注意 / 坑
- `apply=True` 时 `import_client` 必填，否则抛 ValueError；仅 dry-run 时为 None。
- import API 默认 `http://127.0.0.1:8021`；apply 是真实写库操作，先 dry-run 看计划再 `--apply`。
