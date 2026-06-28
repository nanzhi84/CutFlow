import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, KeyRound, Loader2, UserCircle2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api, type AuthUser } from "../../api/client";
import { useToast } from "../ui/Toast";

export function ProfileSecurityPanel({ user }: { user: AuthUser | null }) {
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [passwordForm, setPasswordForm] = useState({ old_password: "", new_password: "", confirm_password: "" });
  const queryClient = useQueryClient();
  const toast = useToast();

  useEffect(() => {
    setDisplayName(user?.display_name ?? "");
  }, [user?.display_name]);

  const profileMutation = useMutation({
    mutationFn: api.auth.updateMe,
    onSuccess: (updated) => {
      queryClient.setQueryData(["auth", "session"], (current: unknown) => {
        if (!current || typeof current !== "object") return current;
        return { ...current, user: updated };
      });
      toast.success("资料已更新");
    },
  });
  const passwordMutation = useMutation({
    mutationFn: api.auth.changePassword,
    onSuccess: () => {
      toast.success("密码已更新");
      setPasswordForm({ old_password: "", new_password: "", confirm_password: "" });
    },
  });

  function saveProfile(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    profileMutation.mutate({ display_name: displayName.trim() });
  }

  function savePassword(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (passwordForm.new_password !== passwordForm.confirm_password) {
      toast.warning("两次输入的新密码不一致");
      return;
    }
    passwordMutation.mutate({
      old_password: passwordForm.old_password,
      new_password: passwordForm.new_password,
    });
  }

  return (
    <div className="grid gap-5 xl:grid-cols-2">
      <section className="card">
        <h2 className="flex items-center gap-2 text-xl font-semibold text-text-primary">
          <UserCircle2 className="h-5 w-5 text-accent" />
          个人资料
        </h2>
        <p className="mt-2 text-sm leading-6 text-text-secondary">修改系统内显示名称，不影响登录邮箱。</p>
        <form className="mt-6 space-y-4" onSubmit={saveProfile}>
          <label>
            <span>显示名称</span>
            <input
              autoComplete="nickname"
              required
              value={displayName}
              onChange={(event) => setDisplayName(event.target.value)}
              placeholder="请输入显示名称"
            />
          </label>
          <button className="btn-primary" type="submit" disabled={profileMutation.isPending}>
            {profileMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
            保存资料
          </button>
        </form>
      </section>

      <section className="card">
        <h2 className="flex items-center gap-2 text-xl font-semibold text-text-primary">
          <KeyRound className="h-5 w-5 text-accent" />
          修改密码
        </h2>
        <p className="mt-2 text-sm leading-6 text-text-secondary">新密码至少 8 位。修改后请使用新密码继续登录。</p>
        <form className="mt-6 space-y-4" onSubmit={savePassword}>
          <label>
            <span>当前密码</span>
            <input
              autoComplete="current-password"
              required
              type="password"
              value={passwordForm.old_password}
              onChange={(event) => setPasswordForm((current) => ({ ...current, old_password: event.target.value }))}
            />
          </label>
          <label>
            <span>新密码</span>
            <input
              autoComplete="new-password"
              minLength={8}
              required
              type="password"
              value={passwordForm.new_password}
              onChange={(event) => setPasswordForm((current) => ({ ...current, new_password: event.target.value }))}
            />
          </label>
          <label>
            <span>确认新密码</span>
            <input
              autoComplete="new-password"
              minLength={8}
              required
              type="password"
              value={passwordForm.confirm_password}
              onChange={(event) => setPasswordForm((current) => ({ ...current, confirm_password: event.target.value }))}
            />
          </label>
          <button className="btn-primary" type="submit" disabled={passwordMutation.isPending}>
            {passwordMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
            更新密码
          </button>
        </form>
      </section>
    </div>
  );
}
