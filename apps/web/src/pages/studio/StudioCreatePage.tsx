import { useMutation, useQuery } from "@tanstack/react-query";
import { Calculator, ChevronLeft, ChevronRight, Loader2, Play } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { api, type ApiError, type DigitalHumanVideoCostEstimateResponse } from "../../api/client";
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
import { CostEstimateModal } from "../../components/studio-create/CostEstimateModal";
import {
  STORAGE_KEY,
  loadStoredForm,
  steps,
  validateAll,
  validateStep,
  type FormState,
  type StudioStep,
} from "../../components/studio-create/studioCreateModel";
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
  const toast = useToast();
  const [step, setStep] = useState<StudioStep>(0);
  const [form, setForm] = useState<FormState>(loadStoredForm);
  const [formError, setFormError] = useState<unknown>(null);
  const [scriptToolMode, setScriptToolMode] = useState<ScriptToolMode>("generate");
  const [scriptGenerateOpen, setScriptGenerateOpen] = useState(false);
  const [candidatePoolOpen, setCandidatePoolOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [costEstimateOpen, setCostEstimateOpen] = useState(false);
  const [costEstimate, setCostEstimate] = useState<DigitalHumanVideoCostEstimateResponse | null>(null);
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

  const voiceOptions = useMemo(() => voices.data?.items.filter((voice) => voice.enabled) ?? [], [voices.data?.items]);
  const selectedVoice = form.voiceId || voiceOptions[0]?.id || "voice_sandbox";
  const selectedVoiceLabel = voiceOptions.find((voice) => voice.id === selectedVoice)?.display_name ?? selectedVoice;
  const scriptCount = form.script.trim().length;

  useEffect(() => {
    const { title, script, ...preferences } = form;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
  }, [form]);

  useEffect(() => {
    if (!adoptedAgentScript || appliedAgentSource.current === adoptedAgentScript.source) return;
    appliedAgentSource.current = adoptedAgentScript.source;
    setForm((current) => ({
      ...current,
      title: adoptedAgentScript.title || current.title,
      script: adoptedAgentScript.script,
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

  function buildJobPayload(script: string, title?: string | null): VideoJobPayload {
    return {
      schema_version: "digital_human_video_request.v1",
      case_id: caseId,
      title: title?.trim() || null,
      script: script.trim(),
      publish_content: "",
      workflow_template_id: "digital_human_v2",
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
        enabled: form.brollEnabled,
        max_inserts: form.maxInserts,
        min_segment_duration: 3,
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
        enabled: form.lipsyncEnabled,
        provider_profile_id: "runninghub.heygem.default",
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
    mutationFn: () => api.jobs.createDigitalHumanVideo(buildJobPayload(form.script, form.title)),
    onSuccess: (data) => {
      const runId = data.initial_run?.id;
      toast.success("任务提交成功", runId ? `Run ${shortId(runId)}` : undefined);
      window.setTimeout(() => {
        navigate(runId ? `${routes.caseOutputs(caseId)}?run=${encodeURIComponent(runId)}` : routes.caseOutputs(caseId));
      }, 1500);
    },
    onError: (error: ApiError) => setFormError(error),
  });
  const estimateCost = useMutation({
    mutationFn: () => api.jobs.estimateDigitalHumanVideoCost(buildJobPayload(form.script, form.title)),
    onSuccess: (data) => {
      setCostEstimate(data);
      setCostEstimateOpen(true);
    },
    onError: (error: ApiError) => setFormError(error),
  });

  function setField<Key extends keyof FormState>(key: Key, value: FormState[Key]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function adoptScriptToolItem(item: ScriptToolItem) {
    setField("title", item.title);
    setField("script", item.script);
    setStep(0);
    toast.info("已采用脚本", item.source === "sandbox" ? "沙箱生成" : "候选池");
  }

  function insertHistoryItem(item: ScriptToolItem) {
    setForm((current) => ({
      ...current,
      script: current.script.trim() ? `${current.script.trim()}\n\n${item.script}` : item.script,
    }));
    setStep(0);
    setHistoryOpen(false);
    toast.info("已插入历史脚本", "内容已追加到当前脚本末尾");
  }

  const batchCreateJobs = useMutation({
    mutationFn: async (items: ScriptToolItem[]) => {
      const created = [];
      for (const item of items) {
        created.push(await api.jobs.createDigitalHumanVideo(buildJobPayload(item.script, item.title)));
      }
      return created;
    },
    onSuccess: (items) => {
      const firstRun = items[0]?.initial_run?.id;
      toast.success("批量出片已提交", `${items.length} 个任务`);
      setCandidatePoolOpen(false);
      window.setTimeout(() => {
        navigate(firstRun ? `${routes.caseOutputs(caseId)}?run=${encodeURIComponent(firstRun)}` : routes.caseOutputs(caseId));
      }, 1000);
    },
    onError: (error: ApiError) => setFormError(error),
  });

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

  function estimate() {
    const invalid = validateAll(form, selectedVoice);
    if (invalid) {
      setStep(invalid.step);
      toast.warning("无法预估", invalid.message);
      return;
    }
    setFormError(null);
    estimateCost.mutate();
  }

  if (caseDetail.isLoading) {
    return <LoadingState />;
  }

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>{caseDetail.data?.name ?? "创作"}</h1>
          <p>{caseDetail.data?.product || caseDetail.data?.industry || "按步骤完成脚本、模板、成片配置与后处理。"}</p>
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
        <section className="card grid gap-5">
          {step === 0 ? (
            <ScriptStep
              form={form}
              setField={setField}
              scriptCount={scriptCount}
              tools={
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
              }
            />
          ) : step === 1 ? (
            <TemplateStep form={form} setField={setField} />
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
                disabled={Boolean(validateStep(step, form, selectedVoice))}
              >
                <span>下一步</span>
                <ChevronRight className="h-4 w-4" />
              </button>
            ) : (
              <div className="flex flex-wrap gap-2">
                <button className="btn-secondary" type="button" disabled={estimateCost.isPending || createJob.isPending} onClick={estimate}>
                  {estimateCost.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Calculator className="h-4 w-4" />}
                  <span>预估成本</span>
                </button>
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
        onBatchCreate={(items) => batchCreateJobs.mutate(items)}
      />
      <ScriptHistoryModal isOpen={historyOpen} history={scriptToolbox.history} onClose={() => setHistoryOpen(false)} onInsert={insertHistoryItem} />
      <CostEstimateModal isOpen={costEstimateOpen} estimate={costEstimate} onClose={() => setCostEstimateOpen(false)} />
    </section>
  );
}
