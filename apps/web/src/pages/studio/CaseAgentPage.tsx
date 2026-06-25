import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { useParams } from "react-router-dom";
import { api } from "../../api/client";
import { caseRubricApi, type ScorePrediction } from "../../api/r6";
import { EmptyState, ErrorState, LoadingState } from "../../components/ui/State";
import { StudioTabs } from "../../components/StudioTabs";
import { TimeText } from "../../components/TimeText";
import { useToast } from "../../components/ui/Toast";

export type AdoptedAgentScriptState = {
  adoptedAgentScript?: {
    title: string;
    script: string;
    source: string;
    scriptVersionId: string | null;
  };
};

export default function CaseAgentPage() {
  const { caseId = "" } = useParams();
  const queryClient = useQueryClient();
  const toast = useToast();

  const caseDetail = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => api.cases.detail(caseId),
    enabled: Boolean(caseId),
  });
  const rubric = useQuery({
    queryKey: ["case-rubric", caseId, "rubric"],
    queryFn: () => caseRubricApi.rubric(caseId),
    enabled: Boolean(caseId),
  });
  const calibration = useQuery({
    queryKey: ["case-rubric", caseId, "calibration"],
    queryFn: () => caseRubricApi.calibration(caseId),
    enabled: Boolean(caseId),
  });
  const bumpProposal = useQuery({
    queryKey: ["case-rubric", caseId, "bump"],
    queryFn: () => caseRubricApi.bumpProposal(caseId),
    enabled: Boolean(caseId),
  });
  const predictions = useQuery({
    queryKey: ["case-rubric", caseId, "predictions"],
    queryFn: () => caseRubricApi.predictions(caseId, { limit: 20 }),
    enabled: Boolean(caseId),
  });
  const pendingRetro = useQuery({
    queryKey: ["case-rubric", caseId, "pending-retro"],
    queryFn: () => caseRubricApi.pendingRetro(caseId),
    enabled: Boolean(caseId),
  });

  async function refreshRubric() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["case-rubric", caseId, "rubric"] }),
      queryClient.invalidateQueries({ queryKey: ["case-rubric", caseId, "calibration"] }),
      queryClient.invalidateQueries({ queryKey: ["case-rubric", caseId, "bump"] }),
      queryClient.invalidateQueries({ queryKey: ["case-rubric", caseId, "predictions"] }),
      queryClient.invalidateQueries({ queryKey: ["case-rubric", caseId, "pending-retro"] }),
    ]);
  }

  const acceptBump = useMutation({
    mutationFn: (proposalId: string) => caseRubricApi.acceptBump(caseId, proposalId),
    onSuccess: async () => {
      toast.success("评分卡已升版");
      await refreshRubric();
    },
  });
  const rejectBump = useMutation({
    mutationFn: (proposalId: string) => caseRubricApi.rejectBump(caseId, proposalId, { reason: "先不用" }),
    onSuccess: async () => {
      toast.success("已保留当前评分卡");
      await refreshRubric();
    },
  });

  if (!caseId) return <EmptyState title="未选择案例" detail="请从案例中心进入工作台。" />;
  const loading = rubric.isLoading || calibration.isLoading || predictions.isLoading;
  const predictionItems = predictions.data?.items ?? [];
  const pendingItems = pendingRetro.data?.items ?? [];

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>评分卡</h1>
          <p>{caseDetail.data?.name ?? "案例评分卡、盲预测与复盘状态。"}</p>
        </div>
      </header>
      <StudioTabs caseId={caseId} />

      {[caseDetail, rubric, calibration, bumpProposal, predictions, pendingRetro].map((query, index) =>
        query.error ? <ErrorState error={query.error} key={index} /> : null,
      )}
      {loading ? <LoadingState label="加载评分卡" /> : null}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(320px,0.7fr)]">
        <section className="card grid gap-4">
          <div className="sectionHeader">
            <div>
              <h2>Case Rubric v{rubric.data?.version ?? "-"}</h2>
              <p>{rubric.data?.cold_start ? "冷启动评分卡" : "已基于奖励信号校准"}</p>
            </div>
            <span className="badge-info">{rubric.data?.status ?? "active"}</span>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            {(rubric.data?.dimensions ?? []).map((dimension) => (
              <div className="rounded-lg border border-border/70 p-3" key={dimension.key}>
                <div className="mb-2 flex items-center justify-between gap-3">
                  <h3 className="text-sm font-semibold text-text-primary">{dimension.label}</h3>
                  <span className="font-mono text-xs text-text-secondary">{Math.round(dimension.weight * 100)}%</span>
                </div>
                <p className="text-xs text-text-tertiary">{dimension.key}</p>
                {Object.keys(dimension.value_scores ?? {}).length ? (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {Object.entries(dimension.value_scores ?? {}).map(([value, score]) => (
                      <span className="badge-info" key={value}>
                        {value}: {Math.round(Number(score) * 100)}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </section>

        <section className="card grid gap-4">
          <div className="sectionHeader">
            <div>
              <h2>复盘</h2>
              <p>{calibration.data?.bump_recommended ? "建议升版" : "当前无需升版"}</p>
            </div>
            <span className="badge-info">{calibration.data?.sample_size ?? 0} 样本</span>
          </div>
          <Metric label="排序一致性" value={formatConsistency(calibration.data?.consistency)} />
          <Metric label="连续误判" value={`${calibration.data?.miss_streak ?? 0}`} />
          <Metric label="待复盘" value={`${calibration.data?.pending_retro_count ?? pendingItems.length}`} />
          {bumpProposal.data ? (
            <div className="border-t border-border/60 pt-4">
              <h3 className="text-sm font-semibold text-text-primary">升版提议</h3>
              <p className="mt-2 text-sm leading-relaxed text-text-secondary">{bumpProposal.data.rationale}</p>
              <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-text-secondary">
                <span>旧一致性 {formatConsistency(bumpProposal.data.old_consistency)}</span>
                <span>新一致性 {formatConsistency(bumpProposal.data.new_consistency)}</span>
              </div>
              <div className="mt-4 flex flex-wrap gap-2">
                <button
                  className="btn-primary"
                  type="button"
                  disabled={acceptBump.isPending || rejectBump.isPending}
                  onClick={() => acceptBump.mutate(bumpProposal.data!.id)}
                >
                  {acceptBump.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
                  <span>接受升版</span>
                </button>
                <button
                  className="btn-secondary"
                  type="button"
                  disabled={acceptBump.isPending || rejectBump.isPending}
                  onClick={() => rejectBump.mutate(bumpProposal.data!.id)}
                >
                  <XCircle className="h-4 w-4" />
                  <span>先不用</span>
                </button>
              </div>
            </div>
          ) : null}
        </section>
      </div>

      <section className="card grid gap-4">
        <div className="sectionHeader">
          <div>
            <h2>最近盲预测</h2>
            <p>创作页生成的脚本会在这里留下评分记录。</p>
          </div>
          <span className="badge-info">{predictionItems.length} 条</span>
        </div>
        {predictionItems.length === 0 ? <EmptyState title="暂无盲预测" detail="从创作页生成脚本后会自动出现。" /> : null}
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {predictionItems.map((prediction) => (
            <PredictionCard prediction={prediction} key={prediction.id} />
          ))}
        </div>
      </section>

      <section className="card grid gap-4">
        <div className="sectionHeader">
          <div>
            <h2>待复盘</h2>
            <p>发布窗口到期但尚未回填指标的成片。</p>
          </div>
          <span className="badge-info">{pendingItems.length} 条</span>
        </div>
        {pendingItems.length === 0 ? <EmptyState title="暂无待复盘" detail="有发布记录且窗口到期后会自动进入列表。" /> : null}
        <div className="divide-y divide-border/60">
          {pendingItems.map((item) => (
            <article className="grid gap-1 py-3 first:pt-0 last:pb-0" key={item.id}>
              <div className="flex items-center justify-between gap-3">
                <h3 className="text-sm font-semibold text-text-primary">{item.title || item.finished_video_id}</h3>
                <span className="text-xs text-text-tertiary">{item.days_since_publish} 天</span>
              </div>
              <p className="text-xs text-text-secondary">
                {item.platform ?? "unknown"} · <TimeText value={item.published_at} />
              </p>
            </article>
          ))}
        </div>
      </section>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between rounded-lg border border-border/70 px-3 py-2">
      <span className="text-sm text-text-secondary">{label}</span>
      <strong className="font-mono text-sm text-text-primary">{value}</strong>
    </div>
  );
}

function PredictionCard({ prediction }: { prediction: ScorePrediction }) {
  return (
    <article className="rounded-lg border border-border/70 p-3">
      <div className="mb-2 flex items-start justify-between gap-3">
        <span className="badge-info">{bandLabel(prediction.band)}</span>
        <span className="font-mono text-sm text-text-primary">{prediction.composite.toFixed(1)}</span>
      </div>
      <p className="line-clamp-3 text-sm leading-relaxed text-text-secondary">{prediction.reason || "暂无理由"}</p>
      <div className="mt-3 flex items-center justify-between gap-3 text-xs text-text-tertiary">
        <TimeText value={prediction.locked_at} />
        <span>{prediction.settled_reward == null ? "未结算" : `奖励 ${prediction.settled_reward.toFixed(2)}`}</span>
      </div>
    </article>
  );
}

function bandLabel(value: string) {
  if (value === "top") return "最看好";
  if (value === "ok") return "还不错";
  return "一般";
}

function formatConsistency(value: number | null | undefined) {
  if (value == null) return "-";
  return `${Math.round(value * 100)}%`;
}
