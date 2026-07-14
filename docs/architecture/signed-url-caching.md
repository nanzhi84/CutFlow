# 预签名 URL 缓存与浏览器缓存（issue #206）

2026-07，`cutagent-dev` 桶存量只有 8.9 GB，一个月却向公网流出 **884 GB**——同一批封面被重复下载约 99 次。7/14 单日 276.5 GB，OSS 账单 ¥111.87。触发者只是一个停在 Outputs 列表页、什么都没做的浏览器标签。

## 为什么会烧钱

三件事叠成一个「每 10 秒把所有封面重下一遍」的放大器：

1. **每次读都重新签名**。`_signed_run_thumbnail` 每次列表请求都调 `signed_url()`。OSS V1 签名里嵌的是 `Expires = now + ttl`，SigV4 嵌的是 `X-Amz-Date`——**都带秒级时间戳**，所以同一个对象隔一秒签两次，得到两个不同的 URL。
2. **浏览器按完整 URL（含 query）做缓存键**。URL 一变就是 cache miss，整张图重下。
3. **封面是 1080×1920 无损 PNG**（约 2.35 MB），列表一页 19 张。

`19 × 2.35 MB × 360 次/小时 ≈ 16 GB/小时`。

## 最容易搞反的两点

- **浏览器是拿预签名 URL 直连 OSS 的，不经过我们的服务器。** 所以服务端加任何响应缓存都省不下一分钱 OSS 流量。唯一能让流量归零的，是**让浏览器不再发这个请求**。
- **单独调长 TTL 一个字节都省不下来。** 签名里的时间戳随时间递增，只要还是每次读都重签，7 天 TTL 也照样每次算出不同签名。**必须先让 URL 稳定，TTL 才有意义。**

## 现在的做法

**URL 稳定**（`packages/core/storage/signed_url_cache.py`）。签名结果按 `(uri, ttl, response_content_disposition)` 缓存；对象 key 是 `{purpose}/{uuid4 | sha256}/{name}`，**新版本必然是新 key、同一 key 的内容永不改变**，所以按 key 缓存签名天然安全。配了 `CUTAGENT_REDIS_URL` 时由 Redis 持有缓存（多个 API 副本给出同一个 URL）；Redis 挂了退化为进程内 LRU，走 `telemetry.record_redis_degraded("signed_url_cache")` + 结构化 warning 显式上报。这一层降级只影响成本、不影响正确性，因此**刻意不进 `/api/health/ready` 摘流**。

**TTL = 7 天**（`CUTAGENT_OBJECTSTORE_SIGNED_GET_TTL_SECONDS`，SigV4 的上限；OSS 原生 V1 没有上限）。注意别和只管上传 PUT 的 `settings.upload.presign_ttl_seconds` 混。

**Cache-Control: public, max-age=302400, immutable**。写入时经 `ExtraArgs` 打在对象上；读取时还额外通过签名子资源 `response-cache-control`（OSS 原生）/ `ResponseCacheControl`（SigV4）在 GET 响应里回一次——后者让**存量对象**（上传时没有这个头的）也立刻拿到缓存指令，不需要回填。`immutable` 让浏览器连条件请求都不发。

> **OSS 原生签名的坑**：`response-*` 是**签名**子资源，必须同时出现在 canonicalized resource（进 string_to_sign）和 URL query 里，且 canonicalized resource 里要按**字典序**排列，否则整条链接 403。又因为 string_to_sign 用的是**字面空格**，query 侧必须用 `quote`（`%20`）而不是 `urlencode` 默认的 `quote_plus`（`+`）——阿里云自家 SDK 就是 emit `%20`，`+` 只是碰巧能过、没有任何文档保证。写测试时**要断言原始 query 串**：`parse_qs` 会把 `+` 解回空格，正好把这个差别遮住。

### 为什么重签阈值恰好是 50%

`REFRESH_FRACTION = 0.5` 不是随手取的。设 TTL 为 `T`、重签阈值为 `f`（剩余不足 `f·T` 时重签），则：

- 一个 URL 最多被服务 `(1-f)·T` 就会轮换 → 要让浏览器在轮换前不重复请求，需要 `max-age ≥ (1-f)·T`；
- 我们发出的 URL 至少还剩 `f·T` 有效期 → 要让浏览器不会拿着已过期的 URL 去请求（403），需要 `max-age ≤ f·T`。

两式同时成立当且仅当 `f = 0.5`，此时 `max-age = (1-f)·T = f·T = T/2`。**改其中一个必须同时改另一个**，否则要么白白多下一轮，要么出现 403 破图。

## 缩略图

列表卡片不需要 1080×1920 的原封面。`build_cover_thumbnail_bytes`（`packages/media/cover_image.py`，用已有的 opencv，不引新依赖）产出长边 ≤512px、≤50 KB 的 WebP：

- 成片：`ExportFinishedVideo` / `ExportSeedanceVideo` 在封面定下来之后跑一次 `cover_thumbnail()`（**fail-open**：视频和封面都已产出、AI 封面还已付费，缩略图失败绝不能让导出失败），存进 `finished_videos.cover_thumb_artifact`（迁移 0052）。`_signed_run_thumbnail` 按「WebP → 原封面 → 成片本身」的顺序取。
- 素材库：上传完成时额外产一张 WebP（图片上传也产，否则它的卡片会去签**原图**）。

存量数据跑一次 `scripts/backfill_cover_thumbnails.py`（幂等、可中断、可重跑）。回填前缺缩略图的行回退到原封面，不白屏。

## 轮询

`RunsPage` 的三条轮询只在**确有 run 处于非终态**时保持 10s；全部终态后退避到 60s（仅用于发现别处新建的 run）；页面不可见则完全停。轮询是上面所有成本的放大系数。

**但退避不能只靠轮询兜底**：成片行在 run 转终态**之前**就已提交，而 run 一转终态、interval 从 10s 换成 60s 时，react-query 会**清掉本来马上要触发的那次 tick**。所以必须跟着 run 事件一并 `invalidate(["finished-videos", caseId])`，否则用户会看到 run 已「成功」却一分钟拿不到成片播放器和下载入口。

## 存量回填的坑

`backfill_media_assets` 的「已完成」判断必须是 **SQL 谓词**，不能是 `LIMIT` 之后的 Python 过滤：写入缩略图会 bump `updated_at`（`TimestampMixin.onupdate`），而那正是排序列，于是 `--limit N` 会把刚做完的 N 行重新排到队首，第二次跑扫到的还是同一批——永远推进不了，却报告「0 条待处理」。按不可变的 `id` 排序，并把谓词下推。同理，`thumbnail_uri` 为 NULL 的**视频**资产没东西可缩（要 ffmpeg 才能派生），必须排除在扫描之外，否则它会把 `--limit` 的窗口占死。

## 效果

| 阶段 | 每张封面重复下载频率 |
| --- | --- |
| 修复前 | 每 10 秒 → **约 16 GB/小时** |
| 只做 URL 稳定（TTL 仍 15min） | 每 ~12 分钟 |
| URL 稳定 + 7 天 TTL + Cache-Control | **每 3.5 天** |
| 再叠加 WebP 缩略图 | 每 3.5 天，且单张 2.35 MB → ~30 KB |

## 运维

三个业务桶的访问日志已开启，投递到 `cutagent-logs/<bucket>/`（30 天生命周期自动清理）。同类问题可直接定位到客户端 IP——本次事故正是因为当时没开日志，只能靠费用形态 + 代码路径推断。
