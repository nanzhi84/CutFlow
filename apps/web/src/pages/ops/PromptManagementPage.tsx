import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  GitCompare,
  Info,
  Link2,
  Plus,
  Rocket,
  RotateCcw,
  Save,
  Send,
  Settings2,
  ToggleLeft,
  ToggleRight,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type ApiError, type PromptBindingView } from "../../api/client";
import { Modal } from "../../components/ui/Modal";
import { EmptyState, ErrorState, LoadingState } from "../../components/ui/State";
import { TimeText } from "../../components/TimeText";
import { useToast } from "../../components/ui/Toast";
import {
  describePrompt,
  diffRows,
  emptyBinding,
  emptyTemplate,
  flow,
  promptGroups,
  schemaText,
  statusLabel,
  templateUsage,
  usedVariables,
  variableChips,
  variableLabel,
  type BindingForm,
  type PromptGroupKey,
  type TemplateForm,
} from "./promptManagementUtils";

function UsageBadge({ usage, compact = false }: { usage: ReturnType<typeof templateUsage>; compact?: boolean }) {
  const label = compact ? (usage.inProduction ? "生产使用中" : "未启用") : usage.label;
  if (usage.inProduction) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-status-success/15 px-2.5 py-1 text-xs font-medium text-status-success">
        <span className="h-1.5 w-1.5 rounded-full bg-status-success" />
        {label}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-black/5 px-2.5 py-1 text-xs font-medium text-text-tertiary">
      <span className="h-1.5 w-1.5 rounded-full bg-text-tertiary/60" />
      {compact ? "未启用" : usage.label}
    </span>
  );
}

function StatusStepper({ status }: { status?: string }) {
  const activeIndex = flow.indexOf((status ?? "") as (typeof flow)[number]);
  return (
    <div className="inline-flex w-full items-stretch overflow-hidden rounded-2xl border border-border/70 bg-white/60 text-xs font-medium sm:w-auto">
      {flow.map((step, index) => {
        const isActive = index === activeIndex;
        const isDone = activeIndex >= 0 && index < activeIndex;
        return (
          <div
            key={step}
            className={`flex flex-1 items-center justify-center gap-1.5 whitespace-nowrap px-3 py-1.5 transition-colors sm:flex-none ${
              index > 0 ? "border-l border-border/60" : ""
            } ${isActive ? "bg-accent text-[#1b1d1a]" : isDone ? "bg-accent/15 text-text-secondary" : "text-text-tertiary"}`}
          >
            <span
              className={`flex h-4 w-4 items-center justify-center rounded-full text-[10px] leading-none ${
                isActive ? "bg-[#1b1d1a] text-accent" : isDone ? "bg-accent/40 text-text-secondary" : "bg-black/5 text-text-tertiary"
              }`}
            >
              {isDone ? "✓" : index + 1}
            </span>
            {statusLabel[step]}
          </div>
        );
      })}
    </div>
  );
}

