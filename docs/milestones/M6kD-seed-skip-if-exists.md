# M6k-D 施工简报：内容寻址存储「已存在则跳过上传」（省 seed 重传，加速 worker 启动）

负责：Codex（执行）/ Claude（架构 + 验收）
分支：`feat/m6kd-store-skip-if-exists`
来源：演示切远端 OSS 后，**每次重启 worker/API 都重传 seed 媒体到 OSS（上海，慢）**，启动要 2-3 分钟。根因：`_ensure_seed_media_assets`（digital_human.py:211）在每次构造 pipeline（worker 启动）时对基座 seed 资产调 `store_file(..., addressed=True)`；M6k 已让 key 内容寻址（sha256 确定性）不再累积，但**仍每次 put 上传一遍**。

## 已勘定事实（勿推翻）

- `store_file(object_store, path, *, purpose, addressed=False)`（packages/media/assets.py）：addressed=True 时 `content_key=sha256(content)`，`prepare_upload(..., content_key=)` 生成确定性 key，再 `put_bytes`。**同内容 → 同 key → 同对象**。
- seed 媒体内容确定（testsrc2/正弦 wav 等，文件 `.data/generated-media/seed/*` 复用），sha256 稳定 → key 稳定 → 首次上传后对象一直存在。
- `ObjectStore.exists(ref)` 已存在（Local/S3/Tiered 都实现；S3 走 head_object）。
- `StoredObject(ref, size_bytes, sha256)`：size/sha256 由内存 content 算，不依赖远端。

## 改动清单（仅 `packages/media/assets.py` + 测试）

### A. addressed 存储「已存在则跳过」

- A1 `store_file`：当 `addressed=True` 时，构造 ref 后**先 `object_store.exists(ref)`**；若已存在，直接返回 `StoredObject(ref=ref, size_bytes=len(content), sha256=content_key)`，**不调 put_bytes**（省去重传）。否则照常 `put_bytes`。
  - 仅对 addressed=True 生效（内容寻址 → key 确定 → 存在即同内容，跳过安全）。`addressed=False`（运行产物，随机 key）维持每次 put 不变。
  - 注意 import `StoredObject`（from packages.core.storage.object_store import StoredObject）。
- A2 不改 `_ensure_seed_media_assets`、不改 object_store；只动 store_file。
- A3（说明，不做）跳过时不写本地 cache；后续若需本地文件由 ObjectStore `_path` 懒下载（cache miss → 下载）。seed 探针用的是本地 `.data/generated-media/seed/*` 原文件，不依赖 cache。

### B. 测试

- B1 `store_file(addressed=True)` 单测：注入 fake/Local object_store——
  - 对象不存在时：调 put_bytes（落对象），返回 StoredObject sha256/size 正确。
  - 对象已存在时（先 store 一次，再 store 同内容）：**第二次不调 put_bytes**（用计数 fake 或断言对象/调用次数），仍返回正确 StoredObject（同 uri/sha256）。
- B2 `addressed=False` 单测：每次都 put（不跳过），维持现状。
- B3 复用 M6k 既有 `tests/media/test_assets_store_file.py` 夹具。全量基线（约 198 单测）不回退。

## 边界（Out of scope）

- 不改运行产物路径（addressed=False 不变）。不碰 pipeline/provider/前端。
- 不做 seed source_artifact_id 持久化（与本批正交；M6P 已让 DB 素材在 run 中可用，本批只省启动重传）。

## 验收门（验收官，真 OSS live）

1. 重启 worker（seed 对象已在 OSS）：启动**明显变快**（seed 不再重传，仅 HEAD 跳过）；首次（对象不存在）仍正常上传。
2. 全量 + DB + Temporal 三套绿。
