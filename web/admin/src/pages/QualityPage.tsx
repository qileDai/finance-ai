import { useEffect, useState } from "react";
import { api, type LowRun } from "../api";
import { pct, StateBox, statusBadge } from "../components/ui";

export function QualityPage({ refreshKey }: { refreshKey: number }) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stats, setStats] = useState<Record<string, unknown>>({});
  const [runs, setRuns] = useState<LowRun[]>([]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    api
      .quality(24)
      .then((d) => {
        if (!alive) return;
        setStats(d.stats || {});
        setRuns(d.low_confidence_runs || []);
      })
      .catch((e: Error) => {
        if (alive) setError(e.message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [refreshKey]);

  const actions = (stats.actions || {}) as Record<string, number>;
  const latency = (stats.qa_latency_ms || {}) as Record<string, number>;
  const routes = (stats.intent_routes || {}) as Record<string, unknown>;

  return (
    <StateBox loading={loading} error={error}>
      <div className="kpi-row">
        <div className="kpi">
          <div className="kpi-label">总 runs</div>
          <div className="kpi-value">{Number(stats.agent_runs_total || 0)}</div>
          <div className="kpi-sub">reply {pct(stats.reply_rate)}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">置信度 / answer</div>
          <div className="kpi-value">
            {Number(stats.avg_confidence || 0).toFixed(2)}
          </div>
          <div className="kpi-sub">
            answer {Number(stats.avg_answer_score || 0).toFixed(2)} · retrieval{" "}
            {Number(stats.avg_retrieval_score || 0).toFixed(2)}
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">silent / abstain / human</div>
          <div className="kpi-value" style={{ fontSize: "1.1rem" }}>
            {pct(stats.silent_rate)} / {pct(stats.abstain_rate)} /{" "}
            {pct(stats.human_transfer_rate)}
          </div>
          <div className="kpi-sub">
            {Object.entries(actions)
              .map(([k, v]) => `${k}=${v}`)
              .join(" · ") || "无 action"}
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">QA 延迟 p50 / p95</div>
          <div className="kpi-value" style={{ fontSize: "1.25rem" }}>
            {latency.p50 ?? 0}
            <span className="muted"> / </span>
            {latency.p95 ?? 0}
            <span className="muted" style={{ fontSize: "0.85rem" }}>
              {" "}
              ms
            </span>
          </div>
          <div className="kpi-sub">
            intent total {Number(routes.intent_routes_total || 0)} · model{" "}
            {pct(routes.model_invoke_rate)} · veto {pct(routes.veto_rate)}
          </div>
        </div>
      </div>

      <div className="panel">
        <h2>低置信 / 非 reply（最近 50）</h2>
        <StateBox empty={!runs.length}>
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>roomid</th>
                  <th>action</th>
                  <th>confidence</th>
                  <th>answer</th>
                  <th>retrieval</th>
                  <th>ms</th>
                  <th>问题</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r, i) => (
                  <tr key={r.id || `${r.created_at}-${i}`}>
                    <td className="mono muted">{r.created_at || ""}</td>
                    <td className="mono">{r.roomid || ""}</td>
                    <td>
                      <span className={statusBadge(r.action)}>{r.action || "-"}</span>
                    </td>
                    <td className="mono">{r.confidence ?? ""}</td>
                    <td className="mono">{r.answer_score ?? ""}</td>
                    <td className="mono">{r.retrieval_score ?? ""}</td>
                    <td className="mono">{r.duration_ms ?? 0}</td>
                    <td>{String(r.question || "").slice(0, 80)}</td>
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
