import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { pct, StateBox } from "../components/ui";

export function OverviewPage({ refreshKey }: { refreshKey: number }) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<Awaited<ReturnType<typeof api.overview>> | null>(
    null,
  );

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    api
      .overview()
      .then((d) => {
        if (alive) setData(d);
      })
      .catch((e: Error) => {
        if (alive) setError(e.message || "加载失败");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [refreshKey]);

  const c = (data?.conversation || {}) as Record<string, unknown>;
  const r = (data?.registration || {}) as Record<string, unknown>;
  const w = (data?.icris_worker || {}) as Record<string, unknown>;
  const actions = (c.actions || {}) as Record<string, number>;
  const latency = (c.qa_latency_ms || {}) as Record<string, number>;
  const fails = (r.recent_failures || []) as Array<Record<string, unknown>>;

  return (
    <StateBox loading={loading} error={error}>
      <div className="kpi-row">
        <div className="kpi">
          <div className="kpi-label">QA runs</div>
          <div className="kpi-value">{Number(c.agent_runs_total || 0)}</div>
          <div className="kpi-sub">reply {pct(c.reply_rate)}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">平均置信度</div>
          <div className="kpi-value">{Number(c.avg_confidence || 0).toFixed(2)}</div>
          <div className="kpi-sub">低置信 {Number(c.low_confidence_count || 0)}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">注册成功率</div>
          <div className="kpi-value">{pct(r.success_rate)}</div>
          <div className="kpi-sub">
            pending {Number(r.pending_count || 0)} · running {Number(r.running_count || 0)}
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Inbox / 发送失败</div>
          <div className="kpi-value">
            {Number(c.inbox_unprocessed || 0)}
            <span className="muted" style={{ fontSize: "1rem" }}>
              {" "}
              / {Number(c.send_failures || 0)}
            </span>
          </div>
          <div className="kpi-sub">
            Worker {w.alive ? "alive" : "down"}
            {w.enabled === false ? " (disabled)" : ""}
          </div>
        </div>
      </div>

      <div className="panel">
        <h2>快捷入口</h2>
        <div className="toolbar">
          <Link className="btn" to="/quality">
            回答质量
          </Link>
          <Link className="btn" to="/jobs">
            注册任务
          </Link>
          <Link className="btn" to="/sessions">
            会话材料
          </Link>
        </div>
        <p className="muted" style={{ margin: 0 }}>
          silent {pct(c.silent_rate)} · abstain {pct(c.abstain_rate)} · human{" "}
          {pct(c.human_transfer_rate)} · actions{" "}
          {Object.entries(actions)
            .map(([k, v]) => `${k}=${v}`)
            .join(", ") || "无"}{" "}
          · QA p50 {latency.p50 ?? 0}ms / p95 {latency.p95 ?? 0}ms
        </p>
      </div>

      <div className="panel">
        <h2>近 24h 失败注册</h2>
        <StateBox empty={!fails.length}>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>roomid</th>
                  <th>错误</th>
                  <th>截图</th>
                </tr>
              </thead>
              <tbody>
                {fails.map((f) => (
                  <tr key={String(f.id)}>
                    <td className="mono">{String(f.id)}</td>
                    <td className="mono">{String(f.roomid || "")}</td>
                    <td>{String(f.last_error || "").slice(0, 120)}</td>
                    <td className="mono muted">
                      {String(f.screenshot_path || "").slice(0, 48)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </StateBox>
      </div>
    </StateBox>
  );
}
