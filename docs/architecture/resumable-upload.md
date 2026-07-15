# 可恢复上传与原子登记

上传链路支持精确到 `200 * 1024 * 1024` bytes 的浏览器直传。它解决两个独立问题：浏览器刷新后仍有原始文件可继续上传，以及对象已写入后服务端进程退出仍能自动完成 Artifact/业务对象登记。

## 产品边界

- 全局上限是 200 MiB；字体仍为 40 MiB，音色参考仍为 80 MiB。
- 小于 16 MiB 使用单 PUT；大于等于 16 MiB 使用 multipart。
- multipart 固定 8 MiB 分片，浏览器最多并发 3 片。
- 不支持跨设备恢复；清除站点数据、无痕窗口退出或浏览器主动清退存储后无法恢复。
- 校验只确认大小、SHA-256、基础文件类型和可解析性，不增加 codec、分辨率、时长或病毒扫描准入。

## 客户端持久化

远端上传前必须先完成本地准备：

1. `navigator.storage.persist()` 必须获准，`estimate()` 必须能确认剩余空间足够；否则明确失败，尚未创建远端会话。
2. Web Worker 以 4 MiB 块把完整文件写入 OPFS，并用增量 SHA-256 同步计算期望摘要。主线程不会对完整文件调用 `arrayBuffer()`。
3. IndexedDB 只保存当前用户、稳定 `client_upload_id`、服务端会话 ID、OPFS 路径、文件元数据、分片几何、非权威 ETag 缓存和任务状态；不保存 Blob 或预签名 URL。
4. OPFS 完整落盘前，页面通过 `beforeunload` 提醒用户不要退出；完整落盘后才调用 `prepare`。
5. 登录后上传管理器只扫描当前用户的记录。恢复时先调用 `resume`，以后端 `ListParts` 覆盖本地缓存，只上传缺片；401/403 只重签对应分片。

终态 `ready/cancelled/expired/rejected/failed` 会清理本地任务和 OPFS 文件。瞬时网络或服务端错误保留本地文件，下一次登录/刷新可自动恢复。

关键路径：

- `apps/web/src/uploads/manager.ts`
- `apps/web/src/uploads/storage.ts`
- `apps/web/src/uploads/staging.worker.ts`
- `apps/web/src/uploads/sha256.ts`

## 服务端状态与幂等

```text
prepared -> uploading -> completing -> object_completed -> verified -> ready
                         \-> rejected
任意未完成状态 -> cancelled / expired
不可恢复的内部错误 -> failed
```

- `client_upload_id` 有唯一约束；相同请求重试返回同一会话，不会重复创建 multipart upload。复用同一 ID 但文件身份或参数不同会返回幂等冲突。
- prepare 重试和分片签名前会在数据库行锁内刷新过期状态；已过期会话落成 `expired` 并清理远端状态，不能靠重签复活。PUT 签名 TTL 取配置值与会话剩余寿命的较小值，并预留 1 秒取整边界。
- `completing` 先于 `CompleteMultipartUpload` 持久化，覆盖“对象存储成功、数据库尚未更新”的退出窗口。
- `ListParts` 是唯一分片事实源；浏览器不提交用于完成对象的 ETag 清单。
- 服务端按文件流式重算 `canonical_sha256`。客户端 SHA 只是期望值。
- `verified -> Artifact + MediaAsset/PublishPackage + ready` 在一个数据库事务内完成。
- `artifacts.source_upload_session_id` 是 nullable FK 且唯一，同一上传最多登记一个上传 Artifact。会话行锁和唯一约束共同处理 API/reconciler 并发。
- 缩略图在 `ready` 后以确定性对象键和 Artifact ID 派生；失败只退避重试，不回滚可用上传。

`POST /api/uploads/{id}/object-complete` 原子记录完成意图并返回 202。API 会触发一次后台推进，独立 worker 每隔 `CUTAGENT_UPLOAD_RECONCILE_INTERVAL_SECONDS` 扫描一次；两条路径使用同一数据库租约。每次直接处理使用独立 lease token，长时 ffmpeg/对象存储阶段按租期三分之一（上限 30 秒）心跳续租，阶段提交以 token fencing；进程退出后心跳停止，租约过期即可由 worker 接管。多副本批量领取使用 `FOR UPDATE SKIP LOCKED`。

关键路径：

- `apps/api/services/uploads.py`
- `packages/core/storage/sqlalchemy_uploads.py`
- `packages/media/upload_reconciler.py`
- `apps/worker/main.py`
- `packages/core/storage/alembic/versions/0058_resumable_uploads.py`

## 对象存储与清理

Local、S3/OSS 和 tiered ObjectStore 都实现 `create/sign/list/complete/abort` multipart 协议。staging 位于 durable bucket 的 `incoming/uploads/`；验证后复制到按上传类型路由的最终位置。

主动清理规则：

- cancel/expire/rejected/重试耗尽：abort 未完成 multipart，并删除 staging/final 派生对象。
- ready：删除残留 staging；重复执行安全。
- OSS lifecycle：保留桶内非本系统规则，并为 `incoming/uploads/` 增加 1 天过期和 `AbortIncompleteMultipartUpload` 1 天兜底。

生产 bucket 配置：

```bash
export CUTAGENT_UPLOAD_CORS_ALLOWED_ORIGINS=https://app.example.com
python scripts/provision_oss_cors.py
```

CORS 必须允许 PUT 并暴露 `ETag`。脚本执行后应在 OSS 控制台确认 CORS 和 lifecycle 均已生效。

## 部署与迁移

multipart 前端启用前必须完成以下顺序：

1. 暂停或排空旧 API 写流量，执行 `python scripts/migrate.py`，再部署新 API 与 worker。
2. 执行 `scripts/provision_oss_cors.py`，确认现有 lifecycle 规则仍在，新增规则同时包含 staging expiration 和 abort incomplete multipart。
3. 重启 worker，确认日志出现 worker ready 且 upload reconciler 无持续错误。
4. 最后部署前端；用真实 OSS 做一次中途关闭页面后的恢复验证。

迁移会把能从 `UploadedFileArtifact.v1` 找回 Artifact 的 legacy `completed` 会话标成 `ready`；有对象 URI 但没有 Artifact 的标成 `verified` 等待补登记；无法重建的标成 `failed` 并保留可审计原因。部署后先查看：

```sql
SELECT id, status, object_uri, last_error
FROM upload_sessions
WHERE last_error LIKE 'legacy completed session%'
ORDER BY created_at;
```

`failed` 行必须人工核对；不要直接改成 `ready`。`verified` 行由 worker 收敛，最终对象缺失或大小不符会进入 `rejected`。

## 验证

默认单元/契约/故障注入测试随 `pytest` 和前端 Vitest 运行。真实 MinIO 路径由门禁显式启用：

```bash
CUTAGENT_RUN_S3_TESTS=1 \
python -m pytest -q \
  tests/integration/test_upload_resumable_minio.py \
  tests/api/test_object_store_backends.py::test_s3_object_store_roundtrip_with_minio
```

该集成用例上传 23 MiB 文件的前两片（69.6%），重建客户端对象后通过 `ListParts` 只补第三片，并覆盖重复完成、cancel abort 和 expire abort。`scripts/ci_gate.sh` 与 GitHub integration job 都运行这条真实 MinIO 路径。
