import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  ClipboardList,
  Download,
  Loader2,
  Plus,
  RefreshCw,
  Send,
  Trash2,
  Wand2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, type ApiError, type FinishedVideo, type RunCard } from "../../api/client";
import { caseAgentApi } from "../../api/r6";
import type { components } from "../../api/schema";
import { StudioTabs } from "../../components/StudioTabs";
import { useScriptToolbox } from "../../components/script-tools/useScriptToolbox";
import { EmptyState, ErrorState, LoadingState } from "../../components/ui/State";
import { StatusPill } from "../../components/ui/StatusPill";
import { useToast } from "../../components/ui/Toast";
import {
  BATCH_MAX_ITEMS,
  batchLipsyncEnabled,
  batchSubtitleLayerFlags,
  parsePastedScripts,
  summarizeBatchResults,
} from "../../components/studio-create/batchModel";
import { usePageVisible } from "../../hooks/usePageVisible";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { triggerDownload } from "../../lib/download";
import { shortId } from "../../lib/format";
import { routes } from "../../routes";

type BatchRequest = components["schemas"]["BatchDigitalHumanVideoRequest"];
type BatchItem = components["schemas"]["BatchItem"];
type BatchItemOverrides = components["schemas"]["BatchItemOverrides"];
type BatchItemResult = components["schemas"]["BatchItemResult"];
type RunStatus = components["schemas"]["RunStatus"];

type RowState =
  | "draft"
  | "polished"
  | "adopted"
  | "queued"
  | "running"
  | "succeeded"
  | "failed";

type BatchRow = {
  id: string;
  title: string;
  script: string;
  selected: boolean;
  state: RowState;
  draftId?: string;
  draftTitle?: string;
  draftScript?: string;
  jobId?: string | null;
  runId?: string | null;
  error?: string | null;
};

const workflowOptions = [
  { value: "digital_human_v2", label: "主链" },
  { value: "digital_human_editing_agent_v2", label: "剪辑 Agent" },
  { value: "seedance_t2v_v1", label: "Seedance" },
] as const;

