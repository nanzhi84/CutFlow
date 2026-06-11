import type { RunCard } from "../../api/client";

export type RunAction = "cancel" | "forceCancel" | "retry" | "resume" | "delete";

export type PendingAction = {
  type: RunAction;
  run: RunCard;
};

export function connectionLabel(state: string) {
  if (state === "live") return "实时连接中";
  if (state === "connecting") return "正在连接";
  if (state === "reconnecting") return "重连中";
  if (state === "error") return "连接异常";
  return "未连接";
}

export function confirmTitle(action: PendingAction | null) {
  if (action?.type === "cancel") return "确认中断生成任务";
  if (action?.type === "forceCancel") return "确认强制终止生成任务";
  if (action?.type === "retry") return "确认重试任务";
  if (action?.type === "resume") return "确认续跑任务";
  if (action?.type === "delete") return "确认删除任务记录";
  return "确认操作";
}

export function confirmMessage(action: PendingAction | null) {
  if (action?.type === "cancel") return "系统会请求停止当前生成链路，已完成产物会保留在运行记录中。";
  if (action?.type === "forceCancel") return "系统会强制要求后端工作流停止，适用于普通中断长时间无响应的任务。";
  if (action?.type === "retry") return "系统会复制当前配置并创建新的生成任务，可能产生新的供应商费用。";
  if (action?.type === "resume") return "系统会从失败阶段继续执行，并复用已完成节点的有效产物。";
  if (action?.type === "delete") return "系统只删除任务记录和关联 Job 记录，不删除已经落库的成片文件。";
  return "请确认是否继续。";
}

export function confirmConsequences(action: PendingAction | null) {
  if (action?.type === "cancel") return ["不会删除已生成文件", "任务会进入中断中，最终状态由后端工作流确认"];
  if (action?.type === "forceCancel") return ["不会删除已生成文件", "会跳过温和中断等待，可能让当前节点报告为取消或失败"];
  if (action?.type === "retry") return ["会创建新的 Run", "会重新调用必要供应商能力并可能计费"];
  if (action?.type === "resume") return ["会复用可用产物", "只从失败或待恢复阶段继续执行"];
  if (action?.type === "delete") return ["处理中任务不能删除", "成片文件会保留，但会与该任务记录解除关联", "删除后运行详情和节点时间线不可再查看"];
  return [];
}

export function confirmButtonText(action: PendingAction | null) {
  if (action?.type === "cancel") return "确认中断";
  if (action?.type === "forceCancel") return "强制终止";
  if (action?.type === "retry") return "确认重试";
  if (action?.type === "resume") return "确认续跑";
  if (action?.type === "delete") return "删除记录";
  return "确认";
}

export function warningLabel(value: string) {
  if (value === "broll.skipped_no_material") return "B-roll 素材不足，已跳过插入";
  if (value === "bgm.skipped_library_unannotated") return "BGM 库未完成标注，已跳过配乐";
  if (value === "font.default_used") return "指定字体不可用，已使用默认字体";
  if (value === "cover.frame_fallback") return "封面生成降级为取帧";
  if (value === "timestamp.estimated") return "部分时间戳为系统估算";
  if (value === "cost.unpriced") return "部分供应商费用未定价";
  return "未知警告";
}

export function severityLabel(value: string) {
  if (value === "info") return "提示";
  if (value === "warning") return "警告";
  if (value === "fatal") return "致命";
  return "错误";
}

export function artifactLabel(value: string) {
  if (value === "video.final" || value === "video.finished") return "最终视频";
  if (value === "video.rendered") return "渲染视频";
  if (value === "subtitle.ass") return "字幕文件";
  if (value === "cover.image") return "封面图片";
  if (value === "audio.tts") return "配音音频";
  if (value === "publish.package") return "发布包";
  if (value === "run.report.public") return "公开报告";
  if (value === "run.report.debug") return "调试报告";
  return "运行产物";
}
