import { FormEvent, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";

export function LoginPage() {
  const { user, loading, setUser } = useAuth();
  const nav = useNavigate();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (!loading && user) {
    return <Navigate to="/" replace />;
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await api.login(username, password);
      setUser(res.username);
      nav("/", { replace: true });
    } catch (err) {
      setError((err as Error).message || "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-page">
      <div className="login-panel">
        <div className="login-brand-row">
          <img className="login-logo" src="/admin/logo.png" alt="赢态" width={48} height={48} />
          <div>
            <div className="login-brand">赢态 Finance AI</div>
            <p className="login-sub">运营管理后台</p>
          </div>
        </div>
        <form className="login-form" onSubmit={onSubmit}>
          <label>
            用户名
            <input
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </label>
          <label>
            密码
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error ? <div className="login-error">{error}</div> : null}
          <button type="submit" className="btn btn-primary login-btn" disabled={busy}>
            {busy ? "登录中…" : "登录"}
          </button>
        </form>
        <p className="login-hint muted">使用 .env 中 ADMIN_USERNAME / ADMIN_PASSWORD</p>
      </div>
    </div>
  );
}
