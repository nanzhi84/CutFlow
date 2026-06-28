import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Briefcase, Loader2, Save, Sparkles, UserCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, type ApiError, type CaseDetail, type PatchCaseRequest } from "../../api/client";
import { joinList, parseList } from "../../components/modals/CaseModal";
import { EmptyState, ErrorState, LoadingState } from "../../components/ui/State";
import { StudioTabs } from "../../components/StudioTabs";
import { useToast } from "../../components/ui/Toast";

type ProfileForm = {
  name: string;
  description: string;
  industry: string;
  product: string;
  target_audience: string;
  key_selling_points: string;
  competitor_names: string;
  brand_keywords: string;
  ip_persona: string;
  brand_voice: string;
  strategy_tags: string;
};

function formFromCase(detail: CaseDetail): ProfileForm {
  return {
    name: detail.name ?? "",
    description: detail.description ?? "",
    industry: detail.industry ?? "",
    product: detail.product ?? "",
    target_audience: detail.target_audience ?? "",
    key_selling_points: joinList(detail.key_selling_points),
    competitor_names: joinList(detail.competitor_names),
    brand_keywords: joinList(detail.brand_keywords),
    ip_persona: detail.ip_persona ?? "",
    brand_voice: detail.brand_voice ?? "",
    strategy_tags: joinList(detail.strategy_tags),
  };
}

function buildPayload(form: ProfileForm): PatchCaseRequest {
  return {
    name: form.name.trim() || null,
    description: form.description.trim() || null,
    industry: form.industry.trim() || null,
    product: form.product.trim() || null,
    target_audience: form.target_audience.trim() || null,
    key_selling_points: parseList(form.key_selling_points),
    competitor_names: parseList(form.competitor_names),
    brand_keywords: parseList(form.brand_keywords),
    ip_persona: form.ip_persona.trim() || null,
    brand_voice: form.brand_voice.trim() || null,
    strategy_tags: parseList(form.strategy_tags),
  };
}

const emptyForm: ProfileForm = {
  name: "",
  description: "",
  industry: "",
  product: "",
  target_audience: "",
  key_selling_points: "",
  competitor_names: "",
  brand_keywords: "",
  ip_persona: "",
  brand_voice: "",
  strategy_tags: "",
};

