import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, FolderOpen, Pencil, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type ApiError, type CaseListItem, type CreateCaseRequest } from "../../api/client";
import { CaseModal } from "../../components/modals/CaseModal";
import { EmptyState, ErrorState, LoadingState } from "../../components/State";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { SearchInput } from "../../components/ui/SearchInput";
import { useToast } from "../../components/Toast";
import { TimeText } from "../../components/TimeText";
import { routes } from "../../routes";

export default function CaseListPage() {
  const [search, setSearch] = useState("");
  const [industry, setIndustry] = useState("");
  const [isCreating, setIsCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [formError, setFormError] = useState<unknown>(null);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const cases = useQuery({
    queryKey: ["cases", search, industry],
    queryFn: () => api.cases.list({ search: search || null, industry: industry || null, limit: 100 }),
  });
  const createCase = useMutation({
    mutationFn: (payload: CreateCaseRequest) => api.cases.create(payload),
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      setIsCreating(false);
      toast.success("案例草稿已创建", created.name);
      navigate(routes.caseProfile(created.id));
    },
    onError: (error: ApiError) => setFormError(error),
  });
  const deleteCase = useMutation({
    mutationFn: (caseId: string) => api.cases.delete(caseId),
    onSuccess: async () => {
      const deletedName = deleteTarget?.name;
      setDeleteTarget(null);
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      toast.success("案例已删除", deletedName);
    },
    onError: (error: ApiError) => toast.error("删除失败", error),
  });

  const items = useMemo(() => cases.data?.items ?? [], [cases.data?.items]);
  const industryOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const item of items) {
      if (item.industry) seen.add(item.industry);
    }
    if (industry) seen.add(industry);
    return [...seen].sort((a, b) => a.localeCompare(b, "zh"));
  }, [items, industry]);

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>案例中心</h1>
          <p>{cases.data?.total_hint ?? items.length} 个案例工作空间，统一管理创作、成片和发布准备。</p>
        </div>
        <button
          className="btn-primary"
          type="button"
          onClick={() => {
            setFormError(null);
            setIsCreating(true);
          }}
        >
          <Plus size={16} />
          <span>新建案例</span>
        </button>
      </header>

      <div className="card flex flex-wrap items-center gap-3 p-3">
        <SearchInput value={search} onChange={setSearch} placeholder="搜索案例名称" className="max-w-xl flex-1" />
        <label className="flex items-center gap-2 text-sm text-text-secondary">
          <span>行业</span>
          <select
            className="w-auto"
            value={industry}
            onChange={(event) => setIndustry(event.target.value)}
            aria-label="行业筛选"
          >
            <option value="">全部行业</option>
            {industryOptions.map((option) => (
              <option value={option} key={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
      </div>

      {cases.isLoading ? <LoadingState /> : null}
      {cases.error ? <ErrorState error={cases.error} /> : null}
      {!cases.isLoading && !cases.error && items.length === 0 ? (
        <EmptyState title="暂无案例" detail="新建一个案例后即可进入创作工作台。" />
      ) : null}

      {items.length > 0 ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {items.map((item) => (
            <article className="card card-hover grid gap-5" key={item.id}>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="mb-3 inline-flex h-10 w-10 items-center justify-center rounded-2xl bg-accent/10 text-accent">
                    <FolderOpen className="h-5 w-5" />
                  </div>
                  <h2 className="truncate text-lg font-semibold text-text-primary">{item.name}</h2>
                  <p className="mt-1 text-sm">
                    {item.industry ? <span className="text-text-secondary">{item.industry} · </span> : null}
                    最近更新 <TimeText value={item.updated_at} />
                  </p>
                </div>
                <Link className="icon-button no-underline" to={routes.caseStudio(item.id)} aria-label={`进入 ${item.name}`}>
                  <ArrowRight className="h-4 w-4" />
                </Link>
              </div>
              <CaseCounts item={item} />
              <p className="text-sm text-text-secondary">
                {item.active_memory_count} 条活跃记忆 · v{item.version}
              </p>
              <div className="flex items-center justify-between gap-3 border-t border-border/70 pt-4">
                <Link className="btn-secondary no-underline" to={routes.caseStudio(item.id)}>
                  <ArrowRight className="h-4 w-4" />
                  <span>进入工作台</span>
                </Link>
                <div className="flex items-center gap-2">
                  <Link className="btn-secondary no-underline" to={routes.caseProfile(item.id)}>
                    <Pencil className="h-4 w-4" />
                    <span>编辑</span>
                  </Link>
                  <button
                    className="btn-danger"
                    type="button"
                    onClick={() => setDeleteTarget({ id: item.id, name: item.name })}
                  >
                    <Trash2 className="h-4 w-4" />
                    <span>删除</span>
                  </button>
                </div>
              </div>
            </article>
          ))}
        </div>
      ) : null}

      {isCreating ? (
        <CaseModal
          isSaving={createCase.isPending}
          error={formError}
          onClose={() => setIsCreating(false)}
          onCreate={(payload) => {
            setFormError(null);
            createCase.mutate(payload);
          }}
        />
      ) : null}

      <ConfirmDialog
        isOpen={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        onConfirm={() => {
          if (deleteTarget) deleteCase.mutate(deleteTarget.id);
        }}
        title="删除案例"
        message={`确认删除「${deleteTarget?.name ?? ""}」？`}
        consequences={[
          "仅无活跃任务且没有成片引用的案例会被删除。",
          "已生成成片、历史运行或发布准备仍引用该案例时，系统会阻止删除。",
          "删除后案例工作台不会再出现在列表中。",
        ]}
        confirmText="删除案例"
        type="danger"
        isLoading={deleteCase.isPending}
      />
    </section>
  );
}

function CaseCounts({ item }: { item: CaseListItem }) {
  const counts = [
    { label: "素材", value: item.material_count ?? 0 },
    { label: "脚本", value: item.script_count ?? 0 },
    { label: "声音", value: item.voice_count ?? 0 },
    { label: "质检", value: item.quality_count ?? 0 },
  ];
  return (
    <dl className="grid grid-cols-4 gap-2 text-center">
      {counts.map((count) => (
        <div className="rounded-xl bg-surface-muted/60 px-2 py-2" key={count.label}>
          <dd className="text-lg font-semibold text-text-primary">{count.value}</dd>
          <dt className="text-xs text-text-secondary">{count.label}</dt>
        </div>
      ))}
    </dl>
  );
}
