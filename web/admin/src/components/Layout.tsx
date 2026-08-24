import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { useAuth } from "../auth";
import { Menu } from "antd";
import {
  AppstoreOutlined,
  BarChartOutlined,
  FormOutlined,
  IdcardOutlined,
  MailOutlined,
  ScheduleOutlined,
  SendOutlined,
  TeamOutlined,
} from "@ant-design/icons";

const TITLES: Record<string, string> = {
  "/": "概览",
  "/sessions": "会话材料",
  "/register": "快速注册",
  "/id-extract": "证件识别",
  "/wework-send": "外部群发消息",
  "/jobs": "注册任务",
  "/email-config": "邮箱配置",
  "/quality": "回答质量",
};

type NavGroup = {
  label: string;
  items: { to: string; label: string; icon: React.ReactNode }[];
};

const NAV_GROUPS: NavGroup[] = [
  {
    label: "运营",
    items: [
      { to: "/", label: "概览", icon: <AppstoreOutlined /> },
      { to: "/sessions", label: "会话材料", icon: <TeamOutlined /> },
      { to: "/jobs", label: "注册任务", icon: <ScheduleOutlined /> },
      { to: "/quality", label: "回答质量", icon: <BarChartOutlined /> },
    ],
  },
  {
    label: "工具",
    items: [
      { to: "/id-extract", label: "证件识别", icon: <IdcardOutlined /> },
      { to: "/register", label: "快速注册", icon: <FormOutlined /> },
      { to: "/wework-send", label: "外部群发消息", icon: <SendOutlined /> },
    ],
  },
  {
    label: "系统",
    items: [
      { to: "/email-config", label: "邮箱配置", icon: <MailOutlined /> },
    ],
  },
];

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
          <Menu
            mode="inline"
            selectedKeys={[base]}
            items={NAV_GROUPS.map((g) => ({
              key: g.label,
              label: g.label,
              type: "group" as const,
              children: g.items.map((it) => ({
                key: it.to,
                icon: it.icon,
                label: it.label,
              })),
            }))}
            onClick={({ key }) => nav(key)}
            style={{ border: "none", background: "transparent", fontSize: 13 }}
            theme="dark"
          />
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
