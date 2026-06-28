import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, CheckCircle2, KeyRound, Loader2, ShieldCheck, Sparkles } from "lucide-react";
import { useState } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { ErrorState } from "../../components/ui/State";
import { useToast } from "../../components/ui/Toast";
import { routes } from "../../routes";
import { useAuth } from "./AuthContext";

function safeNextPath(raw: string | null) {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return routes.studio();
  if (raw === routes.login() || raw === routes.register()) return routes.studio();
  return raw;
}

export default function RegisterPage() {
  const { isAuthenticated } = useAuth();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();
  const nextPath = safeNextPath(searchParams.get("next"));
  const [form, setForm] = useState({
    email: "",
    display_name: "",
    registration_code: "",
    password: "",
    confirm_password: "",
  });
  const registerMutation = useMutation({
    mutationFn: api.auth.register,
    onSuccess: (data) => {
      queryClient.setQueryData(["auth", "session"], data.session);
      toast.success("注册成功", "已自动登录");
      navigate(nextPath, { replace: true });
    },
  });

  if (isAuthenticated) return <Navigate to={routes.studio()} replace />;

  function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (form.password !== form.confirm_password) {
      toast.warning("两次输入的密码不一致");
      return;
    }
    registerMutation.mutate({
      email: form.email.trim(),
      display_name: form.display_name.trim() || form.email.trim(),
      registration_code: form.registration_code.trim(),
      password: form.password,
    });
  }

  return (
    <main className="min-h-screen overflow-hidden bg-background px-5 py-8">
      <div className="mx-auto grid min-h-[calc(100vh-4rem)] max-w-6xl items-center gap-6 lg:grid-cols-[1.04fr_0.96fr]">
        <section className="hidden rounded-[32px] border border-border/70 bg-white/55 p-8 shadow-glow backdrop-blur lg:block">
          <div className="flex items-center gap-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-[#d6ff48] text-[#1b1d1a] shadow-[0_12px_30px_rgba(214,255,72,0.16)]">
              <Sparkles className="h-6 w-6" />
            </div>
            <div>
              <p className="text-xs text-text-tertiary">Public Access</p>
              <h1 className="font-display mt-1 text-4xl leading-none text-text-primary">树影cutagent</h1>
            </div>
          </div>

          <div className="mt-10 space-y-5">
            <div className="rounded-[26px] border border-border/70 bg-background-secondary/80 p-5">
              <p className="text-lg font-medium text-text-primary">使用管理员发放的注册码创建账户。</p>
              <p className="mt-3 text-sm leading-7 text-text-secondary">
                注册成功后会直接进入工作台。注册码可能有次数和过期时间限制，明文码不会在系统列表中再次显示。
              </p>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              {["统一会话保护", "注册码控制新成员", "角色权限隔离", "中文状态与时间"].map((item) => (
                <div className="flex items-center gap-2 rounded-2xl border border-border/70 bg-white/55 p-4 text-sm text-text-secondary" key={item}>
                  <CheckCircle2 className="h-4 w-4 text-status-success" />
                  {item}
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="mx-auto w-full max-w-xl rounded-[32px] border border-border bg-surface/95 p-7 shadow-2xl shadow-black/10 backdrop-blur lg:p-9">
          <div className="flex items-center gap-3 lg:hidden">
            <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-[#d6ff48] text-[#1b1d1a]">
              <Sparkles className="h-5 w-5" />
            </div>
            <div>
              <p className="text-xs text-text-tertiary">树影cutagent</p>
              <h1 className="font-display text-2xl leading-none text-text-primary">注册</h1>
            </div>
          </div>

          <div className="mt-6">
            <div className="inline-flex items-center gap-2 rounded-full border border-accent/20 bg-accent/10 px-3 py-1 text-xs font-medium text-accent">
              <ShieldCheck className="h-4 w-4" />
              注册入口
            </div>
            <h2 className="font-display mt-4 text-4xl leading-none text-text-primary">创建账户</h2>
            <p className="mt-2 text-sm leading-6 text-text-secondary">输入邮箱、密码和管理员发放的注册码。</p>
          </div>

          <form className="mt-7 space-y-4" onSubmit={submit}>
            <label>
              <span>邮箱</span>
              <input
                autoComplete="email"
                required
                type="email"
                value={form.email}
                onChange={(event) => setForm((current) => ({ ...current, email: event.target.value }))}
                placeholder="name@example.com"
              />
            </label>
            <label>
              <span>显示名称</span>
              <input
                autoComplete="nickname"
                value={form.display_name}
                onChange={(event) => setForm((current) => ({ ...current, display_name: event.target.value }))}
                placeholder="例如：运营团队 / 张三"
              />
            </label>
            <label>
              <span>注册码</span>
              <input
                autoComplete="one-time-code"
                required
                value={form.registration_code}
                onChange={(event) => setForm((current) => ({ ...current, registration_code: event.target.value }))}
                placeholder="请输入管理员发放的注册码"
              />
            </label>
            <label>
              <span>密码</span>
              <input
                autoComplete="new-password"
                minLength={8}
                required
                type="password"
                value={form.password}
                onChange={(event) => setForm((current) => ({ ...current, password: event.target.value }))}
                placeholder="至少 8 位"
              />
            </label>
            <label>
              <span>确认密码</span>
              <input
                autoComplete="new-password"
                minLength={8}
                required
                type="password"
                value={form.confirm_password}
                onChange={(event) => setForm((current) => ({ ...current, confirm_password: event.target.value }))}
              />
            </label>

            {registerMutation.error ? <ErrorState error={registerMutation.error} /> : null}

            <button className="btn-primary w-full py-3 text-base" type="submit" disabled={registerMutation.isPending}>
              {registerMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
              创建并登录
              <ArrowRight className="h-4 w-4" />
            </button>
          </form>

          <div className="mt-6 flex flex-wrap items-center justify-between gap-3 text-sm">
            <span className="text-text-secondary">已经有账户了？</span>
            <Link className="font-medium text-accent hover:text-accent-hover" to={routes.login()}>
              去登录
            </Link>
          </div>
        </section>
      </div>
    </main>
  );
}
