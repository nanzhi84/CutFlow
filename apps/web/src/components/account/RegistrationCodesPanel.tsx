import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Loader2, RefreshCw, Save, Ticket } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type RegistrationCodePreview, type AuthUser } from "../../api/client";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { useToast } from "../ui/Toast";
import { codeStatusLabel, codeStatusOptions, displayTime, fromDateTimeLocal, roleOptions, toDateTimeLocal } from "./accountModel";

type CodeDraft = {
  status: RegistrationCodePreview["status"];
  purpose: string;
  expires_at: string;
};

function draftFromCode(code: RegistrationCodePreview): CodeDraft {
  return { status: code.status, purpose: code.purpose ?? "", expires_at: toDateTimeLocal(code.expires_at) };
}

function remainingUses(code: RegistrationCodePreview) {
  if (code.max_uses === null || code.max_uses === undefined) return "不限";
  return Math.max(0, code.max_uses - code.used_count).toLocaleString("zh-CN");
}

export function RegistrationCodesPanel() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [drafts, setDrafts] = useState<Record<string, CodeDraft>>({});
  const [pendingDisable, setPendingDisable] = useState<RegistrationCodePreview | null>(null);
  const [issuedCode, setIssuedCode] = useState("");
  const [createForm, setCreateForm] = useState({
    role: "viewer" as AuthUser["role"],
    custom_code: "",
    purpose: "",
    max_uses: "1",
    expires_at: "",
  });
  const codes = useQuery({
    queryKey: ["auth", "registration-codes"],
    queryFn: () => api.auth.registrationCodes({ limit: 100 }),
  });
  const createMutation = useMutation({
    mutationFn: api.auth.createRegistrationCode,
    onSuccess: async (created) => {
      setIssuedCode(created.plaintext_code);
      toast.success("注册码已生成", "明文码仅本次展示");
      setCreateForm({ role: "viewer", custom_code: "", purpose: "", max_uses: "1", expires_at: "" });
      await queryClient.invalidateQueries({ queryKey: ["auth", "registration-codes"] });
    },
  });
  const patchMutation = useMutation({
    mutationFn: ({ codeId, draft }: { codeId: string; draft: CodeDraft }) =>
      api.auth.patchRegistrationCode(codeId, {
        status: draft.status,
        purpose: draft.purpose.trim() || null,
        expires_at: fromDateTimeLocal(draft.expires_at),
      }),
    onSuccess: async () => {
      toast.success("注册码已更新");
      await queryClient.invalidateQueries({ queryKey: ["auth", "registration-codes"] });
    },
  });

  useEffect(() => {
    if (!codes.data) return;
    setDrafts(Object.fromEntries(codes.data.items.map((code) => [code.id, draftFromCode(code)])));
  }, [codes.data]);

  function submitCreate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const maxUses = createForm.max_uses.trim() ? Number(createForm.max_uses) : null;
    if (maxUses !== null && (!Number.isInteger(maxUses) || maxUses < 1)) {
      toast.warning("可用次数至少为 1");
      return;
    }
    setIssuedCode("");
    createMutation.mutate({
      role: createForm.role,
      custom_code: createForm.custom_code.trim() || null,
      purpose: createForm.purpose.trim() || null,
      max_uses: maxUses,
      expires_at: fromDateTimeLocal(createForm.expires_at),
    });
  }

  function updateDraft(codeId: string, patch: Partial<CodeDraft>) {
    setDrafts((current) => ({ ...current, [codeId]: { ...current[codeId], ...patch } }));
  }

  function saveCode(code: RegistrationCodePreview) {
    const draft = drafts[code.id];
    if (!draft) return;
    if (code.status === "active" && draft.status === "disabled") {
      setPendingDisable(code);
      return;
    }
    patchMutation.mutate({ codeId: code.id, draft });
  }

  function confirmDisable() {
    if (!pendingDisable) return;
    const draft = drafts[pendingDisable.id];
    if (draft) patchMutation.mutate({ codeId: pendingDisable.id, draft });
    setPendingDisable(null);
  }

  async function copyIssuedCode() {
    if (!issuedCode) return;
    await navigator.clipboard?.writeText(issuedCode);
    toast.success("已复制注册码");
  }

  return (
    <section className="space-y-5">
      <div className="card">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="flex items-center gap-2 text-xl font-semibold text-text-primary">
              <Ticket className="h-5 w-5 text-accent" />
              注册码
            </h2>
            <p className="mt-2 text-sm leading-6 text-text-secondary">新成员凭管理员发放的注册码注册。明文码只在创建成功后展示一次。</p>
          </div>
          <button className="btn-secondary text-sm" type="button" onClick={() => void codes.refetch()}>
            <RefreshCw className={`h-4 w-4 ${codes.isFetching ? "animate-spin" : ""}`} />
            刷新
          </button>
        </div>

        <form className="mt-6 grid gap-4 md:grid-cols-2 xl:grid-cols-[150px_180px_minmax(180px,1fr)_180px_1fr_auto]" onSubmit={submitCreate}>
          <label>
            <span>注册角色</span>
            <select value={createForm.role} onChange={(event) => setCreateForm((current) => ({ ...current, role: event.target.value as AuthUser["role"] }))}>
              {roleOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <label>
            <span>自定义码</span>
            <input
              value={createForm.custom_code}
              onChange={(event) => setCreateForm((current) => ({ ...current, custom_code: event.target.value }))}
              placeholder="留空自动生成"
            />
          </label>
          <label>
            <span>用途备注</span>
            <input
              value={createForm.purpose}
              onChange={(event) => setCreateForm((current) => ({ ...current, purpose: event.target.value }))}
              placeholder="例如：交付团队入职"
            />
          </label>
          <label>
            <span>可用次数</span>
            <input
              min={1}
              type="number"
              value={createForm.max_uses}
              onChange={(event) => setCreateForm((current) => ({ ...current, max_uses: event.target.value }))}
              placeholder="留空不限"
            />
          </label>
          <label>
            <span>过期时间</span>
            <input
              type="datetime-local"
              value={createForm.expires_at}
              onChange={(event) => setCreateForm((current) => ({ ...current, expires_at: event.target.value }))}
            />
          </label>
          <div className="flex items-end">
            <button className="btn-primary w-full" type="submit" disabled={createMutation.isPending}>
              {createMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Ticket className="h-4 w-4" />}
              生成
            </button>
          </div>
        </form>

        {issuedCode ? (
          <div className="mt-5 rounded-[22px] border border-accent/25 bg-accent/10 p-4">
            <p className="text-sm font-medium text-text-primary">新注册码（仅本次展示）</p>
            <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
              <code className="min-w-0 flex-1 break-all rounded-2xl bg-white/80 px-4 py-3 font-mono text-sm text-text-primary">
                {issuedCode}
              </code>
              <button className="btn-secondary" type="button" onClick={() => void copyIssuedCode()}>
                <Copy className="h-4 w-4" />
                复制
              </button>
            </div>
          </div>
        ) : null}
      </div>

      <div className="card">
        <h2 className="text-xl font-semibold text-text-primary">注册码列表</h2>
        <div className="mt-4 space-y-3">
          {codes.data?.items.length === 0 ? (
            <div className="rounded-[22px] border border-dashed border-border bg-white/45 px-6 py-8 text-center text-sm text-text-tertiary">
              暂无注册码
            </div>
          ) : null}
          {codes.data?.items.map((code) => {
            const draft = drafts[code.id] ?? draftFromCode(code);
            return (
              <div className="rounded-[22px] border border-border/70 bg-white/55 p-4" key={code.id}>
                <div className="grid gap-3 xl:grid-cols-[minmax(180px,1fr)_130px_170px_minmax(180px,1fr)_1fr_auto] xl:items-end">
                  <div className="min-w-0">
                    <p className="truncate font-mono font-semibold text-text-primary">{code.id}</p>
                    <p className="mt-1 text-xs text-text-tertiary">
                      已用 {code.used_count.toLocaleString("zh-CN")} / 剩余 {remainingUses(code)} · 创建 {displayTime(code.created_at)}
                    </p>
                  </div>
                  <div>
                    <span className={code.status === "active" ? "badge-success" : code.status === "expired" ? "badge-warning" : "badge"}>
                      {codeStatusLabel(code.status)}
                    </span>
                  </div>
                  <label>
                    <span>状态</span>
                    <select value={draft.status} onChange={(event) => updateDraft(code.id, { status: event.target.value as RegistrationCodePreview["status"] })}>
                      {codeStatusOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                    </select>
                  </label>
                  <label>
                    <span>用途备注</span>
                    <input value={draft.purpose} onChange={(event) => updateDraft(code.id, { purpose: event.target.value })} placeholder="未填写" />
                  </label>
                  <label>
                    <span>过期时间</span>
                    <input type="datetime-local" value={draft.expires_at} onChange={(event) => updateDraft(code.id, { expires_at: event.target.value })} />
                  </label>
                  <button className="btn-primary" type="button" onClick={() => saveCode(code)} disabled={patchMutation.isPending}>
                    {patchMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                    保存
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <ConfirmDialog
        isOpen={Boolean(pendingDisable)}
        onClose={() => setPendingDisable(null)}
        onConfirm={confirmDisable}
        title="停用注册码？"
        message="该注册码将无法继续用于新成员注册。"
        consequences={["已经注册的成员不会受影响。", "注册码历史使用次数会保留。", "如需恢复，可重新启用该注册码。"]}
        confirmText="确认停用"
        type="danger"
        isLoading={patchMutation.isPending}
      />
    </section>
  );
}
