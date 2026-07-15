# 树影文档

长期文档分两类：`docs/` 根下的三个概览文件（milestones、关键技术选型、关键设计决策），加上 `docs/architecture/` 子目录里针对单个横切子系统的权威参考文档。三个根文件保留当前仍有价值的工程事实，曾经踩过的错误、bug 与问题处理原则折叠进「关键设计决策」，不再单独开长篇事故档案；当某个子系统的边界 / 降级语义 / 命名细节多到塞进「关键设计决策」会喧宾夺主时，才在 `docs/architecture/` 下单开一份长期参考文档。

概览：

- [Milestones](milestones.md)
- [关键技术选型](technical-choices.md)
- [关键设计决策](design-decisions.md)

架构参考（`docs/architecture/`）：

- [Redis 跨进程协调层](architecture/redis-coordination.md) — Redis 是可选协调层而非查询缓存；四项用途、限流分组、拓扑硬闸门、降级语义。
- [预签名 URL 缓存与浏览器缓存](architecture/signed-url-caching.md) — issue #206 烧掉 884 GB 的根因；为什么「单独调长 TTL 一分钱都省不下」；50% 重签阈值的推导；WebP 缩略图与回填。
- [可恢复上传与原子登记](architecture/resumable-upload.md) — 200 MiB、OPFS/Worker 增量哈希、S3 multipart、崩溃恢复状态机、唯一登记与部署顺序。

不要把一次性执行计划、临时调研、PR 清理证据或过期审计长文放回 `docs/`。实现完成后，把仍然有用的结论折叠进上面的根文件；只有稳定的子系统权威说明才进 `docs/architecture/`。
