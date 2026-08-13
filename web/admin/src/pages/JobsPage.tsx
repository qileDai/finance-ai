import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type JobRow } from "../api";
import { formatDateTime } from "../format";
import { StateBox, statusBadge } from "../components/ui";

type Props = {
  refreshKey: number;
  onToast: (msg: string) => void;
  onRefresh: () => void;
};

export function JobsPage({ refreshKey, onToast, onRefresh }: Props) {
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<JobRow[]>([]);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [sortKey, setSortKey] = useState<"id" | "status" | "updated_at">("id");
  const [sortAsc, setSortAsc] = useState(false);
  const nav = useNavigate();

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    api
      .jobs(status, 80)
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
  }, [status, refreshKey]);

  const sorted = useMemo(() => {
    const copy = [...items];
    copy.sort((a, b) => {
      const av = String(a[sortKey] ?? "");
      const bv = String(b[sortKey] ?? "");
      if (sortKey === "id") {
        return sortAsc ? a.id - b.id : b.id - a.id;
      }
      return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    return copy;
  }, [items, sortKey, sortAsc]);

  function toggleSort(key: typeof sortKey) {
    if (sortKey === key) setSortAsc(!sortAsc);
    else {
      setSortKey(key);
      setSortAsc(key !== "id");
    }
  }

  async function act(id: number, kind: "cancel" | "requeue") {
    const label = kind === "cancel" ? "取消" : "重跑";
    if (!window.confirm(`确认${label}任务 #${id}？`)) return;
    setBusyId(id);
    try {
      const res =
        kind === "cancel" ? await api.cancelJob(id) : await api.requeueJob(id);
      onToast(res.message || `${label}成功`);
      onRefresh();
    } catch (e) {
      onToast((e as Error).message || `${label}失败`);
    } finally {
      setBusyId(null);
    }
  }

  return (
    <>
      <div className="toolbar">
        {[
          ["", "全部"],
          ["pending", "pending"],
          ["running", "running"],
          ["succeeded", "succeeded"],
          ["failed", "failed"],
          ["cancelled", "cancelled"],
        ].map(([v, label]) => (
          <button
            key={v || "all"}
            type="button"
            className={`chip ${status === v ? "active" : ""}`}
            onClick={() => setStatus(v)}
          >
            {label}
          </button>
        ))}
      </div>
      <StateBox loading={loading} error={error} empty={!sorted.length}>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>
                  <button type="button" className="btn btn-sm" onClick={() => toggleSort("id")}>
                    ID
                  </button>
                </th>
                <th>来源</th>
                <th>公司</th>
                <th>roomid</th>
                <th>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => toggleSort("status")}
                  >
                    状态
                  </button>
                </th>
                <th>尝试</th>
                <th>dry/submit</th>
                <th>错误</th>
                <th>
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => toggleSort("updated_at")}
                  >
                    更新
                  </button>
                </th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((j) => (
                <tr
                  key={j.id}
                  className="clickable"
                  onClick={() => nav(`/jobs/${j.id}`)}
                >
                  <td className="mono">
                    <Link to={`/jobs/${j.id}`} onClick={(e) => e.stopPropagation()}>
                      #{j.id}
                    </Link>
                  </td>
                  <td className="mono muted">{j.source || "-"}</td>
                  <td>{j.company_name || "-"}</td>
                  <td className="mono">{j.roomid}</td>
                  <td>
                    <span className={statusBadge(j.status)}>{j.status}</span>
                  </td>
                  <td className="mono">
                    {j.attempts ?? 0}/{j.max_attempts ?? 0}
                  </td>
                  <td className="mono">
                    {j.dry_run ? "Y" : "N"}/{j.allow_submit ? "Y" : "N"}
                  </td>
                  <td title={String(j.last_error || "")}>
                    {String(j.last_error || "").slice(0, 60)}
                  </td>
                  <td className="mono muted">{formatDateTime(j.updated_at)}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    {j.status === "pending" ? (
                      <button
                        type="button"
                        className="btn btn-sm btn-danger"
                        disabled={busyId === j.id}
                        onClick={() => act(j.id, "cancel")}
                      >
                        取消
                      </button>
                    ) : null}
                    {j.status === "failed" || j.status === "cancelled" ? (
                      <button
                        type="button"
                        className="btn btn-sm btn-primary"
                        disabled={busyId === j.id}
                        onClick={() => act(j.id, "requeue")}
                      >
                        重跑
                      </button>
                    ) : null}
                    {j.status !== "pending" &&
                    j.status !== "failed" &&
                    j.status !== "cancelled"
                      ? "-"
                      : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </StateBox>
    </>
  );
}
