import type { NodeRun, RunCard } from "../../api/client";

export type RunAction = "cancel" | "forceCancel" | "retry" | "resume" | "delete";

// 原始流水线节点 → 中文说明（节点流水线里作为英文名的补充副标题）。
const NODE_LABELS: Record<string, string> = {
  ValidateRequest: "校验请求",
  LoadCaseContext: "加载案例上下文",
  ResolveCreativeIntent: "解析创作意图",
  TTS: "生成配音",
  MaterialPackPlanning: "规划素材包",
  NarrationAlignment: "对齐旁白时间轴",
  NarrationBoundaryPlanning: "规划旁白边界",
  TimelineWindowPlanning: "编译时间线窗口",
  WindowQueryPlanning: "生成窗口检索 query",
  WindowMaterialRetrieval: "向量检索窗口候选",
  DeterministicEditingPlanning: "确定性剪辑指派",
  // 历史 run 兼容：#158 后 active 模板不再调度 PortraitPlanning。
  PortraitPlanning: "规划数字人镜头",
  BrollPlanning: "规划 B-roll 插入",
  StylePlanning: "规划字幕与包装",
  MediaSelectionAgentPlanning: "媒体选择 Agent 规划",
  TimelineAssemblyValidation: "组装并校验时间线",
  PortraitTrackBuild: "生成数字人轨道",
  LipSync: "口型同步",
  RenderFinalTimeline: "渲染主时间线",
  CaptionCompositionPlanning: "规划固定字幕带",
  BgmAgentPlanning: "规划背景音乐",
  SubtitleAndBgmMix: "混合字幕与配乐",
  ExportFinishedVideo: "导出成片",
  FinalizeRunReport: "生成运行报告",
  SeedanceGenerateVideo: "Seedance 文生视频",
  ExportSeedanceVideo: "导出 Seedance 成片",
};

export function nodeLabel(id: string): string {
  return NODE_LABELS[id] ?? id;
}

// 各工作流模板的节点顺序，镜像 packages/production/pipeline/node_sequence.py。
// 节点流水线用它补齐"尚未创建 NodeRun 的待执行节点"（后端 node runs 是懒创建的）。
const TEMPLATE_NODE_SEQUENCES: Record<string, string[]> = {
  digital_human_v2: [
    "ValidateRequest",
    "LoadCaseContext",
    "ResolveCreativeIntent",
    "TTS",
    "MaterialPackPlanning",
    "NarrationAlignment",
    "NarrationBoundaryPlanning",
    "TimelineWindowPlanning",
    "WindowQueryPlanning",
    "WindowMaterialRetrieval",
    "DeterministicEditingPlanning",
    "TimelineAssemblyValidation",
    "PortraitTrackBuild",
    "LipSync",
    "RenderFinalTimeline",
    "CaptionCompositionPlanning",
    "SubtitleAndBgmMix",
    "ExportFinishedVideo",
    "FinalizeRunReport",
  ],
  digital_human_editing_agent_v2: [
    "ValidateRequest",
    "LoadCaseContext",
    "ResolveCreativeIntent",
    "TTS",
    "MaterialPackPlanning",
    "NarrationAlignment",
    "NarrationBoundaryPlanning",
    "TimelineWindowPlanning",
    "WindowQueryPlanning",
    "WindowMaterialRetrieval",
    "MediaSelectionAgentPlanning",
    "TimelineAssemblyValidation",
    "PortraitTrackBuild",
    "LipSync",
    "RenderFinalTimeline",
    "CaptionCompositionPlanning",
    "BgmAgentPlanning",
    "SubtitleAndBgmMix",
    "ExportFinishedVideo",
    "FinalizeRunReport",
  ],
  seedance_t2v_v1: [
    "ValidateRequest",
    "LoadCaseContext",
    "SeedanceGenerateVideo",
    "ExportSeedanceVideo",
    "FinalizeRunReport",
  ],
};

export type NodeTimelineItem = {
  nodeId: string;
  status: string;
  node?: NodeRun;
};

/** 模板期望序列 ⊕ 实际 NodeRun：未创建的节点补成 pending，模板外的历史节点按实际顺序追加。
 *  run 已成功时不再补 pending（老模板 run 的节点集合可能与现行序列不同，补出来是噪音）。 */
export function buildNodeTimeline(
  templateId: string | null | undefined,
  nodes: NodeRun[],
  runStatus?: string,
): NodeTimelineItem[] {
  const byId = new Map(nodes.map((node) => [node.node_id, node]));
  const sequence = TEMPLATE_NODE_SEQUENCES[templateId ?? ""] ?? [];
  const items: NodeTimelineItem[] = sequence.flatMap((nodeId) => {
    const node = byId.get(nodeId);
    if (!node && runStatus === "succeeded") return [];
    return [{ nodeId, status: node?.status ?? "pending", node }];
  });
  const known = new Set(sequence);
  for (const node of nodes) {
    if (!known.has(node.node_id)) {
      items.push({ nodeId: node.node_id, status: node.status, node });
    }
  }
  return items;
}

