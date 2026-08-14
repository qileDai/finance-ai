import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { useAuth } from "../auth";

const TITLES: Record<string, string> = {
  "/": "概览",
  "/sessions": "会话材料",
  "/register": "快速注册",
  "/wework-send": "外部群发消息",
  "/jobs": "注册任务",
  "/quality": "回答质量",
};

type Props = {
  onRefresh: () => void;
  toast: string;
};

export function Layout({ onRefresh, toast }: Props) {
  const loc = useLocation();
  const nav = useNavigate();
  const { user, logout } = useAuth();
  const base = loc.pathname.replace(/\/$/, "") || "/";
  const title =
    TITLES[base] ||
    (base.startsWith("/sessions/")
      ? "会话详情"
      : base.startsWith("/jobs/")
        ? "任务详情"
        : "管理后台");

  const [tick, setTick] = useState(0);
  useEffect(() => {
    setTick((t) => t + 1);
  }, [loc.pathname, loc.search]);

  async function onLogout() {
    await logout();
    nav("/login", { replace: true });
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <img className="brand-logo" src="/admin/logo.png" alt="赢态" width={40} height={40} />
          <div>
            <div className="brand-name">赢态 Finance AI</div>
            <div className="brand-sub">运营管理后台</div>
          </div>
        </div>
        <nav className="nav">
          <NavLink to="/" end>
            概览
          </NavLink>
          <NavLink to="/sessions">会话材料</NavLink>
          <NavLink to="/register">快速注册</NavLink>
          <NavLink to="/wework-send">外部群发消息</NavLink>
          <NavLink to="/jobs">注册任务</NavLink>
          <NavLink to="/quality">回答质量</NavLink>
        </nav>
      </aside>
      <div className="main">
        <header className="topbar">
          <div>
            <h1>{title}</h1>
            <div className="topbar-meta">
              近 24h 指标 · {user ? `已登录 ${user}` : ""}
            </div>
          </div>
          <div className="topbar-actions">
            <a href="/health" target="_blank" rel="noreferrer">
              /health
            </a>
            <button type="button" className="btn" onClick={onRefresh}>
              刷新
            </button>
            <button type="button" className="btn btn-primary" onClick={onLogout}>
              退出
            </button>
          </div>
        </header>
        <main className="content" key={tick}>
          <Outlet />
        </main>
      </div>
      {toast ? <div className="toast">{toast}</div> : null}
    </div>
  );
}