export default function CaseProfilePage() {
  const { caseId = "" } = useParams();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState<ProfileForm>(emptyForm);
  const [formError, setFormError] = useState<unknown>(null);

  const caseDetail = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => api.cases.detail(caseId),
    enabled: Boolean(caseId),
  });

  useEffect(() => {
    if (caseDetail.data) setForm(formFromCase(caseDetail.data));
  }, [caseDetail.data]);

  const save = useMutation({
    mutationFn: () => api.cases.patch(caseId, buildPayload(form)),
    onSuccess: async (updated) => {
      setForm(formFromCase(updated));
      await queryClient.invalidateQueries({ queryKey: ["case", caseId] });
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      toast.success("案例画像已保存", updated.name);
    },
    onError: (error: ApiError) => setFormError(error),
  });

  function update<Key extends keyof ProfileForm>(key: Key, value: ProfileForm[Key]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  if (!caseId) return <EmptyState title="未选择案例" detail="请从案例中心进入工作台。" />;

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>案例画像</h1>
          <p>维护基础信息、卖点画像与 IP 人设，用于脚本生成和智能体推理。</p>
        </div>
      </header>
      <StudioTabs caseId={caseId} />
      {caseDetail.error ? <ErrorState error={caseDetail.error} /> : null}
      {caseDetail.isLoading ? <LoadingState label="加载案例画像" /> : null}

      {caseDetail.data ? (
        <form
          className="pageStack"
          onSubmit={(event) => {
            event.preventDefault();
            setFormError(null);
            save.mutate();
          }}
        >
          <section className="surface formSection">
            <div className="sectionHeader">
              <h2>
                <Briefcase className="inline-block h-[18px] w-[18px] align-[-3px] text-accent" />
                <span className="ml-2">基础信息</span>
              </h2>
              <span className="text-xs text-text-secondary">名称、行业与产品会用于卡片展示和脚本上下文</span>
            </div>
            <label>
              <span>案例名称</span>
              <input
                value={form.name}
                onChange={(event) => update("name", event.target.value)}
                placeholder="例如：无忧快喷"
                required
              />
            </label>
            <label>
              <span>案例简介</span>
              <textarea
                value={form.description}
                onChange={(event) => update("description", event.target.value)}
                rows={3}
                placeholder="卡片展示，建议 2-3 行"
              />
            </label>
            <div className="twoCol">
              <label>
                <span>行业</span>
                <input
                  value={form.industry}
                  onChange={(event) => update("industry", event.target.value)}
                  placeholder="例如：海南连锁钣喷门店"
                />
              </label>
              <label>
                <span>目标受众</span>
                <input
                  value={form.target_audience}
                  onChange={(event) => update("target_audience", event.target.value)}
                  placeholder="例如：爱车人士、时间敏感型"
                />
              </label>
            </div>
            <label>
              <span>产品信息</span>
              <textarea
                value={form.product}
                onChange={(event) => update("product", event.target.value)}
                rows={3}
                placeholder="可写产品线、规格、价格带"
              />
            </label>
          </section>

          <section className="surface formSection">
            <div className="sectionHeader">
              <h2>
                <Sparkles className="inline-block h-[18px] w-[18px] align-[-3px] text-accent" />
                <span className="ml-2">卖点画像</span>
              </h2>
              <span className="text-xs text-text-secondary">生成脚本时从中选取融入文案</span>
            </div>
            <label>
              <span>核心卖点（每行一个）</span>
              <textarea
                value={form.key_selling_points}
                onChange={(event) => update("key_selling_points", event.target.value)}
                rows={4}
                placeholder="坏多大补多大，不破坏周边漆面&#10;全海南门店联保，快至两小时取车"
              />
            </label>
            <div className="twoCol">
              <label>
                <span>竞品名称（每行一个）</span>
                <textarea
                  value={form.competitor_names}
                  onChange={(event) => update("competitor_names", event.target.value)}
                  rows={3}
                />
              </label>
              <label>
                <span>品牌关键词（每行一个）</span>
                <textarea
                  value={form.brand_keywords}
                  onChange={(event) => update("brand_keywords", event.target.value)}
                  rows={3}
                />
              </label>
            </div>
          </section>

          <section className="surface formSection">
            <div className="sectionHeader">
              <h2>
                <UserCircle className="inline-block h-[18px] w-[18px] align-[-3px] text-accent" />
                <span className="ml-2">IP 人设</span>
              </h2>
              <span className="text-xs text-text-secondary">生成 IP 人设类脚本时注入提示词</span>
            </div>
            <label>
              <span>IP 人设背景</span>
              <textarea
                value={form.ip_persona}
                onChange={(event) => update("ip_persona", event.target.value)}
                rows={4}
                placeholder="例如：王工，985 土木毕业，海南「无忧快喷」创始人……"
              />
            </label>
            <div className="twoCol">
              <label>
                <span>品牌声音</span>
                <input
                  value={form.brand_voice}
                  onChange={(event) => update("brand_voice", event.target.value)}
                  placeholder="例如：专业、真诚、轻松"
                />
              </label>
              <label>
                <span>策略标签（每行一个）</span>
                <textarea
                  value={form.strategy_tags}
                  onChange={(event) => update("strategy_tags", event.target.value)}
                  rows={2}
                />
              </label>
            </div>
          </section>

          {formError ? <ErrorState error={formError} /> : null}
          <div className="formActions">
            <button
              className="ghostButton"
              type="button"
              onClick={() => caseDetail.data && setForm(formFromCase(caseDetail.data))}
              disabled={save.isPending}
            >
              重置
            </button>
            <button className="primaryButton" type="submit" disabled={save.isPending || !form.name.trim()}>
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save size={16} />}
              <span>{save.isPending ? "保存中" : "保存画像"}</span>
            </button>
          </div>
        </form>
      ) : null}
    </section>
  );
}
