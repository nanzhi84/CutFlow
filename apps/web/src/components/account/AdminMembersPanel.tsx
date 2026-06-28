import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, RefreshCw, Save, UserPlus } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type AuthUser } from "../../api/client";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { useToast } from "../ui/Toast";
import { displayTime, roleOptions, userStatusOptions } from "./accountModel";

type UserDraft = {
  display_name: string;
  role: AuthUser["role"];
  status: AuthUser["status"];
};

function draftFromUser(user: AuthUser): UserDraft {
  return { display_name: user.display_name, role: user.role, status: user.status };
}

export function AdminMembersPanel({ currentUser }: { currentUser: AuthUser | null }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [drafts, setDrafts] = useState<Record<string, UserDraft>>({});
  const [pendingDisable, setPendingDisable] = useState<AuthUser | null>(null);
  const [createForm, setCreateForm] = useState({
    email: "",
    display_name: "",
    password: "",
    role: "viewer" as AuthUser["role"],
  });
  const users = useQuery({
    queryKey: ["auth", "users"],
    queryFn: () => api.auth.users({ limit: 100 }),
  });
  const createMutation = useMutation({
    mutationFn: api.auth.createUser,
    onSuccess: async () => {
      toast.success("成员已创建");
      setCreateForm({ email: "", display_name: "", password: "", role: "viewer" });
      await queryClient.invalidateQueries({ queryKey: ["auth", "users"] });
    },
  });
  const patchMutation = useMutation({
    mutationFn: ({ userId, draft }: { userId: string; draft: UserDraft }) => api.auth.patchUser(userId, draft),
    onSuccess: async (updated) => {
      toast.success("成员信息已更新");
      await queryClient.invalidateQueries({ queryKey: ["auth", "users"] });
      if (updated.id === currentUser?.id) {
        await queryClient.invalidateQueries({ queryKey: ["auth", "session"] });
      }
    },
  });

  useEffect(() => {
    if (!users.data) return;
    setDrafts(Object.fromEntries(users.data.items.map((user) => [user.id, draftFromUser(user)])));
  }, [users.data]);

  function updateDraft(userId: string, patch: Partial<UserDraft>) {
    setDrafts((current) => ({ ...current, [userId]: { ...current[userId], ...patch } }));
  }

  function submitCreate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    createMutation.mutate({
      email: createForm.email.trim(),
      display_name: createForm.display_name.trim() || createForm.email.trim(),
      password: createForm.password || undefined,
      role: createForm.role,
    });
  }

  function saveUser(user: AuthUser) {
    const draft = drafts[user.id];
    if (!draft) return;
    if (user.status !== "disabled" && draft.status === "disabled") {
      setPendingDisable(user);
      return;
    }
    patchMutation.mutate({ userId: user.id, draft });
  }

  function confirmDisable() {
    if (!pendingDisable) return;
    const draft = drafts[pendingDisable.id];
    if (draft) patchMutation.mutate({ userId: pendingDisable.id, draft });
    setPendingDisable(null);
  }

  return (
    <section className="space-y-5">
      <div className="card">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="flex items-center gap-2 text-xl font-semibold text-text-primary">
              <UserPlus className="h-5 w-5 text-accent" />
              新增成员
            </h2>
            <p className="mt-2 text-sm leading-6 text-text-secondary">预创建团队成员账号。未填写密码时会自动生成随机密码。</p>
          </div>
          <button className="btn-secondary text-sm" type="button" onClick={() => void users.refetch()}>
            <RefreshCw className={`h-4 w-4 ${users.isFetching ? "animate-spin" : ""}`} />
            刷新
          </button>
        </div>

        <form className="mt-6 grid gap-4 md:grid-cols-2 xl:grid-cols-[1fr_1fr_180px_1fr_auto]" onSubmit={submitCreate}>
          <label>
            <span>邮箱</span>
            <input
              autoComplete="email"
              required
              type="email"
              value={createForm.email}
              onChange={(event) => setCreateForm((current) => ({ ...current, email: event.target.value }))}
            />
          </label>
          <label>
            <span>显示名称</span>
            <input
              autoComplete="nickname"
              value={createForm.display_name}
              onChange={(event) => setCreateForm((current) => ({ ...current, display_name: event.target.value }))}
            />
          </label>
          <label>
            <span>角色</span>
            <select
              value={createForm.role}
              onChange={(event) => setCreateForm((current) => ({ ...current, role: event.target.value as AuthUser["role"] }))}
            >
              {roleOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <label>
            <span>初始密码</span>
            <input
              autoComplete="new-password"
              minLength={8}
              type="password"
              value={createForm.password}
              onChange={(event) => setCreateForm((current) => ({ ...current, password: event.target.value }))}
              placeholder="留空由系统生成"
            />
          </label>
          <div className="flex items-end">
            <button className="btn-primary w-full" type="submit" disabled={createMutation.isPending}>
              {createMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <UserPlus className="h-4 w-4" />}
              新增
            </button>
          </div>
        </form>
      </div>

      <div className="card">
        <h2 className="text-xl font-semibold text-text-primary">成员列表</h2>
        <div className="mt-4 space-y-3">
          {users.data?.items.length === 0 ? (
            <div className="rounded-[22px] border border-dashed border-border bg-white/45 px-6 py-8 text-center text-sm text-text-tertiary">
              暂无成员
            </div>
          ) : null}
          {users.data?.items.map((user) => {
            const draft = drafts[user.id] ?? draftFromUser(user);
            return (
              <div className="rounded-[22px] border border-border/70 bg-white/55 p-4" key={user.id}>
                <div className="grid gap-3 xl:grid-cols-[minmax(180px,1.1fr)_1fr_170px_160px_auto] xl:items-end">
                  <div className="min-w-0">
                    <p className="truncate font-medium text-text-primary">{user.email}</p>
                    <p className="mt-1 text-xs text-text-tertiary">创建：{displayTime(user.created_at)}</p>
                  </div>
                  <label>
                    <span>显示名称</span>
                    <input value={draft.display_name} onChange={(event) => updateDraft(user.id, { display_name: event.target.value })} />
                  </label>
                  <label>
                    <span>角色</span>
                    <select value={draft.role} onChange={(event) => updateDraft(user.id, { role: event.target.value as AuthUser["role"] })}>
                      {roleOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                    </select>
                  </label>
                  <label>
                    <span>状态</span>
                    <select value={draft.status} onChange={(event) => updateDraft(user.id, { status: event.target.value as AuthUser["status"] })}>
                      {userStatusOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                    </select>
                  </label>
                  <button className="btn-primary" type="button" onClick={() => saveUser(user)} disabled={patchMutation.isPending}>
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
        title="停用成员账号？"
        message="该成员将无法继续登录或访问受保护页面。"
        consequences={["已登录会话会在下次校验时失效。", "历史任务、素材和审计记录不会删除。", "如需恢复，可由管理员重新启用账号。"]}
        confirmText="确认停用"
        type="danger"
        isLoading={patchMutation.isPending}
      />
    </section>
  );
}
