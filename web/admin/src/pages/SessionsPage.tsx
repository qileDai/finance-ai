import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, type MaterialRow, type SessionSummary } from "../api";
import { StateBox, statusBadge } from "../components/ui";

export function SessionsPage({ refreshKey }: { refreshKey: number }) {
  const [params, setParams] = useSearchParams();
  const channel = params.get("channel") || "all";
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<SessionSummary[]>([]);
  const nav = useNavigate();

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    api
      .sessions(channel)
      .then((d) => {
        if (alive) setItems(d.items || []);
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
  }, [channel, refreshKey]);

  return (
    <>
      <div className="toolbar">
        {(["all", "group", "kf"] as const).map((c) => (
          <button
            key={c}
            type="button"
            className={`chip ${channel === c ? "active" : ""}`}
            onClick={() => setParams(c === "all" ? {} : { channel: c })}
          >
            {c === "all" ? "全部" : c === "group" ? "群 wr*" : "客服 kf:*"}
          </button>
        ))}
      </div>
      <StateBox loading={loading} error={error} empty={!items.length}>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>通道</th>
                <th>roomid</th>
                <th>名称</th>
                <th>状态</th>
                <th>材料数</th>
                <th>公司</th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <tr
                  key={r.roomid}
                  className="clickable"
                  onClick={() => nav(`/sessions/${encodeURIComponent(r.roomid)}`)}
                >
                  <td>{r.channel || ""}</td>
                  <td className="mono">{r.roomid}</td>
                  <td>{r.label || r.name || ""}</td>
                  <td>
                    <span className={statusBadge(r.status)}>{r.status || "-"}</span>
                  </td>
                  <td className="mono">{r.material_count ?? 0}</td>
                  <td>{r.company_name || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </StateBox>
    </>
  );
}

export function SessionDetailPage({ refreshKey }: { refreshKey: number }) {
  const { roomid = "" } = useParams();
  const decoded = decodeURIComponent(roomid);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [session, setSession] = useState<SessionSummary | null>(null);
  const [materials, setMaterials] = useState<MaterialRow[]>([]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    api
      .session(decoded)
      .then((d) => {
        if (!alive) return;
        setSession(d.session);
        setMaterials(d.materials || []);
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
  }, [decoded, refreshKey]);

  return (
    <>
      <div className="toolbar">
        <Link className="btn" to="/sessions">
          ← 返回列表
        </Link>
      </div>
      <StateBox loading={loading} error={error}>
        {session ? (
          <div className="panel">
            <h2>{session.label || session.name || session.roomid}</h2>
            <dl className="detail-grid">
              <div>
                <dt>roomid</dt>
                <dd>{session.roomid}</dd>
              </div>
              <div>
                <dt>状态</dt>
                <dd>
                  <span className={statusBadge(session.status)}>{session.status}</span>
                </dd>
              </div>
              <div>
                <dt>公司</dt>
                <dd>{session.company_name || "-"}</dd>
              </div>
              <div>
                <dt>open_kfid</dt>
                <dd>{session.open_kfid || "-"}</dd>
              </div>
            </dl>
          </div>
        ) : null}
        <div className="panel">
          <h2>材料项</h2>
          <StateBox empty={!materials.length}>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>field</th>
                    <th>状态</th>
                    <th>值</th>
                    <th>文件</th>
                    <th>更新</th>
                  </tr>
                </thead>
                <tbody>
                  {materials.map((m) => (
                    <tr key={String(m.field_key)}>
                      <td className="mono">{m.field_key}</td>
                      <td>
                        <span className={statusBadge(m.status)}>{m.status || "-"}</span>
                      </td>
                      <td>{String(m.value_text || "").slice(0, 80)}</td>
                      <td className="mono muted">
                        {String(m.file_path || "").slice(0, 40)}
                      </td>
                      <td className="mono muted">{m.updated_at || ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </StateBox>
        </div>
      </StateBox>
    </>
  );
}
