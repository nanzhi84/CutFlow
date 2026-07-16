# 工作流取消与产物提交栅栏

运行取消是一条跨 API、Temporal activity、FFmpeg、SQL 和对象存储的协议。终态 `cancelled` 的含义不是「取消请求已经发出」，而是当前 activity 已停止、其拥有的子进程树已经回收，并且不会再发布新的交付产物。

## 状态与时序

1. API 在 `workflow_runs` 行锁内记录 `cancel_mode`、`cancel_requested_at`，并把运行中的 run 改为 `cancelling`。尚未开始的 run 可直接进入 `cancelled`。
2. SQL 提交成功后，API 才向 Temporal workflow 发送 cancel signal。force 是 sticky escalation：后续 graceful 请求不能把它降级。
3. workflow 取消当前 activity，并等待 activity 完成清理。activity 同时检查 Temporal cancellation 和 SQL 取消标记，避免 signal 或 worker task 延迟留下执行窗口。
4. `FfmpegRunner` 向整个进程组发送 SIGTERM；graceful 模式等待最多 5 秒，仍未退出或升级为 force 时发送 SIGKILL，随后 `communicate()` 回收进程。
5. activity 返回清理确认后，workflow 执行 `mark_run_cancelled`，写入 run/job 终态并释放未提交的素材预留。

worker 重启后会扫描 durable `cancelling` run 并重发 signal。不得用 Temporal terminate 代替上述流程，因为 terminate 不等待 activity 清理。

## SQL 提交栅栏

`sync_workflow_snapshot()` 的第一步是锁定 `workflow_runs`。如果 durable 状态已经是 `cancelling` 或 `cancelled`，本次快照不得新增：

- succeeded/degraded/skipped NodeRun 及其 output artifact；
- 成片、封面、字幕、发布包、剪映草稿和编辑交接包；
- FinishedVideo/VideoVersion/PublishPackage、成功 outbox 及 yield 事件；
- selection ledger 的新提交。

取消报告、provider invocation、prompt invocation、usage record，以及 reservation 的 release/expire 仍可落库。数据库行锁决定竞态赢家：完成提交先拿锁则产物有效；取消请求先拿锁则后到的成功提交被栅栏拒绝。

resume 只水合 succeeded/degraded/skipped NodeRun 显式列出的 output artifact。失败节点的诊断附件和未绑定到成功节点的对象不会成为恢复输入。

## 媒体与对象存储

media 包内的 ffmpeg/ffprobe 调用统一经过 `FfmpegRunner`，以便继承 activity 的取消 token。最终渲染先写临时目录中的 `*.part.*`，完成探测与媒体校验后再通过 `promote_staged_media()` 原子晋升，随后才能上传。

S3/OSS 上传写入 SHA-256 metadata，并在上传后用 HEAD 校验大小和 checksum。`scripts/provision_oss_cors.py` 同时维护 staging 过期与未完成 multipart 清理规则；`scripts/gc_orphan_objects.py` 默认 dry-run，只处理超过保留期且不在 artifact 表引用集合中的生成对象，确认后才可加 `--apply`。

## 可观测与排障

结构化日志包含 `run_id`、`node_id`、PID/PGID、cancel mode、TERM/KILL 与 reaped 事件。核心指标为取消完成延迟、force kill 次数和取消后被栅栏跳过的产物提交数。

排障时先确认状态顺序 `running → cancelling → cancelled`，再按同一 `run_id` 检查 `media.process.cancel_requested`、`media.process.sigterm_sent`/`media.process.sigkill_sent`、`media.process.reaped`。只有出现 reaped 且 SQL 中没有取消后的交付产物，才算取消闭环完成。