function newRow(script = "", title = ""): BatchRow {
  const id = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}_${Math.random()}`;
  return {
    id,
    title,
    script,
    selected: true,
    state: script.trim() ? "adopted" : "draft",
  };
}

export default function BatchWorkbenchPage() {
  const { caseId = "" } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const queryClient = useQueryClient();
  const pageVisible = usePageVisible();
  const toolbox = useScriptToolbox(caseId);
  const [rows, setRows] = useState<BatchRow[]>(() => [newRow()]);
  const [pasteText, setPasteText] = useState("");
  const [useMyDefaults, setUseMyDefaults] = useState(true);
  const [workflowTemplate, setWorkflowTemplate] = useState<(typeof workflowOptions)[number]["value"]>("digital_human_v2");
  const [brollMode, setBrollMode] = useState<"insert" | "full_coverage">("insert");
  const [subtitleEnabled, setSubtitleEnabled] = useState(true);
  const [bgmEnabled, setBgmEnabled] = useState(true);
  const [selectedVoice, setSelectedVoice] = useState("");
  const hydratedDefaults = useRef(false);

  const updateRow = useCallback((id: string, patch: Partial<BatchRow>) => {
    setRows((current) => current.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  }, []);

  const selectedRows = useMemo(() => rows.filter((row) => row.selected), [rows]);
  const runnableRows = useMemo(() => selectedRows.filter((row) => row.script.trim()), [selectedRows]);
  const runIds = rows.map((row) => row.runId).filter(Boolean) as string[];
  const estimatedDuration = Math.max(
    8,
    Math.round(runnableRows.reduce((sum, row) => sum + row.script.trim().length / 5.6, 0)),
  );
  const debouncedDuration = useDebouncedValue(estimatedDuration, 500);

  const caseDetail = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => api.cases.detail(caseId),
    enabled: Boolean(caseId),
  });
  const voices = useQuery({
    queryKey: ["voices", caseId],
    queryFn: () => api.voices.list({ case_id: caseId, enabled: true, limit: 200 }),
    enabled: Boolean(caseId),
  });
  const generationDefaults = useQuery({
    queryKey: ["me", "generation-defaults"],
    queryFn: () => api.me.getGenerationDefaults(),
  });
  const feasibility = useQuery({
    queryKey: ["batch-feasibility", caseId, debouncedDuration],
    queryFn: () => api.jobs.batchFeasibility(caseId, { estimated_audio_duration_sec: debouncedDuration }),
    enabled: Boolean(caseId),
    refetchInterval: pageVisible ? 15000 : false,
  });
  const runOverview = useQuery({
    queryKey: ["run-overview", runIds.join(",")],
    queryFn: () => api.runs.overview({ ids: runIds.join(","), limit: Math.max(runIds.length, 1) }),
    enabled: runIds.length > 0,
    refetchInterval: pageVisible ? 8000 : false,
  });
  const finishedVideos = useQuery({
    queryKey: ["finished-videos", caseId, "batch-workbench"],
    queryFn: () => api.finishedVideos.list(caseId, { limit: 100 }),
    enabled: Boolean(caseId),
    refetchInterval: pageVisible ? 12000 : false,
  });

  // Hydrate the option panel from the user's saved generation defaults once, on
  // first load. Only the batch panel's exposed controls are seeded here; the
  // richer numeric/style fields ride along inside buildOverrides so that a
  // submit merges "user default + explicit panel change" instead of clobbering
  // saved defaults with hardcoded constants.
  useEffect(() => {
    if (hydratedDefaults.current || !generationDefaults.data) return;
    hydratedDefaults.current = true;
    const defaults = generationDefaults.data;
    if (defaults.broll?.mode) setBrollMode(defaults.broll.mode === "full_coverage" ? "full_coverage" : "insert");
    if (defaults.subtitle) {
      const normalEnabled = defaults.subtitle.normal_enabled ?? true;
      const emphasisEnabled = defaults.subtitle.emphasis_enabled ?? true;
      setSubtitleEnabled(Boolean(defaults.subtitle.enabled && (normalEnabled || emphasisEnabled)));
    }
    if (defaults.bgm) setBgmEnabled(Boolean(defaults.bgm.enabled));
    if (defaults.voice?.voice_id) setSelectedVoice(defaults.voice.voice_id);
  }, [generationDefaults.data]);

  useEffect(() => {
    const firstVoice = voices.data?.items.find((voice) => voice.enabled);
    if (!selectedVoice && firstVoice) setSelectedVoice(firstVoice.id);
  }, [selectedVoice, voices.data?.items]);

  useEffect(() => {
    const byId = new Map((runOverview.data?.items ?? []).map((run) => [run.runId, run]));
    if (byId.size === 0) return;
    setRows((current) =>
      current.map((row) => {
        if (!row.runId) return row;
        const run = byId.get(row.runId);
        if (!run) return row;
        const nextState = stateFromRun(run.status);
        return row.state === nextState ? row : { ...row, state: nextState };
      }),
    );
  }, [runOverview.data?.items]);

  const polishSelected = useMutation({
    mutationFn: async () =>
      Promise.all(
        runnableRows.map(async (row, index) => ({
          rowId: row.id,
          draft: await caseAgentApi.generateScript(caseId, {
            brief: `批量工作台润色第 ${index + 1} 条脚本。\n当前脚本：${row.script.trim()}`,
            memory_ids: [],
            variation_count: 1,
            persona_mode: "hard_ad",
            operation: "polish",
            strategy_tags: [],
            reference_script: row.script.trim(),
            duration: null,
          }),
        })),
      ),
    onSuccess: (values) => {
      setRows((current) =>
        current.map((row) => {
          const hit = values.find((item) => item.rowId === row.id);
          if (!hit) return row;
          return {
            ...row,
            state: "polished",
            draftId: hit.draft.id,
            draftTitle: hit.draft.title || row.title || "润色脚本",
            draftScript: hit.draft.script,
          };
        }),
      );
      toast.success("批量润色完成", `${values.length} 条草稿可采纳`);
    },
    onError: (error: ApiError) => toast.error("批量润色失败", error),
  });

  const submitBatch = useMutation({
    mutationFn: (rowsSnapshot: BatchRow[]) =>
      api.jobs.createDigitalHumanVideoBatch(buildBatchPayload(rowsSnapshot)),
    onSuccess: async (response, rowsSnapshot) => {
      // Map results back by the submitted-row snapshot index, never by the live
      // selection — otherwise toggling a checkbox while the request is in flight
      // would misattribute job/run ids to the wrong row.
      const resultByRowId = new Map<string, BatchItemResult>();
      for (const result of response.results) {
        const row = rowsSnapshot[result.index];
        if (row) resultByRowId.set(row.id, result);
      }
      setRows((current) =>
        current.map((row) => {
          const result = resultByRowId.get(row.id);
          if (!result) return row;
          if (result.status === "failed") {
            return { ...row, state: "failed", error: result.error || "提交失败" };
          }
          return {
            ...row,
            state: "queued",
            jobId: result.job_id,
            runId: result.run_id,
            error: null,
          };
        }),
      );
      const { created, failed } = summarizeBatchResults(response.results);
      toast.success("批量生产已入队", failed ? `成功 ${created} 条，失败 ${failed} 条` : `${created} 条等待 admission`);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["run-overview"] }),
        queryClient.invalidateQueries({ queryKey: ["finished-videos", caseId] }),
      ]);
    },
    onError: (error: ApiError) => toast.error("批量入队失败", error),
  });

  const downloadSelected = useMutation({
    mutationFn: () => api.finishedVideos.batchDownloads(caseId, { ids: selectedFinishedVideos().map((video) => video.id).join(",") }),
    onSuccess: (response) => {
      let started = 0;
      for (const item of response.items) {
        if (triggerDownload(item.url, item.filename)) started += 1;
      }
      toast.success("已开始批量下载", `${started} / ${response.items.length} 个裸 MP4`);
    },
    onError: (error: ApiError) => toast.error("批量下载失败", error),
  });

  const sendToPublish = useMutation({
    mutationFn: async () => {
      const packages = [];
      for (const video of selectedFinishedVideos()) {
        packages.push(
          await api.publishing.createPackage({
            source_finished_video_id: video.id,
            title: video.title || video.id,
            description: "",
          }),
        );
      }
      return api.publishing.createBatch({
        publish_package_ids: packages.map((item) => item.id),
        platform_targets: ["douyin"],
      });
    },
    onSuccess: (batch) => {
      toast.success("已送入发布中心", shortId(batch.id));
      navigate(`${routes.casePublish(caseId)}?batch=${encodeURIComponent(batch.id)}`);
    },
    onError: (error: ApiError) => toast.error("送入发布中心失败", error),
  });

  function buildOverrides(): BatchItemOverrides {
    const isSeedance = workflowTemplate === "seedance_t2v_v1";
    // Fields the batch panel does not expose are seeded from the user's saved
    // defaults (with the previous constants as fallback) so an override block is
    // "user default + explicit panel change" rather than a blanket clobber.
    const defaults = generationDefaults.data;
    const voiceDefaults = defaults?.voice;
    const brollDefaults = defaults?.broll;
    const subtitleDefaults = defaults?.subtitle;
    const subtitleFontSize = subtitleDefaults?.font_size;
    const subtitleEmphasisFontSize = subtitleDefaults?.emphasis_font_size;
    const subtitleNormalEnabled = subtitleDefaults?.normal_enabled ?? true;
    const subtitleEmphasisEnabled = subtitleDefaults?.emphasis_enabled ?? true;
    const subtitleLayers = batchSubtitleLayerFlags(
      workflowTemplate,
      subtitleEnabled,
      subtitleNormalEnabled,
      subtitleEmphasisEnabled,
    );
    const bgmDefaults = defaults?.bgm;
    const lipsyncDefaults = defaults?.lipsync;
    return {
      workflow_template_id: workflowTemplate,
      voice: selectedVoice
        ? {
            voice_id: selectedVoice,
            speed: voiceDefaults?.speed ?? 1,
            emotion: voiceDefaults?.emotion ?? "neutral",
            volume: voiceDefaults?.volume ?? 1,
          }
        : undefined,
      broll: {
        enabled: !isSeedance,
        mode: isSeedance ? "insert" : brollMode,
        max_inserts: brollDefaults?.max_inserts ?? 4,
        min_segment_duration: brollDefaults?.min_segment_duration ?? 3,
        allow_generic_coverage: brollDefaults?.allow_generic_coverage ?? true,
      },
      subtitle: {
        ...subtitleLayers,
        style_preset: subtitleDefaults?.style_preset ?? "douyin",
        font_id: subtitleDefaults?.font_id ?? null,
        emphasis_font_id: subtitleDefaults?.emphasis_font_id ?? null,
        ...(subtitleDefaults?.emphasis_primary_color ? { emphasis_primary_color: subtitleDefaults.emphasis_primary_color } : {}),
        ...(subtitleFontSize != null ? { font_size: subtitleFontSize } : {}),
        ...(subtitleEmphasisFontSize != null ? { emphasis_font_size: subtitleEmphasisFontSize } : {}),
        ...(subtitleDefaults?.position ? { position: subtitleDefaults.position } : {}),
      },
      bgm: {
        enabled: !isSeedance && bgmEnabled,
        volume: bgmDefaults?.volume ?? 0.25,
        auto_mix: bgmDefaults?.auto_mix ?? true,
      },
      lipsync: {
        enabled: batchLipsyncEnabled(workflowTemplate, brollMode),
        provider_profile_id: lipsyncDefaults?.provider_profile_id ?? "runninghub.heygem.prod",
        timeout_minutes: lipsyncDefaults?.timeout_minutes ?? 30,
      },
      strictness: {
        strict_timestamps: false,
        portrait_insufficient_policy: "hard_fail",
      },
    };
  }

  function buildBatchPayload(rowsSnapshot: BatchRow[]): BatchRequest {
    const overrides = buildOverrides();
    const items: BatchItem[] = rowsSnapshot.map((row) => ({
      script: row.script.trim(),
      title: row.title.trim() || null,
      publish_content: null,
      script_version_id: null,
      overrides,
    }));
    return {
      schema_version: "batch_digital_human_video_request.v1",
      case_id: caseId,
      items,
      use_my_defaults: useMyDefaults,
    };
  }

  function addPastedScripts() {
    const scripts = parsePastedScripts(pasteText).slice(0, Math.max(0, BATCH_MAX_ITEMS - rows.length));
    if (scripts.length === 0) return;
    setRows((current) => [...current.filter((row) => row.script.trim()), ...scripts.map((script) => newRow(script))]);
    setPasteText("");
  }

  function importToolbox(kind: "candidates" | "history") {
    const source = kind === "candidates" ? toolbox.candidates : toolbox.history;
    const capacity = Math.max(0, BATCH_MAX_ITEMS - rows.length);
    const items = source.slice(0, capacity).map((item) => newRow(item.script, item.title));
    if (items.length === 0) {
      toast.info(kind === "candidates" ? "候选池为空" : "历史为空");
      return;
    }
    setRows((current) => [...current.filter((row) => row.script.trim()), ...items]);
    toast.success(kind === "candidates" ? "已导入候选池" : "已导入历史", `${items.length} 条`);
  }

  // Adopting a polished draft is the reward signal the self-evolution loop reads;
  // it must POST the adopt endpoint per draft, not merely copy text locally. Each
  // row is adopted independently so one failure toasts without swallowing the rest.
  const adoptPolished = useMutation({
    mutationFn: async (rowId?: string) => {
      const targets = rows.filter((row) => (rowId ? row.id === rowId : row.selected) && row.draftScript);
      let adopted = 0;
      let failed = 0;
      for (const row of targets) {
        try {
          if (row.draftId) {
            await caseAgentApi.adoptScriptDraft(caseId, row.draftId, {
              title: row.draftTitle || row.title || null,
              publish_content: null,
            });
          }
          updateRow(row.id, {
            title: row.draftTitle || row.title,
            script: row.draftScript ?? row.script,
            draftId: undefined,
            draftTitle: undefined,
            draftScript: undefined,
            state: "adopted",
          });
          adopted += 1;
        } catch (error) {
          failed += 1;
          toast.error("采纳失败", error);
        }
      }
      return { adopted, failed };
    },
    onSuccess: ({ adopted, failed }) => {
      if (adopted > 0) {
        toast.success("采纳完成", failed ? `成功 ${adopted} 条，失败 ${failed} 条` : `${adopted} 条已采纳`);
      }
    },
  });

  function selectedFinishedVideos(): FinishedVideo[] {
    const videos = finishedVideos.data?.items ?? [];
    const runSet = new Set(selectedRows.map((row) => row.runId).filter(Boolean));
    return videos.filter((video) => video.run_id && runSet.has(video.run_id));
  }

  function rowRun(row: BatchRow): RunCard | undefined {
    return (runOverview.data?.items ?? []).find((run) => run.runId === row.runId);
  }

  const finishedCount = selectedFinishedVideos().length;
  const selectedCanRender = runnableRows.length > 0 && (Boolean(selectedVoice) || useMyDefaults);

  if (!caseId) return <EmptyState title="未选择案例" detail="请从案例中心进入工作台。" />;

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>批量工作台</h1>
          <p>{caseDetail.data?.name ?? "批量脚本、生产和交付队列"}</p>
        </div>
      </header>
      <StudioTabs caseId={caseId} />

      {[caseDetail, voices, feasibility, runOverview, finishedVideos].map((query, index) =>
        query.error ? <ErrorState error={query.error} key={index} /> : null,
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <section className="card grid gap-4">
          <div className="sectionHeader">
            <div>
              <h2>脚本行</h2>
              <p>{rows.length} / {BATCH_MAX_ITEMS} 条 · 已选 {selectedRows.length} 条</p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button className="btn-secondary" type="button" onClick={() => setRows((current) => [...current, newRow()])}>
                <Plus className="h-4 w-4" />
                <span>空白行</span>
              </button>
              <button className="btn-secondary" type="button" onClick={() => importToolbox("candidates")}>
                <ClipboardList className="h-4 w-4" />
                <span>候选池</span>
              </button>
              <button className="btn-secondary" type="button" onClick={() => importToolbox("history")}>
                <RefreshCw className="h-4 w-4" />
                <span>历史</span>
              </button>
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_180px]">
            <textarea
              className="min-h-[92px] rounded-xl border border-border bg-white/70 p-3 text-sm outline-none transition focus:border-accent"
              value={pasteText}
              onChange={(event) => setPasteText(event.target.value)}
              placeholder="粘贴多条脚本，空行分隔"
            />
            <button className="btn-primary self-stretch" type="button" disabled={!pasteText.trim()} onClick={addPastedScripts}>
              <ClipboardList className="h-4 w-4" />
              <span>粘贴入表</span>
            </button>
          </div>

          <div className="overflow-x-auto">
            <table className="min-w-[860px] w-full text-left text-sm">
              <thead className="border-b border-border/70 text-xs text-text-tertiary">
                <tr>
                  <th className="w-10 py-2">
                    <input
                      type="checkbox"
                      checked={rows.length > 0 && rows.every((row) => row.selected)}
                      onChange={(event) => setRows((current) => current.map((row) => ({ ...row, selected: event.target.checked })))}
                    />
                  </th>
                  <th className="w-[180px] py-2">标题</th>
                  <th className="py-2">脚本</th>
                  <th className="w-[128px] py-2">状态</th>
                  <th className="w-[150px] py-2">Run</th>
                  <th className="w-[128px] py-2"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {rows.map((row) => {
                  const run = rowRun(row);
                  return (
                    <tr key={row.id} className="align-top">
                      <td className="py-3">
                        <input
                          type="checkbox"
                          checked={row.selected}
                          onChange={(event) => updateRow(row.id, { selected: event.target.checked })}
                        />
                      </td>
                      <td className="py-3 pr-3">
                        <input
                          className="w-full rounded-lg border border-border bg-white/70 px-2 py-2 text-sm outline-none focus:border-accent"
                          value={row.title}
                          onChange={(event) => updateRow(row.id, { title: event.target.value })}
                          placeholder="可选标题"
                        />
                      </td>
                      <td className="py-3 pr-3">
                        <textarea
                          className="min-h-[88px] w-full rounded-lg border border-border bg-white/70 px-2 py-2 text-sm leading-relaxed outline-none focus:border-accent"
                          value={row.script}
                          onChange={(event) => updateRow(row.id, { script: event.target.value, state: "draft" })}
                        />
                        {row.draftScript ? (
                          <div className="mt-2 rounded-lg border border-accent/25 bg-accent/5 p-2">
                            <p className="text-xs font-semibold text-accent">{row.draftTitle || "润色草稿"}</p>
                            <p className="mt-1 line-clamp-3 whitespace-pre-wrap text-xs text-text-secondary">{row.draftScript}</p>
                          </div>
                        ) : null}
                        {row.error ? <p className="mt-1 text-xs text-status-error">{row.error}</p> : null}
                      </td>
                      <td className="py-3 pr-3">
                        {run ? <StatusPill status={run.status} /> : <span className="badge-info">{stateLabel(row.state)}</span>}
                      </td>
                      <td className="py-3 pr-3">
                        {row.runId ? (
                          <code className="rounded-full bg-surface px-2 py-1 text-xs text-text-secondary">{shortId(row.runId)}</code>
                        ) : (
                          <span className="text-xs text-text-tertiary">未提交</span>
                        )}
                      </td>
                      <td className="py-3">
                        <div className="flex items-center gap-1">
                          <button
                            className="rounded-lg p-2 text-text-tertiary hover:bg-surface hover:text-text-primary"
                            type="button"
                            disabled={!row.draftScript || adoptPolished.isPending}
                            onClick={() => adoptPolished.mutate(row.id)}
                            title="采纳润色"
                          >
                            <CheckCircle2 className="h-4 w-4" />
                          </button>
                          <button
                            className="rounded-lg p-2 text-text-tertiary hover:bg-status-error/10 hover:text-status-error"
                            type="button"
                            onClick={() => setRows((current) => current.filter((item) => item.id !== row.id))}
                            title="删除"
                          >
                            <Trash2 className="h-4 w-4" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border/60 pt-4">
            <div className="flex flex-wrap gap-2">
              <button className="btn-secondary" type="button" disabled={polishSelected.isPending || runnableRows.length === 0} onClick={() => polishSelected.mutate()}>
                {polishSelected.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Wand2 className="h-4 w-4" />}
                <span>批量润色</span>
              </button>
              <button
                className="btn-secondary"
                type="button"
                disabled={adoptPolished.isPending || !rows.some((row) => row.selected && row.draftScript)}
                onClick={() => adoptPolished.mutate(undefined)}
              >
                {adoptPolished.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                <span>采纳选中草稿</span>
              </button>
            </div>
            <button className="btn-primary" type="button" disabled={!selectedCanRender || submitBatch.isPending} onClick={() => submitBatch.mutate(runnableRows)}>
              {submitBatch.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              <span>批量入队</span>
            </button>
          </div>
        </section>

        <aside className="grid content-start gap-4">
          <section className="card grid gap-4">
            <div className="sectionHeader">
              <div>
                <h2>生产选项</h2>
                <p>作为每条 item override 写入</p>
              </div>
            </div>
            <label className="grid gap-1 text-sm">
              <span className="text-xs text-text-tertiary">工作流</span>
              <select className="rounded-lg border border-border bg-white/70 px-3 py-2" value={workflowTemplate} onChange={(event) => setWorkflowTemplate(event.target.value as typeof workflowTemplate)}>
                {workflowOptions.map((item) => (
                  <option value={item.value} key={item.value}>{item.label}</option>
                ))}
              </select>
            </label>
            <label className="grid gap-1 text-sm">
              <span className="text-xs text-text-tertiary">音色</span>
              <select className="rounded-lg border border-border bg-white/70 px-3 py-2" value={selectedVoice} onChange={(event) => setSelectedVoice(event.target.value)}>
                <option value="">使用我的默认</option>
                {(voices.data?.items ?? []).map((voice) => (
                  <option value={voice.id} key={voice.id}>{voice.display_name || voice.id}</option>
                ))}
              </select>
            </label>
            <div className="grid grid-cols-2 gap-2">
              <button className={`btn-secondary ${brollMode === "insert" ? "border-accent/40" : ""}`} type="button" onClick={() => setBrollMode("insert")}>插入 B-roll</button>
              <button className={`btn-secondary ${brollMode === "full_coverage" ? "border-accent/40" : ""}`} type="button" onClick={() => setBrollMode("full_coverage")}>全覆盖</button>
            </div>
            <label className="flex items-center justify-between gap-3 text-sm">
              <span>合并我的默认</span>
              <input type="checkbox" checked={useMyDefaults} onChange={(event) => setUseMyDefaults(event.target.checked)} />
            </label>
            <label className="flex items-center justify-between gap-3 text-sm">
              <span>字幕</span>
              <input type="checkbox" checked={subtitleEnabled} onChange={(event) => setSubtitleEnabled(event.target.checked)} />
            </label>
            <label className="flex items-center justify-between gap-3 text-sm">
              <span>BGM</span>
              <input type="checkbox" checked={bgmEnabled} onChange={(event) => setBgmEnabled(event.target.checked)} />
            </label>
          </section>

          <section className="card grid gap-3">
            <div className="sectionHeader">
              <div>
                <h2>单条可行性</h2>
                <p>按当前选中脚本估算</p>
              </div>
              {feasibility.isFetching ? <Loader2 className="h-4 w-4 animate-spin text-text-tertiary" /> : null}
            </div>
            {feasibility.isLoading ? <LoadingState label="估算素材" /> : null}
            {feasibility.data ? (
              <div className="grid gap-2 text-sm">
                <Metric label="估算配音" value={`${Math.round(feasibility.data.estimatedAudioDurationSec)}s`} />
                <Metric label="人像可用" value={`${Math.round(feasibility.data.portraitDurationSec)}s`} good={feasibility.data.portraitOk} />
                <Metric label="干净 B-roll" value={`${feasibility.data.cleanBrollCandidateCount}`} good={feasibility.data.brollOk} />
                <Metric label="窗口估算" value={`${feasibility.data.estimatedBrollWindowCount}`} />
                {(feasibility.data.notes ?? []).length ? (
                  <p className="rounded-lg bg-status-warning/10 p-2 text-xs text-status-warning">{(feasibility.data.notes ?? []).join(" · ")}</p>
                ) : null}
              </div>
            ) : null}
          </section>

          <section className="card grid gap-3">
            <div className="sectionHeader">
              <div>
                <h2>队列概览</h2>
                <p>当前表格 run 聚合</p>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2">
              {Object.entries(runOverview.data?.statusCounts ?? {}).map(([status, count]) => (
                <div className="rounded-lg border border-border/70 p-2" key={status}>
                  <p className="text-xs text-text-tertiary">{status}</p>
                  <p className="text-lg font-semibold text-text-primary">{count}</p>
                </div>
              ))}
              {!runOverview.data ? <p className="col-span-2 text-sm text-text-secondary">尚无已提交 run。</p> : null}
            </div>
          </section>

          <section className="card grid gap-3">
            <div className="sectionHeader">
              <div>
                <h2>交付</h2>
                <p>选中行里已有 {finishedCount} 条成片</p>
              </div>
            </div>
            <button className="btn-secondary justify-center" type="button" disabled={finishedCount === 0 || downloadSelected.isPending} onClick={() => downloadSelected.mutate()}>
              {downloadSelected.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
              <span>裸 MP4 下载</span>
            </button>
            <button className="btn-primary justify-center" type="button" disabled={finishedCount === 0 || sendToPublish.isPending} onClick={() => sendToPublish.mutate()}>
              {sendToPublish.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              <span>全部送发布中心</span>
            </button>
          </section>
        </aside>
      </div>
    </section>
  );
}

function stateFromRun(status: RunStatus): RowState {
  if (status === "succeeded") return "succeeded";
  if (status === "failed" || status === "cancelled") return "failed";
  if (status === "running" || status === "cancelling") return "running";
  return "queued";
}

function stateLabel(state: RowState) {
  const labels: Record<RowState, string> = {
    draft: "草稿",
    polished: "已润色",
    adopted: "已采纳",
    queued: "排队中",
    running: "生产中",
    succeeded: "已完成",
    failed: "失败",
  };
  return labels[state];
}

function Metric({ label, value, good }: { label: string; value: string; good?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-border/70 p-2">
      <span className="text-xs text-text-tertiary">{label}</span>
      <span className={good === false ? "font-semibold text-status-warning" : "font-semibold text-text-primary"}>{value}</span>
    </div>
  );
}
