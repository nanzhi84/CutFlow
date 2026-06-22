import { useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { KeyRound, Loader2 } from "lucide-react";
import { ErrorState } from "../../components/State";
import { useAuth } from "./AuthContext";
import { routes } from "../../routes";

type LocationState = {
  from?: { pathname?: string };
};

export default function LoginPage() {
  const { isAuthenticated, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const state = location.state as LocationState | null;
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [submitting, setSubmitting] = useState(false);

  if (isAuthenticated) {
    return <Navigate to={routes.studio()} replace />;
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login({ identifier: identifier.trim(), password });
      navigate(state?.from?.pathname ?? routes.studio(), { replace: true });
    } catch (nextError) {
      setError(nextError);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="loginPage">
      <form className="loginPanel" onSubmit={handleSubmit}>
        <div className="loginMark">
          <KeyRound size={22} />
        </div>
        <div>
          <h1>登录 树影cutagent</h1>
          <p>进入 Case-first 前端工作台</p>
        </div>
        <label>
          <span>邮箱/用户名</span>
          <input
            autoComplete="username"
            name="identifier"
            type="text"
            value={identifier}
            onChange={(event) => setIdentifier(event.target.value)}
            required
          />
        </label>
        <label>
          <span>密码</span>
          <input
            autoComplete="current-password"
            name="password"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        {error ? <ErrorState error={error} /> : null}
        <button className="primaryButton full" type="submit" disabled={submitting}>
          {submitting ? <Loader2 size={16} className="spin" /> : <KeyRound size={16} />}
          <span>登录</span>
        </button>
        <div className="flex items-center justify-between gap-3 text-sm">
          <span className="text-text-secondary">还没有账户？</span>
          <Link className="font-medium text-accent hover:text-accent-hover" to={routes.register()}>
            使用注册码注册
          </Link>
        </div>
      </form>
    </main>
  );
}