export default function PromptManagementPage() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const [activeGroup, setActiveGroup] = useState<PromptGroupKey>("script");
  const [selectedTemplateId, setSelectedTemplateId] = useState("");
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [templateForm, setTemplateForm] = useState<TemplateForm>(emptyTemplate);
  const [createOpen, setCreateOpen] = useState(false);
  const [draftContent, setDraftContent] = useState("");
  const [changelog, setChangelog] = useState("");
  const [bindingForm, setBindingForm] = useState<BindingForm>(emptyBinding);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const templates = useQuery({ queryKey: ["prompts"], queryFn: () => api.prompts.list({ limit: 100 }) });
  const bindings = useQuery({
    queryKey: ["prompts", "bindings"],
    queryFn: () => api.prompts.bindings({ limit: 100 }),
  });

  const templateItems = useMemo(() => templates.data?.items ?? [], [templates.data?.items]);
  const activeGroupConfig = promptGroups.find((group) => group.key === activeGroup) ?? promptGroups[0];
  const groupedTemplateItems = useMemo(
    () => templateItems.filter((item) => item.template.purpose.startsWith(activeGroupConfig.prefix)),
    [activeGroupConfig.prefix, templateItems],
  );
  const selectedTemplate = groupedTemplateItems.find((item) => item.template.id === selectedTemplateId) ?? groupedTemplateItems[0];
  const currentTemplateId = selectedTemplate?.template.id ?? "";
  const versions = useQuery({
    queryKey: ["prompts", currentTemplateId, "versions"],
    queryFn: () => api.prompts.versions(currentTemplateId, { limit: 100 }),
    enabled: Boolean(currentTemplateId),
  });
  const versionItems = useMemo(() => versions.data?.items ?? [], [versions.data?.items]);
  const bindingItems = useMemo(() => bindings.data?.items ?? [], [bindings.data?.items]);
  const selectedVersion = versionItems.find((item) => item.version.id === selectedVersionId)?.version ?? versionItems[0]?.version;
  const publishedVersion = selectedTemplate?.published_version ?? null;
  const defaultVersion = versionItems.find((item) => item.version.id === `${currentTemplateId}_v1`)?.version;
  const selectedBindings = bindingItems.filter((item) => item.binding.prompt_template_id === selectedTemplate?.template.id);
  const selectedUsage = templateUsage(bindingItems, currentTemplateId);
  const rows = diffRows(publishedVersion?.content, selectedVersion?.content ?? draftContent);

  const selectedDesc = selectedTemplate ? describePrompt(selectedTemplate.template.purpose, selectedTemplate.template.name) : null;
  const fieldsInContent = useMemo(() => usedVariables(draftContent), [draftContent]);
  const insertableFields = useMemo(
    () => (selectedTemplate ? variableChips(selectedTemplate) : []),
    [selectedTemplate],
  );

  useEffect(() => {
    if (!groupedTemplateItems.some((item) => item.template.id === selectedTemplateId)) {
      setSelectedTemplateId(groupedTemplateItems[0]?.template.id ?? "");
    }
  }, [groupedTemplateItems, selectedTemplateId]);

  useEffect(() => {
    const first = versionItems[0]?.version;
    setSelectedVersionId((current) => (current && versionItems.some((item) => item.version.id === current) ? current : first?.id ?? ""));
  }, [versionItems]);

  useEffect(() => {
    if (selectedTemplate) {
      setDraftContent(publishedVersion?.content ?? selectedVersion?.content ?? "");
      setBindingForm((current) => ({ ...current, node_id: current.node_id || selectedTemplate.template.purpose }));
    }
  }, [publishedVersion?.content, selectedTemplate, selectedVersion?.content]);

  const invalidatePrompts = async () => {
    await queryClient.invalidateQueries({ queryKey: ["prompts"] });
  };

  const createTemplate = useMutation({
    mutationFn: () =>
      api.prompts.create({
        name: templateForm.name.trim(),
        purpose: templateForm.purpose.trim(),
        variables_schema_ref: { schema_id: templateForm.variables_schema_id.trim(), schema_version: "v1" },
        output_schema_ref: { schema_id: templateForm.output_schema_id.trim(), schema_version: "v1" },
      }),
    onSuccess: async (created) => {
      setTemplateForm(emptyTemplate);
      setCreateOpen(false);
      const createdGroup = promptGroups.find((group) => created.template.purpose.startsWith(group.prefix));
      if (createdGroup) setActiveGroup(createdGroup.key);
      setSelectedTemplateId(created.template.id);
      await invalidatePrompts();
      toast.success("提示词已创建", created.template.name);
    },
    onError: (error: ApiError) => toast.error("创建失败", error),
  });

  const createVersion = useMutation({
    mutationFn: () =>
      api.prompts.createVersion(currentTemplateId, {
        content: draftContent,
        changelog: changelog.trim() || null,
      }),
    onSuccess: async (created) => {
      setChangelog("");
      setSelectedVersionId(created.version.id);
      await invalidatePrompts();
      toast.success("草稿已保存", "尚未发布到生产");
    },
    onError: (error: ApiError) => toast.error("保存失败", error),
  });

  const saveAndPublish = useMutation({
    mutationFn: async () => {
      const created = await api.prompts.createVersion(currentTemplateId, {
        content: draftContent,
        changelog: changelog.trim() || "提示词页编辑",
      });
      const versionId = created.version.id;
      await api.prompts.approveVersion(currentTemplateId, versionId, { reason: "提示词页编辑" });
      await api.prompts.publishVersion(currentTemplateId, versionId, { reason: "提示词页编辑" });
      const templateBindings = bindingItems.filter((item) => item.binding.prompt_template_id === currentTemplateId);
      await Promise.all(
        templateBindings.map((item) => api.prompts.patchBinding(item.binding.id, { prompt_version_id: versionId })),
      );
      return { versionId, rebound: templateBindings.length };
    },
    onSuccess: async ({ rebound }) => {
      setChangelog("");
      await invalidatePrompts();
      await queryClient.invalidateQueries({ queryKey: ["prompts", "bindings"] });
      await queryClient.invalidateQueries({ queryKey: ["prompts", currentTemplateId, "versions"] });
      toast.success(
        "已发布到生产",
        rebound > 0 ? `已让 ${rebound} 个生产环节使用新内容` : "已保存为已发布版本（该提示词尚未接入生产）",
      );
    },
    onError: (error: ApiError) => toast.error("发布失败", error),
  });

  const approve = useMutation({
    mutationFn: (versionId: string) => api.prompts.approveVersion(currentTemplateId, versionId, { reason: "ops approval" }),
    onSuccess: async () => {
      await invalidatePrompts();
      toast.success("版本已审批");
    },
    onError: (error: ApiError) => toast.error("审批失败", error),
  });

  const publish = useMutation({
    mutationFn: (versionId: string) => api.prompts.publishVersion(currentTemplateId, versionId, { reason: "ops publish" }),
    onSuccess: async () => {
      await invalidatePrompts();
      toast.success("版本已发布");
    },
    onError: (error: ApiError) => toast.error("发布失败", error),
  });

  const rollback = useMutation({
    mutationFn: (versionId: string) =>
      api.prompts.rollback(currentTemplateId, { target_version_id: versionId, reason: "restore default v1" }),
    onSuccess: async () => {
      await invalidatePrompts();
      toast.success("已恢复默认版本");
    },
    onError: (error: ApiError) => toast.error("回滚失败", error),
  });

  const createBinding = useMutation({
    mutationFn: () =>
      api.prompts.createBinding({
        prompt_template_id: currentTemplateId,
        prompt_version_id: selectedVersion?.id || publishedVersion?.id || "",
        case_id: bindingForm.case_id.trim() || null,
        node_id: bindingForm.node_id.trim() || null,
        priority: bindingForm.priority,
      }),
    onSuccess: async () => {
      setBindingForm(emptyBinding);
      await queryClient.invalidateQueries({ queryKey: ["prompts", "bindings"] });
      toast.success("绑定已创建");
    },
    onError: (error: ApiError) => toast.error("绑定失败", error),
  });

  const patchBinding = useMutation({
    mutationFn: (binding: PromptBindingView) =>
      api.prompts.patchBinding(binding.binding.id, { enabled: !binding.binding.enabled }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["prompts", "bindings"] });
      toast.success("绑定已更新");
    },
    onError: (error: ApiError) => toast.error("更新失败", error),
  });

  const insertVariable = (name: string) => {
    const useDouble = draftContent.includes("{{") || !draftContent.includes("{");
    const token = useDouble ? `{{${name}}}` : `{${name}}`;
    const editor = editorRef.current;
    if (!editor) {
      setDraftContent((value) => `${value}${value.endsWith(" ") || !value ? "" : " "}${token}`);
      return;
    }
    const start = editor.selectionStart ?? draftContent.length;
    const end = editor.selectionEnd ?? start;
    const next = `${draftContent.slice(0, start)}${token}${draftContent.slice(end)}`;
    const cursor = start + token.length;
    setDraftContent(next);
    window.requestAnimationFrame(() => {
      editor.focus();
      editor.setSelectionRange(cursor, cursor);
    });
  };

  const createDisabled = createTemplate.isPending || !templateForm.name.trim() || !templateForm.purpose.trim();
  const busy = saveAndPublish.isPending || createVersion.isPending;

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>提示词</h1>
          <p>这里管理 AI 在各个环节使用的提示词。直接编辑内容，点「保存并发布到生产」即可让线上立刻使用新版本。</p>
        </div>
      </header>

      <div className="flex flex-wrap gap-2" role="tablist" aria-label="提示词分组">
        {promptGroups.map((group) => {
          const count = templateItems.filter((item) => item.template.purpose.startsWith(group.prefix)).length;
          const active = group.key === activeGroup;
          return (
            <button
              aria-selected={active}
              className={`rounded-full border px-3 py-1.5 text-sm transition-colors ${active ? "border-accent bg-accent/15 text-text-primary" : "border-border/70 bg-white/70 text-text-secondary hover:bg-surface-hover"}`}
              key={group.key}
              role="tab"
              type="button"
              onClick={() => setActiveGroup(group.key)}
            >
              {group.label} · {count}
            </button>
          );
        })}
      </div>

      {templates.isLoading ? <LoadingState /> : null}
      {templates.error ? <ErrorState error={templates.error} /> : null}
      {!templates.isLoading && templateItems.length === 0 ? <EmptyState title="暂无提示词" detail="创建后即可编辑并发布。" /> : null}

      <div className="grid gap-5 xl:grid-cols-[320px_minmax(0,1fr)]">
        <aside className="grid content-start gap-3">
          <div className="card flex items-center justify-between gap-3 p-4">
            <div className="min-w-0">
              <p className="text-sm font-semibold text-text-primary">{activeGroupConfig.label}</p>
              <p className="text-xs text-text-tertiary">{groupedTemplateItems.length} 个提示词</p>
            </div>
            <button className="btn-primary shrink-0 px-3 py-2 text-sm" type="button" onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" />
              <span>新建</span>
            </button>
          </div>

          <div className="card max-h-[calc(100vh-16rem)] overflow-y-auto p-0 xl:max-h-[calc(100vh-13rem)]">
            <div className="divide-y divide-border/60">
              {groupedTemplateItems.map((item) => {
                const usage = templateUsage(bindingItems, item.template.id);
                const isSelected = selectedTemplate?.template.id === item.template.id;
                const desc = describePrompt(item.template.purpose, item.template.name);
                return (
                  <button
                    className={`block w-full px-4 py-3 text-left transition-colors hover:bg-surface-hover ${isSelected ? "bg-accent/10" : ""}`}
                    key={item.template.id}
                    type="button"
                    onClick={() => setSelectedTemplateId(item.template.id)}
                  >
                    <UsageBadge usage={usage} compact />
                    <span className="mt-2 block truncate text-sm font-semibold text-text-primary">{desc.title}</span>
                    <span className="mt-0.5 block truncate text-xs text-text-tertiary">{desc.usage}</span>
                  </button>
                );
              })}
              {groupedTemplateItems.length === 0 ? (
                <div className="px-4 py-8 text-center text-sm text-text-secondary">
                  当前分组暂无提示词。
                  <button className="mt-3 block w-full text-accent hover:underline" type="button" onClick={() => setCreateOpen(true)}>
                    + 新建第一个
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </aside>

        {selectedTemplate && selectedDesc ? (
          <div className="grid gap-5">
            <div className="card grid gap-5 p-5">
              <div className="grid gap-2">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-xl font-semibold text-text-primary">{selectedDesc.title}</h2>
                  <UsageBadge usage={selectedUsage} compact />
                </div>
                <p className="text-sm text-text-secondary">{selectedDesc.usage}</p>
                <p className="text-xs text-text-tertiary">
                  {selectedUsage.inProduction ? (
                    <>当前生产环节正在使用本提示词。编辑后点「保存并发布到生产」即可让线上使用新内容。</>
                  ) : (
                    <>本提示词尚未接入生产环节。可先编辑保存，接入生产请展开下方「高级设置」。</>
                  )}
                </p>
              </div>

              <label className="grid gap-2">
                <span>提示词内容</span>
                <textarea
                  ref={editorRef}
                  className="text-sm leading-relaxed"
                  rows={16}
                  value={draftContent}
                  onChange={(event) => setDraftContent(event.target.value)}
                />
              </label>

              {insertableFields.length > 0 ? (
                <div className="grid gap-2">
                  <span className="text-xs font-semibold text-text-secondary">点击插入动态字段</span>
                  <div className="flex flex-wrap gap-2">
                    {insertableFields.map((name) => (
                      <button
                        className="inline-flex items-center gap-1 rounded-full bg-accent/10 px-3 py-1 text-xs text-accent transition-colors hover:bg-accent/20"
                        type="button"
                        key={name}
                        title={`插入 ${variableLabel(name)}`}
                        onClick={() => insertVariable(name)}
                      >
                        <Plus className="h-3 w-3" />
                        {variableLabel(name)}
                      </button>
                    ))}
                  </div>
                </div>
              ) : null}

              {fieldsInContent.length > 0 ? (
                <div className="grid gap-2 rounded-2xl border border-border/70 bg-surface-hover/40 p-4">
                  <p className="flex items-center gap-1.5 text-xs font-semibold text-text-secondary">
                    <Info className="h-3.5 w-3.5 text-accent" />
                    内容里的「字段」说明（运行时自动填充，无需手动填写）
                  </p>
                  <div className="grid gap-1.5 sm:grid-cols-2">
                    {fieldsInContent.map((name) => (
                      <div className="flex items-center gap-2 text-xs" key={name}>
                        <code className="shrink-0 rounded bg-black/5 px-1.5 py-0.5 font-mono text-text-tertiary">{name}</code>
                        <span className="text-text-secondary">{variableLabel(name)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}

              <div className="grid gap-3 border-t border-border/60 pt-4">
                <label className="grid gap-1.5">
                  <span>修改说明（可选）</span>
                  <input value={changelog} onChange={(event) => setChangelog(event.target.value)} placeholder="本次改了什么，便于以后回看" />
                </label>
                <div className="flex flex-wrap gap-2">
                  <button
                    className="btn-primary"
                    type="button"
                    disabled={!draftContent.trim() || !currentTemplateId || busy}
                    onClick={() => saveAndPublish.mutate()}
                  >
                    <Rocket className="h-4 w-4" />
                    <span>{saveAndPublish.isPending ? "发布中…" : "保存并发布到生产"}</span>
                  </button>
                  <button
                    className="btn-secondary"
                    type="button"
                    disabled={!draftContent.trim() || !currentTemplateId || busy}
                    onClick={() => createVersion.mutate()}
                  >
                    <Save className="h-4 w-4" />
                    <span>仅存草稿</span>
                  </button>
                </div>
              </div>
            </div>

            <div className="card overflow-hidden p-0">
              <button
                type="button"
                onClick={() => setShowAdvanced((value) => !value)}
                className="flex w-full items-center justify-between px-5 py-3.5 text-sm transition-colors hover:bg-surface-hover"
              >
                <span className="flex items-center gap-2 font-semibold text-text-primary">
                  <Settings2 className="h-4 w-4 text-accent" />
                  高级设置（开发者）
                  <span className="font-normal text-text-tertiary">版本对比 · 审批发布 · 生产绑定</span>
                </span>
                {showAdvanced ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
              </button>
              {showAdvanced ? (
                <div className="grid gap-5 border-t border-border/70 p-5">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="inline-flex items-center rounded-full border border-border/70 bg-white/70 px-3 py-1 font-mono text-text-secondary">
                      用途 {selectedTemplate.template.purpose}
                    </span>
                    <span className="inline-flex items-center rounded-full border border-border/70 bg-white/70 px-3 py-1 font-mono text-text-secondary">
                      变量 {schemaText(selectedTemplate.template.variables_schema_ref)}
                    </span>
                    <span className="inline-flex items-center rounded-full border border-border/70 bg-white/70 px-3 py-1 font-mono text-text-secondary">
                      输出 {schemaText(selectedTemplate.template.output_schema_ref)}
                    </span>
                  </div>
                  <div className="flex flex-wrap items-center gap-3">
                    <span className="text-xs font-semibold text-text-secondary">当前版本状态</span>
                    <StatusStepper status={selectedVersion?.status} />
                  </div>

                  <div className="grid gap-5 xl:grid-cols-2">
                    <div className="grid content-start gap-4">
                      <div className="flex items-center gap-2">
                        <GitCompare className="h-4 w-4 text-accent" />
                        <h3 className="font-semibold text-text-primary">版本对比</h3>
                      </div>
                      <label className="grid gap-1.5">
                        <span>对比版本（相对已发布版本）</span>
                        <select value={selectedVersionId} onChange={(event) => setSelectedVersionId(event.target.value)}>
                          {versionItems.map((item) => (
                            <option key={item.version.id} value={item.version.id}>
                              {item.version.id} · {statusLabel[item.version.status] ?? item.version.status}
                            </option>
                          ))}
                        </select>
                      </label>
                      <div className="max-h-[300px] min-h-[200px] overflow-auto rounded-2xl border border-border/70 bg-[#111511] p-3 font-mono text-xs text-white">
                        {rows.length > 0 ? (
                          rows.map((row, index) => (
                            <p
                              key={`${row.kind}-${index}`}
                              className={row.kind === "add" ? "text-status-success" : row.kind === "remove" ? "text-status-error" : "text-white/70"}
                            >
                              {row.kind === "add" ? "+ " : row.kind === "remove" ? "- " : "  "}
                              {row.text}
                            </p>
                          ))
                        ) : (
                          <p className="text-white/60">无差异</p>
                        )}
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <button
                          className="btn-secondary"
                          type="button"
                          disabled={!selectedVersion || approve.isPending || !["draft", "reviewing"].includes(selectedVersion.status)}
                          onClick={() => selectedVersion && approve.mutate(selectedVersion.id)}
                        >
                          <CheckCircle2 className="h-4 w-4" />
                          <span>审批</span>
                        </button>
                        <button
                          className="btn-secondary"
                          type="button"
                          disabled={!selectedVersion || publish.isPending || selectedVersion.status !== "approved"}
                          onClick={() => selectedVersion && publish.mutate(selectedVersion.id)}
                        >
                          <Send className="h-4 w-4" />
                          <span>发布</span>
                        </button>
                        <button
                          className="btn-secondary"
                          type="button"
                          disabled={!defaultVersion || rollback.isPending}
                          onClick={() => defaultVersion && rollback.mutate(defaultVersion.id)}
                        >
                          <RotateCcw className="h-4 w-4" />
                          <span>恢复默认</span>
                        </button>
                      </div>
                    </div>

                    <div className="grid content-start gap-4">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-2">
                          <Link2 className="h-4 w-4 text-accent" />
                          <h3 className="font-semibold text-text-primary">生产绑定</h3>
                        </div>
                        <UsageBadge usage={selectedUsage} />
                      </div>

                      <div className="grid gap-2">
                        {selectedBindings.length > 0 ? (
                          selectedBindings.map((item) => (
                            <div
                              className="flex items-center justify-between gap-3 rounded-2xl border border-border/60 bg-white/55 px-3 py-2.5 transition-colors hover:bg-white/75"
                              key={item.binding.id}
                            >
                              <div className="min-w-0">
                                <p className="flex items-center gap-2 truncate text-sm font-semibold text-text-primary">
                                  <span className="truncate">{item.binding.node_id || "全局节点"}</span>
                                  <span
                                    className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${
                                      item.binding.enabled ? "bg-status-success/15 text-status-success" : "bg-black/5 text-text-tertiary"
                                    }`}
                                  >
                                    {item.binding.enabled ? "已启用" : "已停用"}
                                  </span>
                                </p>
                                <p className="mt-0.5 truncate font-mono text-xs text-text-tertiary">
                                  {item.binding.case_id || "全局 Case"} · P{item.binding.priority} ·{" "}
                                  {item.resolved_version?.id ?? item.binding.prompt_version_id}
                                </p>
                                <p className="mt-0.5 text-[11px] text-text-tertiary">
                                  更新于 <TimeText value={item.binding.updated_at} />
                                </p>
                              </div>
                              <button
                                className="icon-button"
                                type="button"
                                onClick={() => patchBinding.mutate(item)}
                                disabled={patchBinding.isPending}
                                aria-label={item.binding.enabled ? "停用绑定" : "启用绑定"}
                                title={item.binding.enabled ? "停用绑定" : "启用绑定"}
                              >
                                {item.binding.enabled ? (
                                  <ToggleRight className="h-5 w-5 text-status-success" />
                                ) : (
                                  <ToggleLeft className="h-5 w-5" />
                                )}
                              </button>
                            </div>
                          ))
                        ) : (
                          <div className="rounded-2xl border border-dashed border-border/70 bg-white/40 px-4 py-5 text-center text-sm text-text-secondary">
                            该提示词暂无绑定 — 未接入生产
                          </div>
                        )}
                      </div>

                      <form
                        className="grid gap-3 border-t border-border/60 pt-4"
                        onSubmit={(event) => {
                          event.preventDefault();
                          createBinding.mutate();
                        }}
                      >
                        <p className="text-xs font-semibold text-text-secondary">
                          新建绑定（绑定当前选中版本：{selectedVersion?.id || publishedVersion?.id || "—"}）
                        </p>
                        <div className="twoCol">
                          <label className="grid gap-1.5">
                            <span>节点</span>
                            <input
                              value={bindingForm.node_id}
                              onChange={(event) => setBindingForm((value) => ({ ...value, node_id: event.target.value }))}
                            />
                          </label>
                          <label className="grid gap-1.5">
                            <span>Case ID</span>
                            <input
                              value={bindingForm.case_id}
                              onChange={(event) => setBindingForm((value) => ({ ...value, case_id: event.target.value }))}
                              placeholder="可留空 = 全局"
                            />
                          </label>
                        </div>
                        <label className="grid gap-1.5">
                          <span>优先级</span>
                          <input
                            type="number"
                            value={bindingForm.priority}
                            onChange={(event) => setBindingForm((value) => ({ ...value, priority: Number(event.target.value) }))}
                          />
                        </label>
                        <button
                          className="btn-secondary"
                          type="submit"
                          disabled={createBinding.isPending || (!selectedVersion && !publishedVersion)}
                        >
                          <Plus className="h-4 w-4" />
                          <span>新建绑定</span>
                        </button>
                      </form>
                    </div>
                  </div>
                </div>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>

      {createOpen ? (
        <Modal isOpen title="新建提示词" onClose={() => setCreateOpen(false)} size="md">
          <form
            className="formGrid"
            onSubmit={(event) => {
              event.preventDefault();
              createTemplate.mutate();
            }}
          >
            <label className="grid gap-1.5">
              <span>名称</span>
              <input
                value={templateForm.name}
                onChange={(event) => setTemplateForm((value) => ({ ...value, name: event.target.value }))}
                placeholder="例如：分镜脚本生成"
                required
                autoFocus
              />
            </label>
            <label className="grid gap-1.5">
              <span>能力 / 用途标识</span>
              <input
                value={templateForm.purpose}
                onChange={(event) => setTemplateForm((value) => ({ ...value, purpose: event.target.value }))}
                placeholder="例如：prompt.script.storyboard"
                required
              />
              <small className="text-xs font-normal text-text-tertiary">
                以 {activeGroupConfig.prefix} 开头将归入「{activeGroupConfig.label}」分组。
              </small>
            </label>
            <div className="twoCol">
              <label className="grid gap-1.5">
                <span>变量 schema</span>
                <input
                  value={templateForm.variables_schema_id}
                  onChange={(event) => setTemplateForm((value) => ({ ...value, variables_schema_id: event.target.value }))}
                  required
                />
              </label>
              <label className="grid gap-1.5">
                <span>输出 schema</span>
                <input
                  value={templateForm.output_schema_id}
                  onChange={(event) => setTemplateForm((value) => ({ ...value, output_schema_id: event.target.value }))}
                  required
                />
              </label>
            </div>
            <div className="formActions justify-end">
              <button className="btn-secondary" type="button" onClick={() => setCreateOpen(false)}>
                取消
              </button>
              <button className="btn-primary" type="submit" disabled={createDisabled}>
                <Plus className="h-4 w-4" />
                <span>创建</span>
              </button>
            </div>
          </form>
        </Modal>
      ) : null}
    </section>
  );
}
