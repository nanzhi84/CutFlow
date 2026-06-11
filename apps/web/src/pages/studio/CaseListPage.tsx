import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, FolderOpen, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type ApiError } from "../../api/client";
import { EmptyState, ErrorState, LoadingState } from "../../components/State";
import { Modal } from "../../components/Modal";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { SearchInput } from "../../components/ui/SearchInput";
import { useToast } from "../../components/Toast";
import { TimeText } from "../../components/TimeText";
import { routes } from "../../routes";

type CaseForm = {
  name: string;
  description: string;
  industry: string;
  product: string;
  target_audience: string;
};

const emptyForm: CaseForm = {
  name: "",
  description: "",
  industry: "",
  product: "",
  target_audience: "",
};

export default function CaseListPage() {
  const [search, setSearch] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [form, setForm] = useState<CaseForm>(emptyForm);
  const [formError, setFormError] = useState<unknown>(null);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const cases = useQuery({
    queryKey: ["cases", search],
    queryFn: () => api.cases.list({ search: search || null, limit: 100 }),
  });
  const createCase = useMutation({
    mutationFn: () =>
      api.cases.create({
        name: form.name.trim(),
        description: form.description.trim() || null,
        industry: form.industry.trim() || null,
        product: form.product.trim() || null,
        target_audience: form.target_audience.trim() || null,
      }),
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ["cases"] });
      setModalOpen(false);
      setForm(emptyForm);
      toast.success("案例已创建", created.name);
      navigate(routes.caseStudio(created.id));
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

  return (
    <section className="pageStack">
      <header className="pageHeader">
        <div>
          <h1>案例中心</h1>
          <p>{cases.data?.total_hint ?? items.length} 个案例工作空间，统一管理创作、成片和发布准备。</p>
        </div>
        <button className="btn-primary" type="button" onClick={() => setModalOpen(true)}>
          <Plus size={16} />
          <span>新建案例</span>
        </button>
      </header>

      <div className="card p-3">
        <SearchInput value={search} onChange={setSearch} placeholder="搜索案例名称" className="max-w-xl" />
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
                  <p className="mt-1 text-sm">最近更新 <TimeText value={item.updated_at} /></p>
                </div>
                <Link className="icon-button no-underline" to={routes.caseStudio(item.id)} aria-label={`进入 ${item.name}`}>
                  <ArrowRight className="h-4 w-4" />
                </Link>
              </div>
              <div className="grid grid-cols-3 gap-2">
                <span className="rounded-2xl border border-border/70 bg-white/60 px-3 py-2">
                  <b className="block font-mono text-lg text-text-primary">{item.active_memory_count}</b>
                  <span className="text-xs text-text-tertiary">活跃记忆</span>
                </span>
                <span className="rounded-2xl border border-border/70 bg-white/60 px-3 py-2">
                  <b className="block font-mono text-lg text-text-primary">{item.version}</b>
                  <span className="text-xs text-text-tertiary">版本</span>
                </span>
                <span className="rounded-2xl border border-border/70 bg-white/60 px-3 py-2">
                  <b className="block truncate text-sm text-text-primary">{item.owner_user_id ? "已分配" : "默认"}</b>
                  <span className="text-xs text-text-tertiary">负责人</span>
                </span>
              </div>
              <div className="flex items-center justify-between gap-3 border-t border-border/70 pt-4">
                <Link className="btn-secondary no-underline" to={routes.caseStudio(item.id)}>
                  <ArrowRight className="h-4 w-4" />
                  <span>进入工作台</span>
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
            </article>
          ))}
        </div>
      ) : null}

      {modalOpen ? (
        <Modal title="新建案例" onClose={() => setModalOpen(false)}>
          <form
            className="formGrid"
            onSubmit={(event) => {
              event.preventDefault();
              setFormError(null);
              createCase.mutate();
            }}
          >
            <label>
              <span>名称</span>
              <input
                value={form.name}
                onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))}
                required
              />
            </label>
            <label>
              <span>描述</span>
              <textarea
                value={form.description}
                onChange={(event) => setForm((current) => ({ ...current, description: event.target.value }))}
                rows={3}
              />
            </label>
            <div className="twoCol">
              <label>
                <span>行业</span>
                <input
                  value={form.industry}
                  onChange={(event) => setForm((current) => ({ ...current, industry: event.target.value }))}
                />
              </label>
              <label>
                <span>产品</span>
                <input
                  value={form.product}
                  onChange={(event) => setForm((current) => ({ ...current, product: event.target.value }))}
                />
              </label>
            </div>
            <label>
              <span>目标受众</span>
              <input
                value={form.target_audience}
                onChange={(event) => setForm((current) => ({ ...current, target_audience: event.target.value }))}
              />
            </label>
            {formError ? <ErrorState error={formError} /> : null}
            <div className="formActions">
              <button className="ghostButton" type="button" onClick={() => setModalOpen(false)}>
                取消
              </button>
              <button className="primaryButton" type="submit" disabled={createCase.isPending || !form.name.trim()}>
                <Plus size={16} />
                <span>创建</span>
              </button>
            </div>
          </form>
        </Modal>
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
