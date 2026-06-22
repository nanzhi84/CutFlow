import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Info, Link2, Loader2, Play } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { api, type ApiError } from "../../api/client";
import { ErrorState, LoadingState } from "../../components/State";
import { StudioTabs } from "../../components/StudioTabs";
import { useToast } from "../../components/Toast";
import {
  ConfigSummary,
  PostProcessStep,
  ProductionStep,
  ScriptStep,
  SubmitStep,
  TemplateStep,
} from "../../components/studio-create/StudioCreateSteps";
import {
  STORAGE_KEY,
  loadStoredForm,
  mapDefaultsToForm,
  mapFormToDefaults,
  steps,
  validateAll,
  validateStep,
  type FormState,
  type StudioStep,
} from "../../components/studio-create/studioCreateModel";
import { SaveDefaultsButton } from "../../components/studio-create/SaveDefaultsButton";
import { BatchScriptsModal } from "../../components/studio-create/BatchScriptsModal";
import { buildBatchRequest, summarizeBatchResults, type BatchScriptInput } from "../../components/studio-create/batchModel";
import { CandidatePoolModal } from "../../components/script-tools/CandidatePoolModal";
import { ScriptGenerateModal } from "../../components/script-tools/ScriptGenerateModal";
import { ScriptHistoryModal } from "../../components/script-tools/ScriptHistoryModal";
import { ScriptToolBar } from "../../components/script-tools/ScriptToolBar";
import { useScriptToolbox } from "../../components/script-tools/useScriptToolbox";
import type { ScriptToolItem, ScriptToolMode } from "../../components/script-tools/scriptToolModel";
import { FlowStepper } from "../../components/ui/FlowStepper";
import { routes } from "../../routes";
import { shortId } from "../../lib/format";
import type { AdoptedAgentScriptState } from "./CaseAgentPage";

type VideoJobPayload = Parameters<typeof api.jobs.createDigitalHumanVideo>[0];