// 把原始节点聚合成 5 个用户可理解的生产阶段。
type StageDef = { key: string; label: string; detail: string; nodes: string[] };
const STAGE_DEFS: StageDef[] = [
  { key: "script", label: "脚本与意图", detail: "校验请求、加载案例、解析创作意图", nodes: ["ValidateRequest", "LoadCaseContext", "ResolveCreativeIntent"] },
  { key: "voice", label: "配音合成", detail: "生成数字人配音并对齐时间轴", nodes: ["TTS", "NarrationAlignment"] },
  { key: "material", label: "素材匹配与编排", detail: "匹配素材，并组装校验时间线", nodes: ["MaterialPackPlanning", "NarrationBoundaryPlanning", "TimelineWindowPlanning", "WindowQueryPlanning", "WindowMaterialRetrieval", "DeterministicEditingPlanning", "PortraitPlanning", "BrollPlanning", "StylePlanning", "MediaSelectionAgentPlanning", "TimelineAssemblyValidation"] },
  { key: "lipsync", label: "口型同步", detail: "生成数字人轨道并做唇形同步", nodes: ["PortraitTrackBuild", "LipSync"] },
  { key: "compose", label: "合成出片", detail: "渲染时间线、规划并混合字幕配乐、导出成片", nodes: ["RenderFinalTimeline", "CaptionCompositionPlanning", "BgmAgentPlanning", "SubtitleAndBgmMix", "ExportFinishedVideo", "FinalizeRunReport"] },
];

export type StageView = { key: string; label: string; detail: string; status: string };

export function buildStages(nodes: NodeRun[]): StageView[] {
  const byId = new Map(nodes.map((node) => [node.node_id, node.status]));
  return STAGE_DEFS.map((stage) => {
    const statuses = stage.nodes.map((id) => byId.get(id)).filter(Boolean) as string[];
    let status = "pending";
    if (statuses.some((s) => s === "failed")) status = "failed";
    else if (statuses.some((s) => s === "running" || s === "admitted")) status = "running";
    else if (statuses.length > 0 && statuses.every((s) => s === "skipped")) status = "skipped";
    else if (statuses.length > 0 && statuses.every((s) => ["succeeded", "skipped", "degraded"].includes(s))) {
      status = statuses.some((s) => s === "degraded") ? "degraded" : "succeeded";
    }
    return { key: stage.key, label: stage.label, detail: stage.detail, status };
  });
}

export type PendingAction = {
  type: RunAction;
  run: RunCard;
};

export function canResumeRun(run: RunCard) {
  return run.status === "failed" && run.canResume;
}

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
  if (action?.type === "forceCancel") return "系统会强制要求生成链路停止，适用于普通中断长时间无响应的任务。";
  if (action?.type === "retry") return "系统会复制当前配置并创建新的生成任务，可能产生新的供应商费用。";
  if (action?.type === "resume") return "系统会从失败阶段继续执行，并复用已完成节点的有效产物。";
  if (action?.type === "delete") return "系统只删除任务记录和关联 Job 记录，不删除已经落库的成片文件。";
  return "请确认是否继续。";
}

export function confirmConsequences(action: PendingAction | null) {
  if (action?.type === "cancel") return ["不会删除已生成文件", "任务会进入中断中，最终状态由生成链路确认"];
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
  if (value === "lipsync.fallback_used") return "主口型供应商失败，已由兜底供应商生成";
  if (value === "bgm.loudness_probe_failed") return "BGM 响度探测失败，已按请求音量混音";
  if (value === "font.resolution_failed") return "指定字体文件解析失败，已使用默认字体";
  if (value === "media_selection_agent.deterministic_fallback") return "媒体选择 Agent 已使用确定性兜底";
  if (value === "media_selection_agent.llm_repair") return "媒体选择 Agent 已通过模型修复重试";
  if (value === "media_selection_agent.local_constraint_repair") return "媒体选择 Agent 已执行本地约束修正";
  if (value === "broll.insertions_dropped_geometry") return "部分 B-roll 插入因时间线几何约束被丢弃";
  if (value === "window_query.template_fallback") {
    return "检索 query 已回退模板拼接（LLM 不可用）";
  }
  if (value === "font.metrics_fallback") return "字体度量回退估算";
  if (value === "caption.composition_fallback") return "部分强调已回退为普通字幕";
  if (value === "bgm.planning_failed") return "BGM Agent 未成功，已使用确定性本地选择";
  return "未知警告";
}

export function severityLabel(value: string) {
  if (value === "info") return "提示";
  if (value === "warning") return "警告";
  if (value === "fatal") return "致命";
  return "错误";
}
