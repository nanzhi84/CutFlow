import {
  BarChart3,
  Home,
  KeyRound,
  Library,
  LogOut,
  MessageSquareText,
  RadioTower,
  Sparkles,
  UserCircle2,
} from "lucide-react";
import { Suspense } from "react";
import type { ComponentType } from "react";
import { NavLink, Outlet, useLocation, useNavigate, useParams } from "react-router-dom";
import { routes } from "../routes";
import { useAuth } from "../pages/auth/AuthContext";
import { ConnectionStatus } from "./ConnectionStatus";
import { LoadingState } from "./ui/State";

const mainNav = [
  { to: routes.overview(), label: "概览", icon: Home, end: true },
  { to: routes.studio(), label: "案例中心", icon: Sparkles },
  { to: routes.library(), label: "素材库", icon: Library },
  { to: routes.analytics(), label: "数据统计", icon: BarChart3 },
  { to: routes.account(), label: "账户中心", icon: UserCircle2 },
  { to: routes.settings(), label: "设置", icon: KeyRound },
  { to: routes.promptOps(), label: "提示词", icon: MessageSquareText },
  { to: routes.publishOps(), label: "发布运维", icon: RadioTower },
] satisfies Array<{ to: string; label: string; icon: ComponentType<{ className?: string }>; end?: boolean }>;

const mobilePrimaryPaths = [routes.overview(), routes.studio(), routes.library(), routes.account()];
const mobilePrimaryItems = mainNav.filter((item) => mobilePrimaryPaths.includes(item.to));

function roleLabel(role?: string) {
  if (role === "admin") return "管理员";
  if (role === "operator") return "运营成员";
  return "查看成员";
}

function Breadcrumbs() {
  const location = useLocation();
  const params = useParams();
  const parts = ["树影cutagent"];
  if (location.pathname === "/") {
    parts.push("概览");
  } else if (location.pathname.startsWith("/studio")) {
    parts.push(params.caseId ? "案例工作台" : "案例中心");
  }
  if (location.pathname.endsWith("/outputs")) {
    parts.push("成片");
  } else if (location.pathname.startsWith("/settings")) {
    parts.push("设置");
  } else if (location.pathname.startsWith("/library")) {
    parts.push("素材库");
  } else if (location.pathname.startsWith("/analytics")) {
    parts.push("数据统计");
  } else if (location.pathname.startsWith("/account")) {
    parts.push("账户中心");
  } else if (location.pathname.startsWith("/ops/prompts")) {
    parts.push("提示词");
  } else if (location.pathname.startsWith("/publish-ops")) {
    parts.push("发布运维");
  }
  return <div className="text-xs font-medium text-text-tertiary">{parts.join(" / ")}</div>;
}

export function AppShell() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const initials = (user?.display_name || user?.email || "U").slice(0, 1).toUpperCase();

  async function handleLogout() {
    await logout();
    navigate(routes.login(), { replace: true });
  }

  return (
    <div className="app-shell min-h-screen lg:flex">
      <aside className="sticky top-0 hidden h-screen w-[188px] shrink-0 flex-col border-r border-border/70 bg-[linear-gradient(180deg,rgba(252,252,247,0.92)_0%,rgba(244,245,239,0.96)_100%)] px-2 py-4 backdrop-blur-xl lg:flex">
        <NavLink to={routes.overview()} className="group flex items-center gap-2.5 rounded-xl py-1 transition-colors hover:bg-white/45">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-[#d6ff48] shadow-[0_8px_20px_rgba(214,255,72,0.14)] transition-transform group-hover:scale-[1.02]">
            <Sparkles className="h-4 w-4 text-[#1b1d1a]" />
          </span>
          <span className="min-w-0">
            <span className="font-display block text-xl leading-none text-text-primary">树影cutagent</span>
            <span className="mt-1 block truncate text-[0.6875rem] text-text-tertiary">工作台</span>
          </span>
        </NavLink>

        <div className="my-3 border-b border-border/60" />

        <nav className="flex-1 space-y-1 overflow-y-auto" aria-label="主导航">
          {mainNav.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}
                end={item.end}
                key={item.to}
                to={item.to}
              >
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border/70 bg-white/55">
                  <Icon className="h-4 w-4" />
                </span>
                <span className="truncate font-medium">{item.label}</span>
              </NavLink>
            );
          })}
        </nav>

        <div className="grid gap-3 border-t border-border/70 pt-3">
          <ConnectionStatus />
          <div className="flex items-center gap-2.5 py-1">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-brand text-sm font-bold text-[#1b1d1a]">
              {initials}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-text-primary">{user?.display_name || user?.email || "未登录"}</p>
              <p className="truncate text-xs text-text-secondary">{roleLabel(user?.role)}</p>
            </div>
            <button
              type="button"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-border/70 bg-white/55 text-text-secondary transition-colors hover:border-status-error/30 hover:bg-status-error/5 hover:text-status-error"
              onClick={() => void handleLogout()}
              aria-label="退出登录"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        </div>
      </aside>

      <header className="sticky top-0 z-40 border-b border-border/70 bg-background/92 px-3 py-3 backdrop-blur-xl lg:hidden">
        <div className="flex items-center justify-between gap-3">
          <NavLink to={routes.overview()} className="flex min-w-0 items-center gap-3">
            <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-[#d6ff48] shadow-[0_10px_24px_rgba(214,255,72,0.16)]">
              <Sparkles className="h-4 w-4 text-[#1b1d1a]" />
            </span>
            <span className="min-w-0">
              <span className="font-display block text-xl leading-none text-text-primary">树影cutagent</span>
              <span className="block truncate text-xs text-text-secondary">{user?.display_name || user?.email || "移动工作台"}</span>
            </span>
          </NavLink>
          <div className="flex shrink-0 items-center gap-2">
            <ConnectionStatus />
            <button
              className="flex h-10 w-10 items-center justify-center rounded-2xl border border-border/70 bg-white/65 text-text-secondary"
              onClick={() => void handleLogout()}
              type="button"
              aria-label="退出登录"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        </div>

        <nav className="mobile-nav-scroll -mx-1 mt-3 flex gap-2 overflow-x-auto px-1 pb-1" aria-label="移动主导航">
          {mainNav.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) => `mobile-nav-pill ${isActive ? "active" : ""}`}
              >
                <Icon className="h-4 w-4" />
                <span>{item.label}</span>
              </NavLink>
            );
          })}
        </nav>
      </header>

      <nav className="fixed inset-x-3 bottom-3 z-40 grid grid-cols-4 rounded-[24px] border border-border/80 bg-white/88 p-1.5 shadow-glow backdrop-blur-xl lg:hidden">
        {mobilePrimaryItems.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `mobile-tab-item ${isActive ? "active" : ""}`}
            >
              <Icon className="h-5 w-5" />
              <span>{item.label}</span>
            </NavLink>
          );
        })}
      </nav>

      <main className="min-w-0 flex-1 px-4 py-5 pb-28 lg:px-7 lg:py-6 lg:pb-8">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <Breadcrumbs />
        </div>
        <Suspense fallback={<LoadingState label="正在加载页面" block />}>
          <Outlet />
        </Suspense>
      </main>
    </div>
  );
}