export default function StudioCreatePage() {
  const { caseId = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [step, setStep] = useState<StudioStep>(0);
  const [form, setForm] = useState<FormState>(loadStoredForm);
  const [formError, setFormError] = useState<unknown>(null);
  const [scriptToolMode, setScriptToolMode] = useState<ScriptToolMode>("generate");
  const [scriptGenerateOpen, setScriptGenerateOpen] = useState(false);
  const [candidatePoolOpen, setCandidatePoolOpen] = useState(false);
  const [batchScriptsOpen, setBatchScriptsOpen] = useState(false);
  const [defaultsJustSaved, setDefaultsJustSaved] = useState(false);
  const hydratedDefaults = useRef(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [referenceUrl, setReferenceUrl] = useState("");
  const [referenceSourceTitle, setReferenceSourceTitle] = useState<string | null>(null);
  const appliedAgentSource = useRef<string | null>(null);
  const adoptedAgentScript = (location.state as AdoptedAgentScriptState | null)?.adoptedAgentScript;
  const scriptToolbox = useScriptToolbox(caseId);
  const caseDetail = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => api.cases.detail(caseId),
    enabled: Boolean(caseId),
  });
  const voices = useQuery({
    queryKey: ["voices"],
    queryFn: () => api.voices.list(),
  });
  const generationDefaults = useQuery({
    queryKey: ["me", "generation-defaults"],
    queryFn: () => api.me.getGenerationDefaults(),
  });

  // Hydrate the form from server-side "my defaults" once, on first load.
  // localStorage is the offline fallback; the server value wins when present.
  // Never overwrite an adopted-script handoff (which carries content the user
  // just chose), and don't re-run after the user has started editing.
  useEffect(() => {
    if (hydratedDefaults.current || !generationDefaults.data || adoptedAgentScript) return;
    hydratedDefaults.current = true;
    setForm((current) => mapDefaultsToForm(generationDefaults.data, current));
  }, [generationDefaults.data, adoptedAgentScript]);

  const saveDefaults = useMutation({
    mutationFn: () => api.me.putGenerationDefaults(mapFormToDefaults(form)),
    onSuccess: (saved) => {
      queryClient.setQueryData(["me", "generation-defaults"], saved);
      setDefaultsJustSaved(true);
      window.setTimeout(() => setDefaultsJustSaved(false), 2000);
      toast.success("已保存为我的默认", "下次进入工作台会自动套用");
    },
    onError: (error: ApiError) => toast.error("保存默认失败", error),
  });

  const voiceOptions = useMemo(() => voices.data?.items.filter((voice) => voice.enabled) ?? [], [voices.data?.items]);
  const selectedVoice = form.voiceId || voiceOptions[0]?.id || "";
  const selectedVoiceLabel = voiceOptions.find((voice) => voice.id === selectedVoice)?.display_name ?? selectedVoice;
  const scriptCount = form.script.trim().length;
  // Surface why "下一步" is blocked instead of leaving a silently-disabled button.
  const stepBlockMessage = validateStep(step, form, selectedVoice);

  useEffect(() => {
    // title/script/scriptVersionId are content, not preferences — never persist them
    // (a restored scriptVersionId would be orphaned since the script text is not stored).
    const { title, script, scriptVersionId, ...preferences } = form;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
  }, [form]);

  useEffect(() => {
    if (!adoptedAgentScript || appliedAgentSource.current === adoptedAgentScript.source) return;
    appliedAgentSource.current = adoptedAgentScript.source;
    setForm((current) => ({
      ...current,
      title: adoptedAgentScript.title || current.title,
      script: adoptedAgentScript.script,
      scriptVersionId: adoptedAgentScript.scriptVersionId,
    }));
    setStep(0);
    toast.info("已从案例智能体回填脚本", adoptedAgentScript.source);
  }, [adoptedAgentScript, toast]);

  useEffect(() => {
    if (!voices.data || voiceOptions.length === 0) return;
    if (!voiceOptions.some((voice) => voice.id === form.voiceId)) {
      setForm((current) => ({ ...current, voiceId: voiceOptions[0].id }));
      toast.warning("已恢复默认声音", "上次选择的声音不可用或已删除");
    }
  }, [form.voiceId, toast, voiceOptions, voices.data]);

  function buildJobPayload(
    script: string,
    title?: string | null,
    scriptVersionId: string | null = null,
  ): VideoJobPayload {
    const isBrollOnly = form.contentMode === "broll_only";
    const isSeedance = form.contentMode === "seedance";
    return {
      schema_version: "digital_human_video_request.v1",
      case_id: caseId,
      title: title?.trim() || null,
      script: script.trim(),
      publish_content: "",
      script_version_id: scriptVersionId,
      workflow_template_id: isSeedance
        ? "seedance_t2v_v1"
        : isBrollOnly
          ? "broll_only_v1"
          : "digital_human_v2",
      reference_asset_ids: isSeedance ? form.seedanceReferenceAssetIds : [],
      voice: {
        voice_id: selectedVoice,
        speed: form.speed,
        emotion: form.emotion.trim() || "neutral",
        volume: 1,
      },
      portrait: {
        template_mode: form.portraitMode,
        rhythm_preset: form.rhythmPreset,
        template_sequence_ids: [],
      },
      broll: {
        // Seedance generates the whole frame itself; B_roll-only fills with material;
        // digital-human uses the user's toggle.
        enabled: isSeedance ? false : isBrollOnly ? true : form.brollEnabled,
        max_inserts: form.maxInserts,
        min_segment_duration: 3,
        allow_generic_coverage: true,
      },
      subtitle: {
        enabled: form.subtitleEnabled,
        style_preset: form.subtitleStyle.trim() || "douyin",
        font_size: form.subtitleSize,
      },
      bgm: {
        enabled: form.bgmEnabled,
        volume: form.bgmVolume,
        auto_mix: form.bgmAutoMix,
      },
      cover: {
        mode: form.coverMode,
      },
      lipsync: {
        // B_roll-only and Seedance never run LipSync; force the block off so the
        // run-config snapshot reflects the actual workflow instead of a phantom
        // "口型同步: 开" the template can't perform.
        enabled: isBrollOnly || isSeedance ? false : form.lipsyncEnabled,
        provider_profile_id: "runninghub.heygem.prod",
        video_extension: form.lipsyncVideoExtension,
        timeout_minutes: form.lipsyncTimeoutMinutes,
      },
      strictness: {
        strict_timestamps: false,
        portrait_insufficient_policy: "hard_fail",
        broll_insufficient_policy: "soft_degrade",
        bgm_unavailable_policy: "soft_degrade",
        strict_cost_pricing: false,
      },
    };
  }

  const createJob = useMutation({
    mutationFn: () => api.jobs.createDigitalHumanVideo(buildJobPayload(form.script, form.title, form.scriptVersionId)),
    onSuccess: (data) => {
      const runId = data.initial_run?.id;
      toast.success("任务提交成功", runId ? `Run ${shortId(runId)}` : undefined);
      window.setTimeout(() => {
        navigate(runId ? `${routes.caseOutputs(caseId)}?run=${encodeURIComponent(runId)}` : routes.caseOutputs(caseId));
      }, 1500);
    },
    onError: (error: ApiError) => setFormError(error),
  });
  const extractReference = useMutation({
    mutationFn: () => api.creative.extractReference({ url: referenceUrl.trim(), language: "zh" }),
    onSuccess: (result) => {
      setForm((current) => ({
        ...current,
        title: result.title || current.title,
        script: result.reference_script,
        scriptVersionId: null,
      }));
      setReferenceSourceTitle(result.title || result.resolved_url);
      setStep(0);
      toast.success("对标视频已提取", result.title || result.platform);
    },
    onError: (error: ApiError) => toast.error("提取失败", error),
  });

  function setField<Key extends keyof FormState>(key: Key, value: FormState[Key]) {
    setForm((current) => {
      const next = { ...current, [key]: value };
      // Editing the script text by hand invalidates any adopted script_version_id —
      // the submitted version id must match the submitted text.
      if (key === "script") next.scriptVersionId = null;
      return next;
    });
  }

  function adoptScriptToolItem(item: ScriptToolItem) {
    // Script-tool items (ai/candidate/history) carry no canonical script version.
    setForm((current) => ({ ...current, title: item.title, script: item.script, scriptVersionId: null }));
    setStep(0);
    toast.info("已采用脚本", item.source === "ai" ? "AI 生成" : "候选池");
  }

  function insertHistoryItem(item: ScriptToolItem) {
    // Appending history text changes the script body, so drop any adopted version id.
    setForm((current) => ({
      ...current,
      script: current.script.trim() ? `${current.script.trim()}\n\n${item.script}` : item.script,
      scriptVersionId: null,
    }));
    setStep(0);
    setHistoryOpen(false);
    toast.info("已插入历史脚本", "内容已追加到当前脚本末尾");
  }

  const batchCreateJobs = useMutation({
    mutationFn: (inputs: BatchScriptInput[]) =>
      api.jobs.createDigitalHumanVideoBatch(buildBatchRequest(caseId, inputs, true)),
    onSuccess: (response) => {
      const { created, failed, firstRunId } = summarizeBatchResults(response.results);
      setCandidatePoolOpen(false);
      setBatchScriptsOpen(false);
      if (failed > 0) {
        toast.warning("批量出片部分成功", `${created} 成功 · ${failed} 失败`);
      } else {
        toast.success("批量出片已提交", `${created} 个任务`);
      }
      if (created > 0) {
        window.setTimeout(() => {
          navigate(
            firstRunId
              ? `${routes.caseOutputs(caseId)}?run=${encodeURIComponent(firstRunId)}`
              : routes.caseOutputs(caseId),
          );
        }, 1000);
      }
    },
    onError: (error: ApiError) => setFormError(error),
  });

  function batchFromScriptItems(items: ScriptToolItem[]) {
    batchCreateJobs.mutate(items.map((item) => ({ script: item.script, title: item.title })));
  }

  function goToStep(next: StudioStep) {
    if (next <= step) {
      setStep(next);
      return;
    }
    const message = validateStep(step, form, selectedVoice);
    if (message) {
      toast.warning("当前步骤未完成", message);
      return;
    }
    setStep(next);
  }

  function submit() {
    const invalid = validateAll(form, selectedVoice);
    if (invalid) {
      setStep(invalid.step);
      toast.warning("无法提交", invalid.message);
      return;
    }
    setFormError(null);
    createJob.mutate();
  }

  function extractReferenceVideo() {
    if (!referenceUrl.trim()) {
      toast.warning("请输入对标视频链接");
      return;
    }
    extractReference.mutate();
  }

  if (caseDetail.isLoading) {
    return <LoadingState block label="加载创作工作台" />;
  }

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>{caseDetail.data?.name ?? "创作"}</h1>
          <p>{caseDetail.data?.product || caseDetail.data?.industry || "按步骤完成脚本、模板、成片配置与后处理。"}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn-secondary text-sm" type="button" onClick={() => setBatchScriptsOpen(true)}>
            批量脚本
          </button>
          <SaveDefaultsButton
            isSaving={saveDefaults.isPending}
            justSaved={defaultsJustSaved}
            onSave={() => saveDefaults.mutate()}
          />
        </div>
      </header>
      <StudioTabs caseId={caseId} />
      {caseDetail.error ? <ErrorState error={caseDetail.error} /> : null}

      <div className="card">
        <FlowStepper
          steps={steps}
          activeStep={step}
          onStepClick={(next) => goToStep(next as StudioStep)}
          ariaLabel="创作流程步骤"
        />
      </div>

      <form
        className="grid gap-4 lg:grid-cols-[minmax(0,1.25fr)_minmax(320px,0.75fr)]"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <section className="card flex flex-col gap-5">
          <div className="grid gap-2.5 border-b border-border/70 pb-3">
            <div className="flex items-center justify-between gap-3 text-sm">
              <span className="font-semibold text-text-primary">{steps[step]}</span>
              <span className="font-mono text-xs tabular-nums text-text-tertiary">
                第 {step + 1} / {steps.length} 步
              </span>
            </div>
            <div className="h-1 overflow-hidden rounded-full bg-border/60" aria-hidden="true">
              <div
                className="h-full rounded-full bg-accent transition-all duration-300"
                style={{ width: `${((step + 1) / steps.length) * 100}%` }}
              />
            </div>
          </div>
          <div className="h-[520px] overflow-y-auto pr-1">
            {step === 0 ? (
              <ScriptStep
                form={form}
                setField={setField}
                scriptCount={scriptCount}
                tools={
                  <>
                    <ScriptToolBar
                      candidateCount={scriptToolbox.candidates.length}
                      historyCount={scriptToolbox.history.length}
                      onOpenGenerate={(mode) => {
                        setScriptToolMode(mode);
                        setScriptGenerateOpen(true);
                      }}
                      onOpenCandidates={() => setCandidatePoolOpen(true)}
                      onOpenHistory={() => setHistoryOpen(true)}
                    />
                    <div className="grid gap-2 rounded-2xl border border-border/70 bg-white/55 p-3">
                      <div className="flex flex-wrap items-end gap-2">
                        <label className="min-w-[220px] flex-1">
                          <span>对标视频提取</span>
                          <input
                            value={referenceUrl}
                            onChange={(event) => setReferenceUrl(event.target.value)}
                            placeholder="粘贴抖音/YouTube/B站视频链接"
                            disabled={extractReference.isPending}
                          />
                        </label>
                        <button
                          className="btn-secondary min-h-10"
                          type="button"
                          disabled={extractReference.isPending}
                          onClick={extractReferenceVideo}
                        >
                          {extractReference.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Link2 className="h-4 w-4" />}
                          <span>提取</span>
                        </button>
                      </div>
                      {referenceSourceTitle ? <p className="truncate text-xs text-text-secondary">来源：{referenceSourceTitle}</p> : null}
                    </div>
                  </>
                }
              />
            ) : step === 1 ? (
              <TemplateStep form={form} setField={setField} caseId={caseId} />
            ) : step === 2 ? (
              <ProductionStep
                form={form}
                setField={setField}
                selectedVoice={selectedVoice}
                voiceOptions={voiceOptions}
              />
            ) : step === 3 ? (
              <PostProcessStep form={form} setField={setField} />
            ) : (
              <SubmitStep form={form} selectedVoiceLabel={selectedVoiceLabel} scriptCount={scriptCount} />
            )}

            {formError ? <ErrorState error={formError} /> : null}
          </div>

          {step < 4 && stepBlockMessage ? (
            <p className="flex items-center gap-1.5 text-xs text-status-warning">
              <Info className="h-3.5 w-3.5 shrink-0" />
              <span>{stepBlockMessage}</span>
            </p>
          ) : null}

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border/70 pt-4">
            <button
              className="btn-secondary"
              type="button"
              disabled={step === 0 || createJob.isPending}
              onClick={() => setStep((current) => Math.max(0, current - 1) as StudioStep)}
            >
              <ChevronLeft className="h-4 w-4" />
              <span>上一步</span>
            </button>
            {step < 4 ? (
              <button
                className="btn-primary"
                type="button"
                onClick={() => goToStep((step + 1) as StudioStep)}
                disabled={Boolean(stepBlockMessage)}
              >
                <span>下一步</span>
                <ChevronRight className="h-4 w-4" />
              </button>
            ) : (
              <div className="flex flex-wrap gap-2">
                <button className="btn-primary" type="submit" disabled={createJob.isPending}>
                  {createJob.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                  <span>提交成片任务</span>
                </button>
              </div>
            )}
          </div>
        </section>

        <ConfigSummary form={form} selectedVoiceLabel={selectedVoiceLabel} scriptCount={scriptCount} />
      </form>

      <ScriptGenerateModal
        isOpen={scriptGenerateOpen}
        mode={scriptToolMode}
        caseId={caseId}
        currentScript={form.script}
        onClose={() => setScriptGenerateOpen(false)}
        onAdopt={(item) => {
          adoptScriptToolItem(item);
          setScriptGenerateOpen(false);
        }}
        onAddCandidate={scriptToolbox.addCandidate}
        onHistory={scriptToolbox.appendHistory}
      />
      <CandidatePoolModal
        isOpen={candidatePoolOpen}
        candidates={scriptToolbox.candidates}
        isBatchCreating={batchCreateJobs.isPending}
        onClose={() => setCandidatePoolOpen(false)}
        onUse={(item) => {
          adoptScriptToolItem(item);
          setCandidatePoolOpen(false);
        }}
        onRemove={scriptToolbox.removeCandidate}
        onClear={scriptToolbox.clearCandidates}
        onBatchCreate={(items) => batchFromScriptItems(items)}
      />
      <BatchScriptsModal
        isOpen={batchScriptsOpen}
        isSubmitting={batchCreateJobs.isPending}
        onClose={() => setBatchScriptsOpen(false)}
        onSubmit={(inputs) => batchCreateJobs.mutate(inputs)}
      />
      <ScriptHistoryModal isOpen={historyOpen} history={scriptToolbox.history} onClose={() => setHistoryOpen(false)} onInsert={insertHistoryItem} />
    </section>
  );
}
