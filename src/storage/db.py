"""企业微信外部群 SQLite 存储"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from config.settings import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "wework_external.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_DURATION_RE = re.compile(r"^(\d+)分(\d+)秒$")


def format_job_run_duration(seconds: float) -> str:
    """墙钟秒数 →「M分S秒」，供 registration_jobs 耗时列存储与展示。"""
    total = max(0, int(round(float(seconds or 0))))
    minutes, secs = divmod(total, 60)
    return f"{minutes}分{secs}秒"


def parse_job_run_duration(text: str) -> int | None:
    """「M分S秒」→ 秒。空串或格式不对返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    m = _DURATION_RE.fullmatch(raw)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _parse_iso_dt(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def duration_since_started(started_at: str, ended_at: str | None = None) -> str:
    """从 started_at 到 ended_at（默认现在）的「M分S秒」。解析失败返回空串。"""
    start = _parse_iso_dt(started_at)
    if start is None:
        return ""
    end = _parse_iso_dt(ended_at or _utc_now())
    if end is None:
        end = datetime.now(timezone.utc)
    return format_job_run_duration((end - start).total_seconds())


def _percentile_stats(values: list[int]) -> dict[str, Any]:
    """返回 count / p50 / p95 / max（毫秒）。"""
    if not values:
        return {"count": 0, "p50": 0, "p95": 0, "max": 0}
    n = len(values)
    sorted_v = sorted(values)

    def _pct(p: float) -> int:
        if n == 1:
            return sorted_v[0]
        idx = min(n - 1, max(0, int(round((p / 100.0) * (n - 1)))))
        return int(sorted_v[idx])

    return {
        "count": n,
        "p50": _pct(50),
        "p95": _pct(95),
        "max": int(sorted_v[-1]),
    }


def _seconds_stats(values: list[int]) -> dict[str, Any]:
    """耗时样本 → count / 平均 / P50 / P95（秒与分钟）。"""
    pct = _percentile_stats(values)
    n = int(pct["count"])
    avg = (sum(values) / n) if n else 0.0
    p50 = int(pct["p50"])
    p95 = int(pct["p95"])
    return {
        "count": n,
        "avg_seconds": round(avg, 1) if n else 0,
        "p50_seconds": p50,
        "p95_seconds": p95,
        "avg_minutes": round(avg / 60.0, 2) if n else 0.0,
        "p50_minutes": round(p50 / 60.0, 2) if n else 0.0,
        "p95_minutes": round(p95 / 60.0, 2) if n else 0.0,
    }


def _succ_fail_rate(success: int, failed: int) -> tuple[float, float]:
    done = int(success) + int(failed)
    if done <= 0:
        return 0.0, 0.0
    return round(success / done, 4), round(failed / done, 4)


def _first_ts(*vals: Any) -> str:
    for v in vals:
        raw = str(v or "").strip()
        if raw:
            return raw
    return ""


def _ts_in_window(ts: str, cutoff: str | None) -> bool:
    if not cutoff:
        return True
    raw = (ts or "").strip()
    if not raw:
        return False
    return raw >= cutoff


def _iso_date(value: str) -> str:
    dt = _parse_iso_dt(value)
    if dt is not None:
        return dt.astimezone(timezone.utc).date().isoformat()
    raw = (value or "").strip()
    return raw[:10] if len(raw) >= 10 else ""


def _stage_payload(
    success: int,
    failed: int,
    duration: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sr, fr = _succ_fail_rate(success, failed)
    out: dict[str, Any] = {
        "success": int(success),
        "failed": int(failed),
        "success_rate": sr,
        "fail_rate": fr,
        "duration": duration,
    }
    if extra:
        out.update(extra)
    return out


def _source_bucket(source: str) -> str:
    src = (source or "").strip().lower()
    if src == "admin":
        return "admin"
    if src == "wework":
        return "wework"
    return "other"


@dataclass
class ExternalGroupStore:
    """外部客户群、消息收件箱、AI 回复审计"""

    db_path: Path = DB_PATH

    def __post_init__(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            # 多线程读写：WAL 降低 database is locked
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error as e:
                logger = __import__("logging").getLogger(__name__)
                logger.warning("SQLite WAL 启用失败: %s", e)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS external_groups (
                    roomid TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    owner_userid TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'INIT',
                    welcomed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_inbox (
                    msgid TEXT PRIMARY KEY,
                    roomid TEXT NOT NULL,
                    from_id TEXT NOT NULL DEFAULT '',
                    msgtype TEXT NOT NULL DEFAULT 'text',
                    content TEXT NOT NULL DEFAULT '',
                    processed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_message_inbox_roomid
                    ON message_inbox(roomid);

                CREATE TABLE IF NOT EXISTS ai_replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    roomid TEXT NOT NULL,
                    trigger_msgid TEXT NOT NULL DEFAULT '',
                    reply_text TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ai_replies_roomid
                    ON ai_replies(roomid);

                CREATE TABLE IF NOT EXISTS archive_cursor (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    seq INTEGER NOT NULL DEFAULT 0
                );

                INSERT OR IGNORE INTO archive_cursor (id, seq) VALUES (1, 0);

                CREATE TABLE IF NOT EXISTS kf_cursor (
                    open_kfid TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL DEFAULT '',
                    token TEXT NOT NULL DEFAULT '',
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS group_materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    roomid TEXT NOT NULL,
                    field_key TEXT NOT NULL,
                    field_value TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'form',
                    status TEXT NOT NULL DEFAULT 'missing',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(roomid, field_key)
                );

                CREATE INDEX IF NOT EXISTS idx_group_materials_roomid
                    ON group_materials(roomid);

                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    roomid TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL DEFAULT '',
                    final_answer TEXT NOT NULL DEFAULT '',
                    retrieval_score REAL NOT NULL DEFAULT 0,
                    answer_score REAL NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    action TEXT NOT NULL DEFAULT 'reply',
                    retries INTEGER NOT NULL DEFAULT 0,
                    trace_json TEXT NOT NULL DEFAULT '{}',
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_runs_roomid
                    ON agent_runs(roomid);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_created
                    ON agent_runs(created_at);

                CREATE TABLE IF NOT EXISTS customer_links (
                    wm_userid TEXT NOT NULL,
                    roomid TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY (wm_userid, roomid)
                );

                CREATE INDEX IF NOT EXISTS idx_customer_links_wm
                    ON customer_links(wm_userid);

                CREATE TABLE IF NOT EXISTS registration_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    roomid TEXT NOT NULL,
                    customer_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    last_error TEXT NOT NULL DEFAULT '',
                    package_dir TEXT NOT NULL DEFAULT '',
                    dry_run INTEGER NOT NULL DEFAULT 1,
                    allow_submit INTEGER NOT NULL DEFAULT 0,
                    screenshot_path TEXT NOT NULL DEFAULT '',
                    esubmit_screenshot_path TEXT NOT NULL DEFAULT '',
                    success_screenshot_path TEXT NOT NULL DEFAULT '',
                    review_status TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    company_name TEXT NOT NULL DEFAULT '',
                    result_messages TEXT NOT NULL DEFAULT '',
                    available_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL,
                    run_duration TEXT NOT NULL DEFAULT '',
                    s03a_duration TEXT NOT NULL DEFAULT '',
                    nnc1_duration TEXT NOT NULL DEFAULT '',
                    id_already_registered INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_registration_jobs_status
                    ON registration_jobs(status, available_at, id);
                CREATE INDEX IF NOT EXISTS idx_registration_jobs_roomid
                    ON registration_jobs(roomid);

                CREATE TABLE IF NOT EXISTS kf_send_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    open_kfid TEXT NOT NULL DEFAULT '',
                    external_userid TEXT NOT NULL DEFAULT '',
                    roomid TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kf_send_log_user_time
                    ON kf_send_log(open_kfid, external_userid, created_at);

                CREATE TABLE IF NOT EXISTS send_fail_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    roomid TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_send_fail_log_time
                    ON send_fail_log(created_at);

                CREATE TABLE IF NOT EXISTS email_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email_address TEXT NOT NULL UNIQUE,
                    imap_host TEXT NOT NULL,
                    imap_port INTEGER NOT NULL DEFAULT 993,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_email_accounts_address
                    ON email_accounts(email_address);

                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS intent_routes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    roomid TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT '',
                    text_hash TEXT NOT NULL DEFAULT '',
                    rule_intent TEXT NOT NULL DEFAULT '',
                    rule_mode TEXT NOT NULL DEFAULT '',
                    model_intent TEXT NOT NULL DEFAULT '',
                    model_mode TEXT NOT NULL DEFAULT '',
                    model_confidence REAL NOT NULL DEFAULT 0,
                    veto_applied TEXT NOT NULL DEFAULT '',
                    plan_steps_json TEXT NOT NULL DEFAULT '[]',
                    final_intent TEXT NOT NULL DEFAULT '',
                    final_mode TEXT NOT NULL DEFAULT '',
                    executed_ok INTEGER NOT NULL DEFAULT 1,
                    agent_mode TEXT NOT NULL DEFAULT 'normal',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_intent_routes_created
                    ON intent_routes(created_at);
                CREATE INDEX IF NOT EXISTS idx_intent_routes_roomid
                    ON intent_routes(roomid);
                """
            )
            self._migrate_columns(conn)
            self._migrate_kf_cursor(conn)
            self._migrate_registration_jobs(conn)
            self._migrate_intent_routes(conn)

            # 默认注册办事处地址 + 公司秘书法人团体信息
            _default_office_json = (
                '{"flat_floor":"ROOM 18 2/F","building":"Tuspark",'
                '"street":"118 Wai Yip Street","district":"Kwun Tong",'
                '"secretary_br_no":"78090873","secretary_license_no":"TC010510",'
                '"secretary_company_no":"0852-52667282"}'
            )
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key='icris_default_office'"
            ).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO system_settings(key, value, updated_at) VALUES (?, ?, ?)",
                    ("icris_default_office", _default_office_json, _utc_now()),
                )
            else:
                # 已有配置补齐秘书字段（不覆盖已有值）
                try:
                    import json as _json

                    cur = _json.loads(row[0] or "{}")
                    if not isinstance(cur, dict):
                        cur = {}
                    changed = False
                    for k, v in (
                        ("secretary_br_no", "78090873"),
                        ("secretary_license_no", "TC010510"),
                        ("secretary_company_no", "0852-52667282"),
                    ):
                        if not str(cur.get(k) or "").strip():
                            cur[k] = v
                            changed = True
                    if changed:
                        conn.execute(
                            "UPDATE system_settings SET value=?, updated_at=? WHERE key=?",
                            (
                                _json.dumps(cur, ensure_ascii=False),
                                _utc_now(),
                                "icris_default_office",
                            ),
                        )
                except Exception:
                    pass

    def _migrate_registration_jobs(self, conn: sqlite3.Connection) -> None:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(registration_jobs)")}
        if not cols:
            return
        if "screenshot_path" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN screenshot_path TEXT NOT NULL DEFAULT ''"
            )
        if "esubmit_screenshot_path" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN esubmit_screenshot_path TEXT NOT NULL DEFAULT ''"
            )
        if "success_screenshot_path" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN success_screenshot_path TEXT NOT NULL DEFAULT ''"
            )
        if "review_status" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN review_status TEXT NOT NULL DEFAULT ''"
            )
        if "payload_json" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN payload_json TEXT NOT NULL DEFAULT ''"
            )
        if "source" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN source TEXT NOT NULL DEFAULT ''"
            )
        if "company_name" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN company_name TEXT NOT NULL DEFAULT ''"
            )
        if "result_messages" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN result_messages TEXT NOT NULL DEFAULT ''"
            )
        if "activation_status" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN activation_status TEXT NOT NULL DEFAULT ''"
            )
        if "activation_checked_at" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN activation_checked_at TEXT NOT NULL DEFAULT ''"
            )
        if "activation_activated_at" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN activation_activated_at TEXT NOT NULL DEFAULT ''"
            )
        if "form_status" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN form_status TEXT NOT NULL DEFAULT ''"
            )
        if "form_filled_at" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN form_filled_at TEXT NOT NULL DEFAULT ''"
            )
        if "form_screenshot_path" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN form_screenshot_path TEXT NOT NULL DEFAULT ''"
            )
        if "run_duration" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN run_duration TEXT NOT NULL DEFAULT ''"
            )
        if "s03a_duration" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN s03a_duration TEXT NOT NULL DEFAULT ''"
            )
        if "nnc1_duration" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN nnc1_duration TEXT NOT NULL DEFAULT ''"
            )
        if "id_already_registered" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN id_already_registered "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "form_boost" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN form_boost "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "nnc1_loading_retries" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN nnc1_loading_retries "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "activation_url" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN activation_url "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "activation_username" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN activation_username "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "activation_url_saved_at" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN activation_url_saved_at "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "activation_pending_at" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN activation_pending_at "
                "TEXT NOT NULL DEFAULT ''"
            )
        if "activation_attempts" not in cols:
            conn.execute(
                "ALTER TABLE registration_jobs ADD COLUMN activation_attempts "
                "INTEGER NOT NULL DEFAULT 0"
            )

    def _migrate_intent_routes(self, conn: sqlite3.Connection) -> None:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(intent_routes)")}
        if cols:
            return
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intent_routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                roomid TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT '',
                text_hash TEXT NOT NULL DEFAULT '',
                rule_intent TEXT NOT NULL DEFAULT '',
                rule_mode TEXT NOT NULL DEFAULT '',
                model_intent TEXT NOT NULL DEFAULT '',
                model_mode TEXT NOT NULL DEFAULT '',
                model_confidence REAL NOT NULL DEFAULT 0,
                veto_applied TEXT NOT NULL DEFAULT '',
                plan_steps_json TEXT NOT NULL DEFAULT '[]',
                final_intent TEXT NOT NULL DEFAULT '',
                final_mode TEXT NOT NULL DEFAULT '',
                executed_ok INTEGER NOT NULL DEFAULT 1,
                agent_mode TEXT NOT NULL DEFAULT 'normal',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_intent_routes_created ON intent_routes(created_at)"
        )

    def _migrate_columns(self, conn: sqlite3.Connection) -> None:
        """增量添加 Phase 2/3 列"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(external_groups)")}
        migrations = {
            "form_token": "TEXT NOT NULL DEFAULT ''",
            "company_name": "TEXT NOT NULL DEFAULT ''",
            "package_dir": "TEXT NOT NULL DEFAULT ''",
            "open_kfid": "TEXT NOT NULL DEFAULT ''",
            "human_notified_at": "TEXT NOT NULL DEFAULT ''",
        }
        for col, typedef in migrations.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE external_groups ADD COLUMN {col} {typedef}")

        ai_cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_replies)")}
        for col, typedef in {
            "run_id": "TEXT NOT NULL DEFAULT ''",
            "confidence": "REAL NOT NULL DEFAULT 0",
        }.items():
            if col not in ai_cols:
                conn.execute(f"ALTER TABLE ai_replies ADD COLUMN {col} {typedef}")

        run_cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_runs)")}
        if run_cols and "duration_ms" not in run_cols:
            conn.execute(
                "ALTER TABLE agent_runs ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0"
            )

        # group_materials: 文件哈希去重（优化 7）
        gm_cols = {r[1] for r in conn.execute("PRAGMA table_info(group_materials)")}
        if gm_cols and "file_hash" not in gm_cols:
            conn.execute(
                "ALTER TABLE group_materials ADD COLUMN file_hash TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_group_materials_hash "
            "ON group_materials(roomid, file_hash)"
        )

    def _migrate_kf_cursor(self, conn: sqlite3.Connection) -> None:
        """将 kf_cursor 从单例 id=1 迁移为 open_kfid 主键"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kf_cursor)")}
        if not cols:
            return
        if "open_kfid" in cols:
            return

        legacy_cursor, legacy_token = "", ""
        if "id" in cols:
            row = conn.execute(
                "SELECT cursor, token FROM kf_cursor WHERE id = 1",
            ).fetchone()
            if row:
                legacy_cursor = str(row["cursor"] or "")
                legacy_token = str(row["token"] or "")
            conn.execute("DROP TABLE kf_cursor")
            conn.execute(
                """
                CREATE TABLE kf_cursor (
                    open_kfid TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL DEFAULT '',
                    token TEXT NOT NULL DEFAULT '',
                    updated_at TEXT
                )
                """
            )
            if legacy_cursor or legacy_token:
                conn.execute(
                    """
                    INSERT INTO kf_cursor (open_kfid, cursor, token, updated_at)
                    VALUES ('__legacy__', ?, ?, ?)
                    """,
                    (legacy_cursor, legacy_token, _utc_now()),
                )

    def upsert_group(
        self,
        roomid: str,
        *,
        name: str = "",
        owner_userid: str = "",
        status: str | None = None,
        welcomed_at: str | None = None,
        form_token: str | None = None,
        company_name: str | None = None,
        package_dir: str | None = None,
        open_kfid: str | None = None,
        human_notified_at: str | None = None,
    ) -> None:
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT roomid, status FROM external_groups WHERE roomid = ?",
                (roomid,),
            ).fetchone()
            if row:
                updates: list[str] = ["updated_at = ?"]
                params: list[Any] = [now]
                if name:
                    updates.append("name = ?")
                    params.append(name)
                if owner_userid:
                    updates.append("owner_userid = ?")
                    params.append(owner_userid)
                if status is not None:
                    updates.append("status = ?")
                    params.append(status)
                if welcomed_at is not None:
                    updates.append("welcomed_at = ?")
                    params.append(welcomed_at)
                if form_token is not None:
                    updates.append("form_token = ?")
                    params.append(form_token)
                if company_name is not None:
                    updates.append("company_name = ?")
                    params.append(company_name)
                if package_dir is not None:
                    updates.append("package_dir = ?")
                    params.append(package_dir)
                if open_kfid is not None:
                    updates.append("open_kfid = ?")
                    params.append(open_kfid)
                if human_notified_at is not None:
                    updates.append("human_notified_at = ?")
                    params.append(human_notified_at)
                params.append(roomid)
                conn.execute(
                    f"UPDATE external_groups SET {', '.join(updates)} WHERE roomid = ?",
                    params,
                )
            else:
                conn.execute(
                    """
                    INSERT INTO external_groups
                        (roomid, name, owner_userid, status, welcomed_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        roomid,
                        name,
                        owner_userid,
                        status or "INIT",
                        welcomed_at,
                        now,
                        now,
                    ),
                )

    def get_group(self, roomid: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM external_groups WHERE roomid = ?",
                (roomid,),
            ).fetchone()
            return dict(row) if row else None

    def list_groups(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM external_groups ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def set_group_status(self, roomid: str, status: str) -> None:
        self.upsert_group(roomid, status=status)

    def mark_human_notified(self, roomid: str, *, at: str | None = None) -> None:
        """转人工提示已发送（持久化，防重启重复刷屏）。"""
        self.upsert_group(
            roomid,
            human_notified_at=at or _utc_now(),
        )

    def clear_human_notified(self, roomid: str) -> None:
        self.upsert_group(roomid, human_notified_at="")

    def insert_message_if_new(
        self,
        msgid: str,
        roomid: str,
        from_id: str,
        msgtype: str,
        content: str,
    ) -> bool:
        """插入消息，返回 True 表示新消息（未处理过）"""
        now = _utc_now()
        with self._conn() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO message_inbox
                        (msgid, roomid, from_id, msgtype, content, processed, created_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?)
                    """,
                    (msgid, roomid, from_id, msgtype, content, now),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_message_processed(self, msgid: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE message_inbox SET processed = 1 WHERE msgid = ?",
                (msgid,),
            )

    def list_unprocessed_messages(
        self,
        *,
        older_than_seconds: int = 120,
        limit: int = 20,
        msgtype: str = "text",
    ) -> list[dict[str, Any]]:
        """超时未处理的入站消息（供崩溃恢复重投）。"""
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=max(30, int(older_than_seconds or 120)))
        ).isoformat()
        limit = max(1, min(int(limit or 20), 100))
        with self._conn() as conn:
            if msgtype:
                rows = conn.execute(
                    """
                    SELECT * FROM message_inbox
                    WHERE processed = 0
                      AND created_at < ?
                      AND msgtype = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (cutoff, msgtype, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM message_inbox
                    WHERE processed = 0 AND created_at < ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (cutoff, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def count_unprocessed_messages(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM message_inbox WHERE processed = 0"
            ).fetchone()
        return int(row["n"]) if row else 0

    def record_kf_send(
        self,
        *,
        open_kfid: str,
        external_userid: str,
        roomid: str = "",
    ) -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO kf_send_log (open_kfid, external_userid, roomid, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (open_kfid or "", external_userid or "", roomid or "", now),
            )

    def count_kf_sends_48h(
        self,
        *,
        open_kfid: str,
        external_userid: str,
        hours: float = 48.0,
    ) -> int:
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max(1.0, float(hours)))
        ).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM kf_send_log
                WHERE open_kfid = ? AND external_userid = ? AND created_at >= ?
                """,
                (open_kfid or "", external_userid or "", cutoff),
            ).fetchone()
        return int(row["n"]) if row else 0

    def record_send_failure(self, roomid: str, reason: str = "") -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO send_fail_log (roomid, reason, created_at)
                VALUES (?, ?, ?)
                """,
                (roomid or "", (reason or "")[:200], now),
            )

    def conversation_quality_stats(self, *, hours: float = 24.0) -> dict[str, Any]:
        """近 N 小时 QA action 分布 + inbox 积压 + 发送失败 + 质量分。"""
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max(1.0, float(hours)))
        ).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT action, COUNT(*) AS n FROM agent_runs
                WHERE created_at >= ?
                GROUP BY action
                """,
                (cutoff,),
            ).fetchall()
            backlog = conn.execute(
                "SELECT COUNT(*) AS n FROM message_inbox WHERE processed = 0"
            ).fetchone()
            fail_row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM send_fail_log
                WHERE created_at >= ?
                """,
                (cutoff,),
            ).fetchone()
            send_row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM kf_send_log
                WHERE created_at >= ?
                """,
                (cutoff,),
            ).fetchone()
            dur_rows = conn.execute(
                """
                SELECT duration_ms FROM agent_runs
                WHERE created_at >= ? AND duration_ms > 0
                ORDER BY duration_ms ASC
                """,
                (cutoff,),
            ).fetchall()
            score_row = conn.execute(
                """
                SELECT
                    AVG(confidence) AS avg_confidence,
                    AVG(answer_score) AS avg_answer_score,
                    AVG(retrieval_score) AS avg_retrieval_score,
                    SUM(CASE WHEN confidence < 0.5 OR action != 'reply' THEN 1 ELSE 0 END)
                        AS low_confidence_count
                FROM agent_runs
                WHERE created_at >= ?
                """,
                (cutoff,),
            ).fetchone()
        actions = {str(r["action"]): int(r["n"]) for r in rows}
        total = sum(actions.values()) or 0
        silent_n = int(actions.get("silent", 0))
        abstain_n = int(actions.get("abstain", 0))
        reply_n = int(actions.get("reply", 0))
        durations = [int(r["duration_ms"]) for r in dur_rows if r["duration_ms"]]
        latency = _percentile_stats(durations)
        human_n = int(actions.get("human", 0))
        route_stats = self.intent_route_stats(hours=hours)

        def _avg(val: Any) -> float:
            if val is None:
                return 0.0
            try:
                return round(float(val), 4)
            except (TypeError, ValueError):
                return 0.0

        return {
            "hours": hours,
            "agent_runs_total": total,
            "actions": actions,
            "reply_rate": round(reply_n / total, 4) if total else 0.0,
            "silent_rate": round(silent_n / total, 4) if total else 0.0,
            "abstain_rate": round(abstain_n / total, 4) if total else 0.0,
            "human_transfer_rate": round(human_n / total, 4) if total else 0.0,
            "avg_confidence": _avg(score_row["avg_confidence"] if score_row else None),
            "avg_answer_score": _avg(score_row["avg_answer_score"] if score_row else None),
            "avg_retrieval_score": _avg(
                score_row["avg_retrieval_score"] if score_row else None
            ),
            "low_confidence_count": int(
                (score_row["low_confidence_count"] if score_row else 0) or 0
            ),
            "inbox_unprocessed": int(backlog["n"]) if backlog else 0,
            "send_failures": int(fail_row["n"]) if fail_row else 0,
            "kf_sends": int(send_row["n"]) if send_row else 0,
            "qa_latency_ms": latency,
            "intent_routes": route_stats,
        }

    def insert_intent_route(
        self,
        *,
        roomid: str,
        status: str = "",
        channel: str = "",
        text_hash: str = "",
        rule_intent: str = "",
        rule_mode: str = "",
        model_intent: str = "",
        model_mode: str = "",
        model_confidence: float = 0.0,
        veto_applied: str = "",
        plan_steps_json: str = "[]",
        final_intent: str = "",
        final_mode: str = "",
        executed_ok: bool = True,
        agent_mode: str = "normal",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO intent_routes (
                    roomid, status, channel, text_hash,
                    rule_intent, rule_mode, model_intent, model_mode, model_confidence,
                    veto_applied, plan_steps_json, final_intent, final_mode,
                    executed_ok, agent_mode, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    roomid or "",
                    status or "",
                    channel or "",
                    text_hash or "",
                    rule_intent or "",
                    rule_mode or "",
                    model_intent or "",
                    model_mode or "",
                    float(model_confidence or 0),
                    veto_applied or "",
                    plan_steps_json or "[]",
                    final_intent or "",
                    final_mode or "",
                    1 if executed_ok else 0,
                    agent_mode or "normal",
                    now,
                ),
            )

    def intent_route_stats(self, *, hours: float = 24.0) -> dict[str, Any]:
        from datetime import timedelta

        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max(1.0, float(hours)))
        ).isoformat()
        try:
            with self._conn() as conn:
                total_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM intent_routes WHERE created_at >= ?",
                    (cutoff,),
                ).fetchone()
                model_row = conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM intent_routes
                    WHERE created_at >= ? AND model_intent != ''
                    """,
                    (cutoff,),
                ).fetchone()
                veto_row = conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM intent_routes
                    WHERE created_at >= ? AND veto_applied != ''
                    """,
                    (cutoff,),
                ).fetchone()
            total = int(total_row["n"]) if total_row else 0
            model_n = int(model_row["n"]) if model_row else 0
            veto_n = int(veto_row["n"]) if veto_row else 0
            return {
                "intent_routes_total": total,
                "model_invoke_rate": round(model_n / total, 4) if total else 0.0,
                "veto_rate": round(veto_n / total, 4) if total else 0.0,
            }
        except Exception:
            return {
                "intent_routes_total": 0,
                "model_invoke_rate": 0.0,
                "veto_rate": 0.0,
            }

    def get_recent_messages(self, roomid: str, *, limit: int = 10) -> list[str]:
        """按时间交错合并客户消息与助手回复，供 QA 上下文。"""
        fetch_n = max(limit * 2, 20)
        with self._conn() as conn:
            inbox_rows = conn.execute(
                """
                SELECT msgtype, content, created_at FROM message_inbox
                WHERE roomid = ? AND content != ''
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (roomid, fetch_n),
            ).fetchall()
            reply_rows = conn.execute(
                """
                SELECT reply_text, created_at FROM ai_replies
                WHERE roomid = ? AND reply_text != ''
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (roomid, fetch_n),
            ).fetchall()

        events: list[tuple[str, str]] = []
        for row in inbox_rows:
            msgtype = str(row["msgtype"] or "text")
            content = str(row["content"] or "").strip()
            if not content:
                continue
            if msgtype in ("image", "file"):
                line = f"客户: [上传文件 {content}]"
            else:
                line = f"客户: {content}"
            events.append((str(row["created_at"] or ""), line))
        for row in reply_rows:
            text = str(row["reply_text"] or "").strip()
            if not text:
                continue
            # 截断过长回复，避免撑爆 prompt（保留更长以便指代/上下文）
            if len(text) > 500:
                text = text[:500] + "…"
            events.append((str(row["created_at"] or ""), f"助手: {text}"))

        events.sort(key=lambda x: x[0])
        return [line for _, line in events[-limit:]]

    def insert_ai_reply(
        self,
        roomid: str,
        trigger_msgid: str,
        reply_text: str,
        model: str = "",
        *,
        run_id: str = "",
        confidence: float = 0.0,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO ai_replies
                    (roomid, trigger_msgid, reply_text, model, run_id, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (roomid, trigger_msgid, reply_text, model, run_id, confidence, _utc_now()),
            )

    def insert_agent_run(
        self,
        run_id: str,
        roomid: str,
        question: str,
        final_answer: str,
        retrieval_score: float,
        answer_score: float,
        confidence: float,
        action: str,
        retries: int,
        trace_json: str,
        duration_ms: int = 0,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs (
                    id, roomid, question, final_answer,
                    retrieval_score, answer_score, confidence,
                    action, retries, trace_json, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, roomid, question, final_answer,
                    retrieval_score, answer_score, confidence,
                    action, retries, trace_json, int(duration_ms or 0), _utc_now(),
                ),
            )

    def list_low_confidence_runs(self, *, limit: int = 20, threshold: float = 0.5) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_runs
                WHERE confidence < ? OR action != 'reply'
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (threshold, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_agent_runs(
        self, roomid: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """查询某群最近 N 条已回复的 agent_runs 记录（优化 4 一致性检查）。"""
        if not roomid:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT question, final_answer, confidence, action, created_at
                FROM agent_runs
                WHERE roomid = ? AND action = 'reply'
                  AND final_answer != ''
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (roomid, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_archive_seq(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT seq FROM archive_cursor WHERE id = 1").fetchone()
            return int(row["seq"]) if row else 0

    def set_archive_seq(self, seq: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE archive_cursor SET seq = ? WHERE id = 1", (seq,))

    def get_kf_cursor(self, open_kfid: str) -> tuple[str, str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cursor, token FROM kf_cursor WHERE open_kfid = ?",
                (open_kfid,),
            ).fetchone()
            if not row:
                return "", ""
            return str(row["cursor"] or ""), str(row["token"] or "")

    def set_kf_cursor(
        self, open_kfid: str, cursor: str, token: str = "",
    ) -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO kf_cursor (open_kfid, cursor, token, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(open_kfid) DO UPDATE SET
                    cursor = excluded.cursor,
                    token = excluded.token,
                    updated_at = excluded.updated_at
                """,
                (open_kfid, cursor, token, now),
            )

    def ensure_kf_cursors(self, open_kfid_list: list[str]) -> None:
        """初始化各客服账号游标；将 __legacy__ 迁移到首个账号"""
        if not open_kfid_list:
            return
        with self._conn() as conn:
            legacy = conn.execute(
                "SELECT cursor, token FROM kf_cursor WHERE open_kfid = '__legacy__'",
            ).fetchone()
            for kfid in open_kfid_list:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO kf_cursor
                        (open_kfid, cursor, token, updated_at)
                    VALUES (?, '', '', ?)
                    """,
                    (kfid, _utc_now()),
                )
            if legacy:
                first = open_kfid_list[0]
                cur, tok = str(legacy["cursor"] or ""), str(legacy["token"] or "")
                existing_cur, _ = self.get_kf_cursor(first)
                if not existing_cur and (cur or tok):
                    self.set_kf_cursor(first, cur, tok)
                conn.execute("DELETE FROM kf_cursor WHERE open_kfid = '__legacy__'")

    def ensure_form_token(self, roomid: str) -> str:
        import secrets

        group = self.get_group(roomid)
        if group and group.get("form_token"):
            return str(group["form_token"])
        token = secrets.token_urlsafe(24)
        self.upsert_group(roomid, form_token=token)
        return token

    def get_group_by_token(self, token: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM external_groups WHERE form_token = ?",
                (token,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_material(
        self,
        roomid: str,
        field_key: str,
        *,
        field_value: str = "",
        file_path: str = "",
        source: str = "form",
        status: str = "received",
        file_hash: str = "",
    ) -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO group_materials
                    (roomid, field_key, field_value, file_path, source, status, file_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(roomid, field_key) DO UPDATE SET
                    field_value = excluded.field_value,
                    file_path = CASE WHEN excluded.file_path != '' THEN excluded.file_path ELSE file_path END,
                    source = excluded.source,
                    status = excluded.status,
                    file_hash = CASE WHEN excluded.file_hash != '' THEN excluded.file_hash ELSE file_hash END,
                    updated_at = excluded.updated_at
                """,
                (roomid, field_key, field_value, file_path, source, status, file_hash, now, now),
            )

    def find_material_by_hash(
        self, roomid: str, file_hash: str
    ) -> dict[str, Any] | None:
        """按文件 SHA-256 哈希查重（优化 7）。命中返回材料行，否则 None。"""
        if not file_hash:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM group_materials WHERE roomid = ? AND file_hash = ? "
                "AND file_hash != '' LIMIT 1",
                (roomid, file_hash),
            ).fetchone()
        return dict(row) if row else None

    def get_materials(self, roomid: str) -> dict[str, dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM group_materials WHERE roomid = ?",
                (roomid,),
            ).fetchall()
        return {str(r["field_key"]): dict(r) for r in rows}

    def rewire_material_file_paths(
        self,
        roomid: str,
        old_dirname: str,
        new_dirname: str,
    ) -> int:
        """材料目录重命名后，更新 file_path 中的目录名片段"""
        if not old_dirname or not new_dirname or old_dirname == new_dirname:
            return 0
        updated = 0
        materials = self.get_materials(roomid)
        now = _utc_now()
        with self._conn() as conn:
            for key, row in materials.items():
                path = str(row.get("file_path") or "")
                if not path or old_dirname not in path:
                    continue
                new_path = path.replace(old_dirname, new_dirname, 1)
                if new_path == path:
                    continue
                conn.execute(
                    """
                    UPDATE group_materials
                    SET file_path = ?, updated_at = ?
                    WHERE roomid = ? AND field_key = ?
                    """,
                    (new_path, now, roomid, key),
                )
                updated += 1
        return updated

    def list_all_materials_summary(
        self, *, channel: str = "all",
    ) -> list[dict[str, Any]]:
        channel = (channel or "all").strip().lower()
        sql = """
                SELECT g.roomid, g.name, g.status, g.company_name, g.open_kfid,
                       COUNT(m.id) AS material_count
                FROM external_groups g
                LEFT JOIN group_materials m ON g.roomid = m.roomid
                """
        params: tuple = ()
        if channel == "group":
            sql += " WHERE g.roomid LIKE 'wr%' "
        elif channel == "kf":
            sql += " WHERE g.roomid LIKE 'kf:%' "
        sql += """
                GROUP BY g.roomid
                ORDER BY g.updated_at DESC
                """
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for r in rows:
            row = dict(r)
            rid = str(row.get("roomid") or "")
            if rid.startswith("kf:"):
                row["channel"] = "kf"
                if not row.get("open_kfid"):
                    from src.wework.kf_session import parse_kf_roomid

                    parsed = parse_kf_roomid(rid)
                    if parsed:
                        row["open_kfid"] = parsed[0]
            elif rid.startswith("wr"):
                row["channel"] = "group"
            else:
                row["channel"] = "other"
            result.append(row)
        return result

    def link_customer(self, wm_userid: str, roomid: str) -> None:
        if not wm_userid.startswith("wm") or not roomid.startswith("wr"):
            return
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO customer_links (wm_userid, roomid, linked_at)
                VALUES (?, ?, ?)
                """,
                (wm_userid, roomid, now),
            )

    def get_linked_groups(self, wm_userid: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT cl.roomid, cl.linked_at, g.name, g.status
                FROM customer_links cl
                LEFT JOIN external_groups g ON g.roomid = cl.roomid
                WHERE cl.wm_userid = ?
                ORDER BY cl.linked_at DESC
                """,
                (wm_userid,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- registration_jobs (L2 ICRIS 队列) ----

    def get_active_registration_job(self, roomid: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM registration_jobs
                WHERE roomid = ? AND status IN ('pending', 'running')
                ORDER BY id DESC
                LIMIT 1
                """,
                (roomid,),
            ).fetchone()
        return dict(row) if row else None

    def get_registration_job(self, job_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def enqueue_registration_job(
        self,
        roomid: str,
        *,
        customer_id: str = "",
        dry_run: bool = True,
        allow_submit: bool = False,
        max_attempts: int | None = None,
        payload: dict[str, Any] | None = None,
        source: str = "",
        company_name: str = "",
        package_dir: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """幂等入队。返回 (job, created)。同 roomid 已有 pending/running 则返回已有任务。"""
        import json

        from config.settings import settings

        existing = self.get_active_registration_job(roomid)
        if existing:
            return existing, False

        now = _utc_now()
        max_att = int(
            max_attempts
            if max_attempts is not None
            else getattr(settings, "icris_job_max_attempts", 3) or 3
        )
        payload_json = ""
        if payload is not None:
            try:
                payload_json = json.dumps(payload, ensure_ascii=False)
            except (TypeError, ValueError):
                payload_json = json.dumps({"_error": "payload_not_serializable"})
        company = (company_name or "").strip()
        if not company and isinstance(payload, dict):
            company = str(
                payload.get("company_name_en") or payload.get("company_name_cn") or ""
            ).strip()
        src = (source or "").strip().lower()
        pkg = (package_dir or "").strip()

        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM registration_jobs
                WHERE roomid = ? AND status IN ('pending', 'running')
                ORDER BY id DESC LIMIT 1
                """,
                (roomid,),
            ).fetchone()
            if row:
                return dict(row), False
            cur = conn.execute(
                """
                INSERT INTO registration_jobs (
                    roomid, customer_id, status, attempts, max_attempts,
                    last_error, package_dir, dry_run, allow_submit,
                    payload_json, source, company_name, result_messages,
                    available_at, created_at, started_at, finished_at, updated_at
                ) VALUES (?, ?, 'pending', 0, ?, '', ?, ?, ?, ?, ?, ?, '', ?, ?, NULL, NULL, ?)
                """,
                (
                    roomid,
                    customer_id or "",
                    max_att,
                    pkg,
                    1 if dry_run else 0,
                    1 if allow_submit else 0,
                    payload_json,
                    src,
                    company,
                    now,
                    now,
                    now,
                ),
            )
            job_id = int(cur.lastrowid)
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return dict(row), True

    def claim_next_job(self) -> dict[str, Any] | None:
        """认领最早可执行的 pending 任务（先登记的先跑）。已拒绝单不认领。"""
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM registration_jobs
                WHERE status = 'pending'
                  AND IFNULL(review_status, '') != 'rejected'
                  AND (available_at = '' OR available_at <= ?)
                ORDER BY id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                return None
            job_id = int(row["id"])
            cur = conn.execute(
                """
                UPDATE registration_jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    started_at = ?,
                    run_duration = '',
                    s03a_duration = '',
                    nnc1_duration = '',
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                  AND IFNULL(review_status, '') != 'rejected'
                """,
                (now, now, job_id),
            )
            if cur.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM registration_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return dict(claimed) if claimed else None

    def peek_claimable_registration(self) -> dict[str, Any] | None:
        """只读：是否有已到期、可 claim 的 pending 注册（不含 running）。"""
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM registration_jobs
                WHERE status = 'pending'
                  AND IFNULL(review_status, '') != 'rejected'
                  AND (available_at = '' OR available_at <= ?)
                ORDER BY id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
        return dict(row) if row else None

    def has_active_registration_queue(self) -> bool:
        """CDP 被注册占用或即将占用：running / awaiting_review / 已到期 pending。"""
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id FROM registration_jobs
                WHERE (
                        status IN ('running', 'awaiting_review')
                        AND IFNULL(review_status, '') != 'rejected'
                    )
                   OR (
                        status = 'pending'
                        AND IFNULL(review_status, '') != 'rejected'
                        AND (available_at = '' OR available_at <= ?)
                   )
                LIMIT 1
                """,
                (now,),
            ).fetchone()
        return row is not None

    def fail_orphan_awaiting_review(self) -> int:
        """进程重启后审核页已丢失：awaiting_review 标失败，避免永远挡住 NNC1。"""
        now = _utc_now()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, started_at, run_duration FROM registration_jobs
                WHERE status = 'awaiting_review'
                  AND IFNULL(review_status, '') != 'rejected'
                """
            ).fetchall()
            n = 0
            for row in rows:
                dur = str(row["run_duration"] or "").strip() or duration_since_started(
                    str(row["started_at"] or ""), now
                )
                cur = conn.execute(
                    """
                    UPDATE registration_jobs
                    SET status = 'failed',
                        last_error = CASE
                            WHEN last_error = '' THEN '进程重启，审核页已丢失'
                            ELSE last_error
                        END,
                        finished_at = CASE WHEN finished_at IS NULL OR finished_at = ''
                                           THEN ? ELSE finished_at END,
                        run_duration = CASE WHEN IFNULL(run_duration, '') = ''
                                            THEN ? ELSE run_duration END,
                        updated_at = ?
                    WHERE id = ? AND status = 'awaiting_review'
                      AND IFNULL(review_status, '') != 'rejected'
                    """,
                    (now, dur, now, int(row["id"])),
                )
                n += int(cur.rowcount or 0)
            return n

    def mark_job_succeeded(
        self,
        job_id: int,
        *,
        package_dir: str = "",
        result_messages: list[Any] | None = None,
        esubmit_screenshot_path: str = "",
        success_screenshot_path: str = "",
        run_duration: str = "",
    ) -> None:
        import json

        now = _utc_now()
        msgs = ""
        if result_messages is not None:
            try:
                msgs = json.dumps(list(result_messages), ensure_ascii=False)
            except (TypeError, ValueError):
                msgs = "[]"
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE registration_jobs
                SET status = 'succeeded',
                    package_dir = CASE WHEN ? != '' THEN ? ELSE package_dir END,
                    result_messages = CASE WHEN ? != '' THEN ? ELSE result_messages END,
                    esubmit_screenshot_path = CASE WHEN ? != '' THEN ? ELSE esubmit_screenshot_path END,
                    success_screenshot_path = CASE WHEN ? != '' THEN ? ELSE success_screenshot_path END,
                    finished_at = ?,
                    run_duration = CASE WHEN ? != '' THEN ? ELSE run_duration END,
                    updated_at = ?,
                    last_error = ''
                WHERE id = ? AND status IN ('running', 'awaiting_review')
                """,
                (
                    package_dir, package_dir, msgs, msgs,
                    esubmit_screenshot_path, esubmit_screenshot_path,
                    success_screenshot_path, success_screenshot_path,
                    now, run_duration, run_duration, now, job_id
                ),
            )

    def mark_job_awaiting_review(self, job_id: int) -> None:
        """标记 job 进入待审核状态（bot 在 s03a 等待人工确认）。

        只从 running 进入，避免覆盖管理员已取消的任务。
        """
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """UPDATE registration_jobs
                   SET status='awaiting_review', review_status='awaiting_review',
                       updated_at=?
                   WHERE id=? AND status='running'""",
                (now, job_id),
            )
        self.set_job_s03a_duration(job_id)

    def set_job_s03a_duration(self, job_id: int) -> None:
        """第一次进入 s03a 时写入到 s03a 耗时（不含审批等待）。已有值不覆盖。"""
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT started_at, s03a_duration FROM registration_jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
            if not row:
                return
            if str(row["s03a_duration"] or "").strip():
                return
            dur = duration_since_started(str(row["started_at"] or ""), now)
            if not dur:
                return
            conn.execute(
                """
                UPDATE registration_jobs
                SET s03a_duration = ?, updated_at = ?
                WHERE id = ? AND IFNULL(s03a_duration, '') = ''
                """,
                (dur, now, job_id),
            )

    def update_job_esubmit_screenshot(self, job_id: int, path: str) -> None:
        """s03a 截图后立即写入 DB，供后台在审核期间展示核对图。"""
        if not path:
            return
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """UPDATE registration_jobs
                   SET esubmit_screenshot_path=?, updated_at=?
                   WHERE id=?""",
                (path, now, job_id),
            )

    def update_job_payload_account(
        self, job_id: int, username: str, password: str
    ) -> None:
        """重跑时更新 payload_json 里的 icris_account，供前端展示新用户名。"""
        import json as _json

        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM registration_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row:
                return
            raw = str(row["payload_json"] or "")
            try:
                data = _json.loads(raw) if raw else {}
            except (TypeError, ValueError, _json.JSONDecodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data.setdefault("icris_account", {})
            if not isinstance(data["icris_account"], dict):
                data["icris_account"] = {}
            data["icris_account"]["username"] = username
            data["icris_account"]["password"] = password
            conn.execute(
                """UPDATE registration_jobs
                   SET payload_json=?, updated_at=?
                   WHERE id=?""",
                (_json.dumps(data, ensure_ascii=False), now, job_id),
            )

    def approve_job_submit(self, job_id: int) -> dict[str, Any] | None:
        """审核通过：设 review_status=approved（bot 轮询到后点继续）。"""
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """UPDATE registration_jobs
                   SET review_status='approved', updated_at=?
                   WHERE id=? AND status='awaiting_review'""",
                (now, job_id),
            )
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def reject_job_submit(self, job_id: int) -> dict[str, Any] | None:
        """审核拒绝：设 review_status=rejected, status=failed。"""
        now = _utc_now()
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT started_at, run_duration FROM registration_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not cur:
                return None
            dur = str(cur["run_duration"] or "").strip() or duration_since_started(
                str(cur["started_at"] or ""), now
            )
            conn.execute(
                """UPDATE registration_jobs
                   SET review_status='rejected', status='failed',
                       finished_at=?,
                       run_duration = CASE WHEN IFNULL(run_duration, '') = ''
                                           THEN ? ELSE run_duration END,
                       updated_at=?
                   WHERE id=? AND status='awaiting_review'""",
                (now, dur, now, job_id),
            )
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_job_review_status(self, job_id: int) -> str:
        """获取当前 review_status（awaiting_review / approved / rejected / 空）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT review_status FROM registration_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return str(row["review_status"] if row else "")

    def get_job_status(self, job_id: int) -> str:
        """获取任务当前 status（用于 bot 轮询是否被取消）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM registration_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return str(row["status"] if row else "")

    def mark_job_id_already_registered(self, job_id: int) -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE registration_jobs
                SET id_already_registered = 1, updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )

    def find_job_id_already_registered(self, id_number: str) -> dict[str, Any] | None:
        """历史任务曾标红该证件号则返回该行（用于快速注册拦截）。"""
        from src.browser.icris_errors import (
            id_numbers_from_job_payload,
            normalize_id_number_key,
        )
        import json

        key = normalize_id_number_key(id_number)
        if not key:
            return None
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM registration_jobs
                WHERE id_already_registered = 1
                ORDER BY id DESC
                """
            ).fetchall()
        for row in rows:
            item = dict(row)
            raw = str(item.get("payload_json") or "")
            try:
                payload = json.loads(raw) if raw else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            for num in id_numbers_from_job_payload(payload):
                if normalize_id_number_key(num) == key:
                    return item
        return None

    # ---- 邮箱账号配置 ----

    def list_email_accounts(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM email_accounts ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_email_account_by_address(self, email: str) -> dict[str, Any] | None:
        addr = (email or "").strip().lower()
        if not addr:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM email_accounts"
                " WHERE lower(email_address)=? AND enabled=1",
                (addr,),
            ).fetchone()
        return dict(row) if row else None

    def find_icris_password_by_username(self, username: str) -> str:
        """只读：最近一条 payload 中 icris_account.username 匹配的密码。不改任务。"""
        import json

        user = (username or "").strip()
        if not user:
            return ""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT payload_json FROM registration_jobs
                   WHERE payload_json LIKE ?
                   ORDER BY id DESC LIMIT 50""",
                (f"%{user}%",),
            ).fetchall()
        for row in rows:
            try:
                data = json.loads(str(row["payload_json"] or "") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            acct = data.get("icris_account") or {}
            if not isinstance(acct, dict):
                continue
            if str(acct.get("username") or "").strip() != user:
                continue
            return str(acct.get("password") or "").strip()
        return ""

    def upsert_email_account(
        self,
        email: str,
        imap_host: str,
        imap_port: int,
        username: str,
        password: str,
        label: str = "",
        *,
        account_id: int | None = None,
        enabled: int | None = None,
    ) -> dict[str, Any]:
        email = (email or "").strip().lower()
        now = _utc_now()
        with self._conn() as conn:
            target = None
            if account_id:
                target = conn.execute(
                    "SELECT * FROM email_accounts WHERE id=?",
                    (account_id,),
                ).fetchone()
                if not target:
                    raise ValueError("account not found")
            else:
                target = conn.execute(
                    "SELECT * FROM email_accounts WHERE lower(email_address)=?",
                    (email,),
                ).fetchone()

            if target:
                other = conn.execute(
                    "SELECT id FROM email_accounts"
                    " WHERE lower(email_address)=? AND id!=?",
                    (email, target["id"]),
                ).fetchone()
                if other:
                    raise ValueError("邮箱地址已存在")
                pwd = password if password else str(target["password"] or "")
                enabled_val = (
                    int(target["enabled"] or 1)
                    if enabled is None
                    else (1 if enabled else 0)
                )
                conn.execute(
                    """UPDATE email_accounts
                       SET email_address=?, imap_host=?, imap_port=?, username=?,
                           password=?, label=?, enabled=?, updated_at=?
                       WHERE id=?""",
                    (
                        email,
                        imap_host,
                        imap_port,
                        username,
                        pwd,
                        label,
                        enabled_val,
                        now,
                        target["id"],
                    ),
                )
                aid = target["id"]
            else:
                enabled_val = 1 if enabled is None else (1 if enabled else 0)
                try:
                    cur = conn.execute(
                        """INSERT INTO email_accounts
                           (email_address, imap_host, imap_port, username, password,
                            label, enabled, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (
                            email,
                            imap_host,
                            imap_port,
                            username,
                            password,
                            label,
                            enabled_val,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError("邮箱地址已存在") from exc
                aid = cur.lastrowid
            row = conn.execute(
                "SELECT * FROM email_accounts WHERE id=?", (aid,)
            ).fetchone()
        return dict(row)

    def delete_email_account(self, account_id: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM email_accounts WHERE id=?", (account_id,)
            )
            return cur.rowcount > 0

    def get_system_setting(self, key: str) -> str:
        """查单个系统配置值。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else ""

    def set_system_setting(self, key: str, value: str) -> None:
        """写入/更新系统配置。"""
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO system_settings(key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, now),
            )

    # ---- 账号激活状态 ----

    def mark_job_activation_pending(self, job_id: int) -> None:
        """注册成功后标记待激活。已 activated 的单不打回。"""
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """UPDATE registration_jobs
                   SET activation_status='pending', activation_pending_at=?,
                       activation_checked_at='', activation_activated_at='',
                       activation_url='', activation_username='',
                       activation_url_saved_at='', activation_attempts=0,
                       updated_at=?
                   WHERE id=?
                     AND IFNULL(activation_status, '') NOT IN ('activated', 'failed')""",
                (now, now, job_id),
            )

    def get_jobs_pending_activation(self) -> list[dict[str, Any]]:
        """待扫信：注册已成功且仍 pending（不含已激活/失败/取消）。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM registration_jobs
                   WHERE activation_status='pending'
                     AND status = 'succeeded'
                   ORDER BY id"""
            ).fetchall()
        return [dict(r) for r in rows]

    def get_jobs_ready_to_activate(self) -> list[dict[str, Any]]:
        """已存匹配链接、尚未激活：先存的先激活。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM registration_jobs
                   WHERE activation_status='pending'
                     AND status = 'succeeded'
                     AND IFNULL(activation_url, '') != ''
                   ORDER BY
                     CASE WHEN activation_url_saved_at = '' THEN '9999'
                          ELSE activation_url_saved_at END,
                     id"""
            ).fetchall()
        return [dict(r) for r in rows]

    def save_job_activation_url(
        self, job_id: int, url: str, username: str
    ) -> bool:
        """把匹配该单用户名的 s06 写到该行。已有链接不覆盖。"""
        now = _utc_now()
        link = (url or "").strip()
        user = (username or "").strip()
        if not link or not user:
            return False
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE registration_jobs
                   SET activation_url=?, activation_username=?,
                       activation_url_saved_at=?, updated_at=?
                   WHERE id=?
                     AND status='succeeded'
                     AND activation_status='pending'
                     AND IFNULL(activation_url, '') = ''""",
                (link, user, now, now, job_id),
            )
            return int(cur.rowcount or 0) == 1

    def clear_job_activation_url(self, job_id: int) -> None:
        """链接失效：清 URL 保持 pending，下小时重搜。"""
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """UPDATE registration_jobs
                   SET activation_url='', activation_username='',
                       activation_url_saved_at='', updated_at=?
                   WHERE id=?
                     AND activation_status IN ('pending', 'activating')""",
                (now, job_id),
            )

    def claim_job_activation(self, job_id: int) -> dict[str, Any] | None:
        """认领浏览器激活，防双开。"""
        now = _utc_now()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE registration_jobs
                   SET activation_status='activating',
                       activation_attempts=activation_attempts + 1,
                       updated_at=?
                   WHERE id=?
                     AND status='succeeded'
                     AND activation_status='pending'
                     AND IFNULL(activation_url, '') != ''""",
                (now, job_id),
            )
            if int(cur.rowcount or 0) != 1:
                return None
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def release_job_activation_claim(self, job_id: int) -> None:
        """浏览器激活未成功：从 activating 放回 pending。"""
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """UPDATE registration_jobs
                   SET activation_status='pending', updated_at=?
                   WHERE id=? AND activation_status='activating'""",
                (now, job_id),
            )

    def reset_stale_activating_jobs(self) -> int:
        """进程重启：卡住的 activating 修回 pending。"""
        now = _utc_now()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE registration_jobs
                   SET activation_status='pending', updated_at=?
                   WHERE activation_status='activating'
                     AND status='succeeded'""",
                (now,),
            )
            return int(cur.rowcount or 0)

    def mark_job_activation_checked(self, job_id: int) -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """UPDATE registration_jobs
                   SET activation_checked_at=?, updated_at=?
                   WHERE id=?""",
                (now, now, job_id),
            )

    def mark_job_activated(self, job_id: int) -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """UPDATE registration_jobs
                   SET activation_status='activated', activation_activated_at=?,
                       form_status='pending', updated_at=?
                   WHERE id=?
                     AND activation_status IN ('pending', 'activating', '')
                     AND IFNULL(form_status, '') NOT IN ('filled', 'failed')""",
                (now, now, job_id),
            )

    def mark_job_activation_failed(self, job_id: int, error: str) -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """UPDATE registration_jobs
                   SET activation_status='failed', last_error=?, updated_at=?
                   WHERE id=?
                     AND IFNULL(activation_status, '') NOT IN ('activated')""",
                (error[:500], now, job_id),
            )

    def mark_job_form_pending(self, job_id: int) -> None:
        """激活成功后标记待填表。"""
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """UPDATE registration_jobs
                   SET form_status='pending', updated_at=?
                   WHERE id=?""",
                (now, job_id),
            )

    def get_jobs_pending_form(self) -> list[dict[str, Any]]:
        """待填表任务：已激活且未填完。载入超时插队（form_boost）优先。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM registration_jobs
                   WHERE activation_status='activated'
                     AND form_status='pending'
                     AND status='succeeded'
                   ORDER BY form_boost DESC, id"""
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_job_form_filled(
        self, job_id: int, screenshot_path: str = "", nnc1_duration: str = ""
    ) -> None:
        """填表成功。"""
        now = _utc_now()
        dur = (nnc1_duration or "").strip()
        with self._conn() as conn:
            if dur:
                conn.execute(
                    """UPDATE registration_jobs
                       SET form_status='filled', form_filled_at=?,
                           form_screenshot_path=?, nnc1_duration=?, updated_at=?
                       WHERE id=?""",
                    (now, screenshot_path, dur, now, job_id),
                )
            else:
                conn.execute(
                    """UPDATE registration_jobs
                       SET form_status='filled', form_filled_at=?,
                           form_screenshot_path=?, updated_at=?
                       WHERE id=?""",
                    (now, screenshot_path, now, job_id),
                )

    def job_form_outcome_written(self, job_id: int) -> bool:
        """填表成功或失败是否已落库（keep-browser 时 bot 会先写）。"""
        row = self.get_registration_job(job_id)
        if not row:
            return False
        return str(row.get("form_status") or "") in ("filled", "failed")

    def mark_job_form_failed(
        self,
        job_id: int,
        error: str,
        screenshot_path: str = "",
        nnc1_duration: str = "",
    ) -> None:
        """填表失败。有截图则写入 form_screenshot_path。"""
        now = _utc_now()
        shot = (screenshot_path or "").strip()
        dur = (nnc1_duration or "").strip()
        err = (error or "")[:2000]
        sets = ["form_status='failed'", "last_error=?", "updated_at=?"]
        params: list[Any] = [err, now]
        if shot:
            sets.append("form_screenshot_path=?")
            params.append(shot)
        if dur:
            sets.append("nnc1_duration=?")
            params.append(dur)
        params.append(job_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE registration_jobs SET {', '.join(sets)} WHERE id=?",
                params,
            )

    def reset_job_form_retry(self, job_id: int) -> dict[str, Any] | None:
        """重跑填表：仅允许 form_status='failed' 的任务，重置为 pending。"""
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return None
            if str(row["form_status"]) != "failed":
                return dict(row)
            conn.execute(
                """UPDATE registration_jobs
                   SET form_status='pending', last_error='',
                       nnc1_duration='', form_boost=0, nnc1_loading_retries=0,
                       updated_at=?
                   WHERE id = ?""",
                (now, job_id),
            )
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def requeue_job_form_loading_timeout(self, job_id: int) -> dict[str, Any] | None:
        """载入中超时：限一次 pending+form_boost 插队。第二次返回 None，保持 failed。"""
        from src.browser.icris_errors import NNC1_LOADING_RETRY_CAP

        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                return None
            retries = int(row["nnc1_loading_retries"] or 0)
            status = str(row["form_status"] or "")
            if retries >= NNC1_LOADING_RETRY_CAP:
                return None
            if status != "failed":
                return None
            conn.execute(
                """UPDATE registration_jobs
                   SET form_status='pending', form_boost=1,
                       nnc1_loading_retries=?, updated_at=?
                   WHERE id = ?""",
                (retries + 1, now, job_id),
            )
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_job_result_messages(
        self, job_id: int, result_messages: list[Any] | None
    ) -> None:
        """Flush step logs while job is still running (does not change status)."""
        import json

        if result_messages is None:
            return
        try:
            msgs = json.dumps(list(result_messages), ensure_ascii=False)
        except (TypeError, ValueError):
            msgs = "[]"
        if not msgs:
            return
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE registration_jobs
                SET result_messages = ?, updated_at = ?
                WHERE id = ?
                """,
                (msgs, now, job_id),
            )

    def mark_job_failed(
        self,
        job_id: int,
        *,
        error: str,
        requeue: bool = False,
        available_at: str = "",
        package_dir: str = "",
        screenshot_path: str = "",
        result_messages: list[Any] | None = None,
        run_duration: str = "",
        success_screenshot_path: str = "",
    ) -> None:
        import json

        now = _utc_now()
        msgs = ""
        if result_messages is not None:
            try:
                msgs = json.dumps(list(result_messages), ensure_ascii=False)
            except (TypeError, ValueError):
                msgs = "[]"
        shot = (screenshot_path or "").strip()
        success_shot = (success_screenshot_path or shot).strip()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status, review_status FROM registration_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                return
            cur_status = str(row["status"] or "")
            review = str(row["review_status"] or "").lower()
            if cur_status == "cancelled":
                return
            if review == "rejected":
                requeue = False
            status = "pending" if requeue else "failed"
            conn.execute(
                """
                UPDATE registration_jobs
                SET status = ?,
                    last_error = ?,
                    package_dir = CASE WHEN ? != '' THEN ? ELSE package_dir END,
                    screenshot_path = CASE WHEN ? != '' THEN ? ELSE screenshot_path END,
                    success_screenshot_path = CASE WHEN ? != '' THEN ? ELSE success_screenshot_path END,
                    result_messages = CASE WHEN ? != '' THEN ? ELSE result_messages END,
                    available_at = CASE WHEN ? != '' THEN ? ELSE available_at END,
                    finished_at = CASE WHEN ? = 'failed' THEN ? ELSE NULL END,
                    run_duration = CASE WHEN ? != '' THEN ? ELSE run_duration END,
                    updated_at = ?
                WHERE id = ? AND status != 'cancelled'
                """,
                (
                    status,
                    (error or "")[:2000],
                    package_dir,
                    package_dir,
                    shot,
                    shot,
                    success_shot,
                    success_shot,
                    msgs,
                    msgs,
                    available_at,
                    available_at,
                    status,
                    now,
                    run_duration,
                    run_duration,
                    now,
                    job_id,
                ),
            )

    def cancel_registration_job(self, job_id: int) -> dict[str, Any] | None:
        """取消 pending/running 任务。running 任务由 worker/bot 轮询感知后终止。"""
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            cur_status = str(row["status"])
            if cur_status not in ("pending", "running"):
                return dict(row)
            conn.execute(
                """
                UPDATE registration_jobs
                SET status = 'cancelled', finished_at = ?, updated_at = ?,
                    last_error = CASE WHEN last_error = '' THEN 'cancelled by admin' ELSE last_error END
                WHERE id = ? AND status IN ('pending', 'running')
                """,
                (now, now, job_id),
            )
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def requeue_registration_job(self, job_id: int) -> dict[str, Any] | None:
        """将 failed/cancelled 任务重新入队（同 room 若已有活跃任务则拒绝）。"""
        now = _utc_now()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            status = str(row["status"])
            if status not in ("failed", "cancelled"):
                return dict(row)
            roomid = str(row["roomid"])
            active = conn.execute(
                """
                SELECT id FROM registration_jobs
                WHERE roomid = ? AND status IN ('pending', 'running')
                LIMIT 1
                """,
                (roomid,),
            ).fetchone()
            if active:
                return dict(row)
            conn.execute(
                """
                UPDATE registration_jobs
                SET status = 'pending',
                    review_status = '',
                    available_at = ?,
                    finished_at = NULL,
                    run_duration = '',
                    s03a_duration = '',
                    nnc1_duration = '',
                    updated_at = ?,
                    last_error = ''
                WHERE id = ?
                """,
                (now, now, job_id),
            )
            row = conn.execute(
                "SELECT * FROM registration_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_latest_registration_job(self, roomid: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM registration_jobs
                WHERE roomid = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (roomid,),
            ).fetchone()
        return dict(row) if row else None

    def _registration_jobs_where(
        self,
        *,
        status: str = "",
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        company_name: str = "",
        director_name: str = "",
        id_number: str = "",
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if keyword:
            kw = f"%{keyword}%"
            clauses.append("(company_name LIKE ? OR payload_json LIKE ?)")
            params.extend([kw, kw])
        if company_name:
            kw = f"%{company_name}%"
            clauses.append("(company_name LIKE ? OR payload_json LIKE ?)")
            params.extend([kw, kw])
        if director_name:
            kw = f"%{director_name}%"
            clauses.append("payload_json LIKE ?")
            params.append(kw)
        if id_number:
            kw = f"%{id_number}%"
            clauses.append("payload_json LIKE ?")
            params.append(kw)
        # created_at 存 UTC ISO（如 2026-08-22T03:17:05+00:00），
        # 前端传本地日期（YYYY-MM-DD），需转成 UTC 范围再用 ISO 字符串比较
        if date_from:
            try:
                from datetime import datetime, time, timezone, timedelta

                tz_sh = timezone(timedelta(hours=8))
                start_local = datetime.strptime(date_from, "%Y-%m-%d").astimezone(tz_sh)
                start_utc = start_local.astimezone(timezone.utc)
                clauses.append("created_at >= ?")
                params.append(start_utc.strftime("%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                pass
        if date_to:
            try:
                from datetime import datetime, time, timezone, timedelta

                tz_sh = timezone(timedelta(hours=8))
                end_local = datetime.combine(
                    datetime.strptime(date_to, "%Y-%m-%d").date(), time(23, 59, 59)
                ).astimezone(tz_sh)
                end_utc = end_local.astimezone(timezone.utc)
                clauses.append("created_at <= ?")
                params.append(end_utc.strftime("%Y-%m-%dT%H:%M:%S"))
            except (ValueError, TypeError):
                pass
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def count_registration_jobs(
        self,
        *,
        status: str = "",
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        company_name: str = "",
        director_name: str = "",
        id_number: str = "",
    ) -> int:
        where, params = self._registration_jobs_where(
            status=status,
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
            company_name=company_name,
            director_name=director_name,
            id_number=id_number,
        )
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM registration_jobs {where}",
                params,
            ).fetchone()
        return int(row["n"] if row else 0)

    def list_registration_jobs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        omit_result_messages: bool = False,
        status: str = "",
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        company_name: str = "",
        director_name: str = "",
        id_number: str = "",
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        where, params = self._registration_jobs_where(
            status=status,
            keyword=keyword,
            date_from=date_from,
            date_to=date_to,
            company_name=company_name,
            director_name=director_name,
            id_number=id_number,
        )
        with self._conn() as conn:
            select_sql = "SELECT *"
            if omit_result_messages:
                cols = [
                    str(r["name"])
                    for r in conn.execute("PRAGMA table_info(registration_jobs)").fetchall()
                    if str(r["name"]) != "result_messages"
                ]
                select_sql = "SELECT " + ", ".join(cols)
            rows = conn.execute(
                f"{select_sql} FROM registration_jobs {where} "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_job_screenshot_path(self, job_id: int, shot_type: str) -> tuple[bool, str]:
        """只读截图路径列。返回 (任务是否存在, 路径)。"""
        col = {
            "esubmit": "esubmit_screenshot_path",
            "success": "success_screenshot_path",
            "form": "form_screenshot_path",
        }.get((shot_type or "").strip().lower(), "screenshot_path")
        if col not in (
            "esubmit_screenshot_path",
            "success_screenshot_path",
            "form_screenshot_path",
            "screenshot_path",
        ):
            col = "screenshot_path"
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT {col} AS path FROM registration_jobs WHERE id = ?",
                (int(job_id),),
            ).fetchone()
        if not row:
            return False, ""
        return True, str(row["path"] or "").strip()

    def registration_job_stats(self, *, hours: float | None = None) -> dict[str, Any]:
        """注册任务统计；hours 不为空时增加近 N 小时成功率与最近失败列表。"""
        from datetime import timedelta

        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS n
                FROM registration_jobs
                GROUP BY status
                """
            ).fetchall()
            running = conn.execute(
                """
                SELECT id, roomid FROM registration_jobs
                WHERE status = 'running'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            recent_failures: list[dict[str, Any]] = []
            window_counts: dict[str, int] = {}
            if hours is not None and float(hours) > 0:
                cutoff = (
                    datetime.now(timezone.utc)
                    - timedelta(hours=max(1.0, float(hours)))
                ).isoformat()
                win_rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS n
                    FROM registration_jobs
                    WHERE COALESCE(finished_at, updated_at, created_at) >= ?
                    GROUP BY status
                    """,
                    (cutoff,),
                ).fetchall()
                window_counts = {str(r["status"]): int(r["n"]) for r in win_rows}
                fail_rows = conn.execute(
                    """
                    SELECT id, roomid, last_error, screenshot_path, updated_at, finished_at
                    FROM registration_jobs
                    WHERE status = 'failed'
                      AND COALESCE(finished_at, updated_at, created_at) >= ?
                    ORDER BY COALESCE(finished_at, updated_at, created_at) DESC
                    LIMIT 10
                    """,
                    (cutoff,),
                ).fetchall()
                recent_failures = [dict(r) for r in fail_rows]
        counts = {str(r["status"]): int(r["n"]) for r in rows}
        out: dict[str, Any] = {
            "counts": counts,
            "pending_count": int(counts.get("pending", 0)),
            "running_count": int(counts.get("running", 0)),
            "running_job_id": int(running["id"]) if running else None,
            "running_roomid": str(running["roomid"]) if running else "",
        }
        if hours is not None and float(hours) > 0:
            succ = int(window_counts.get("succeeded", 0))
            fail = int(window_counts.get("failed", 0))
            done = succ + fail
            out["hours"] = float(hours)
            out["window_counts"] = window_counts
            out["success_rate"] = round(succ / done, 4) if done else 0.0
            out["recent_failures"] = recent_failures
        return out

    def pipeline_ops_stats(self, *, hours: float | None = 24.0) -> dict[str, Any]:
        """运营统计：积压快照 + 窗口内注册/激活/填表成功率、耗时与按日序列。

        hours <= 0 表示不截时间窗（按日序列仍最多 30 天）。
        """
        hours_val = 0.0 if hours is None else float(hours)
        cutoff: str | None = None
        if hours_val > 0:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=hours_val)
            ).isoformat()
        daily_hours = min(hours_val, 30 * 24) if hours_val > 0 else 30 * 24
        daily_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=daily_hours)
        ).isoformat()

        with self._conn() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT status, activation_status, form_status, review_status,
                           run_duration, s03a_duration, nnc1_duration,
                           created_at, finished_at, updated_at,
                           activation_checked_at, activation_activated_at,
                           form_filled_at, id_already_registered,
                           nnc1_loading_retries, form_boost, source
                    FROM registration_jobs
                    """
                ).fetchall()
            ]

        backlog = {
            "register_pending": 0,
            "register_running": 0,
            "awaiting_review": 0,
            "activation_pending": 0,
            "form_pending": 0,
        }
        reg_ok = 0
        reg_fail = 0
        act_ok = 0
        act_fail = 0
        form_ok = 0
        form_fail = 0
        run_secs: list[int] = []
        s03a_secs: list[int] = []
        act_secs: list[int] = []
        nnc1_secs: list[int] = []
        e2e_secs: list[int] = []
        created = 0
        e2e_filled = 0
        review_rejected = 0
        review_approved = 0
        id_already = 0
        form_retries = 0
        source_counts = {"admin": 0, "wework": 0, "other": 0}
        daily_map: dict[str, dict[str, int]] = {}

        def _bump_daily(day: str, key: str) -> None:
            if not day:
                return
            if not _ts_in_window(day, daily_cutoff[:10] if daily_cutoff else None):
                return
            slot = daily_map.setdefault(
                day,
                {
                    "created": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "activated": 0,
                    "form_filled": 0,
                },
            )
            slot[key] = int(slot.get(key, 0)) + 1

        for row in rows:
            status = str(row.get("status") or "")
            act_st = str(row.get("activation_status") or "")
            form_st = str(row.get("form_status") or "")
            review = str(row.get("review_status") or "").lower()
            created_at = str(row.get("created_at") or "")
            finished_at = str(row.get("finished_at") or "")
            updated_at = str(row.get("updated_at") or "")
            act_checked = str(row.get("activation_checked_at") or "")
            act_at = str(row.get("activation_activated_at") or "")
            form_at = str(row.get("form_filled_at") or "")

            if status == "pending":
                backlog["register_pending"] += 1
            elif status == "running":
                backlog["register_running"] += 1
            elif status == "awaiting_review":
                backlog["awaiting_review"] += 1
            if act_st in ("pending", "activating"):
                backlog["activation_pending"] += 1
            if form_st == "pending":
                backlog["form_pending"] += 1

            if _ts_in_window(created_at, cutoff):
                created += 1
                source_counts[_source_bucket(str(row.get("source") or ""))] += 1

            reg_ts = _first_ts(finished_at, updated_at, created_at)
            if status in ("succeeded", "failed") and _ts_in_window(reg_ts, cutoff):
                if status == "succeeded":
                    reg_ok += 1
                else:
                    reg_fail += 1
                sec = parse_job_run_duration(str(row.get("run_duration") or ""))
                if sec is not None:
                    run_secs.append(sec)
                s03a = parse_job_run_duration(str(row.get("s03a_duration") or ""))
                if s03a is not None:
                    s03a_secs.append(s03a)

            act_ts = _first_ts(act_at, updated_at)
            if act_st in ("activated", "failed") and _ts_in_window(act_ts, cutoff):
                if act_st == "activated":
                    act_ok += 1
                else:
                    act_fail += 1
                start_dt = _parse_iso_dt(act_checked)
                end_dt = _parse_iso_dt(act_at)
                if start_dt and end_dt and end_dt >= start_dt:
                    act_secs.append(int(round((end_dt - start_dt).total_seconds())))

            form_ts = _first_ts(form_at, updated_at)
            if form_st in ("filled", "failed") and _ts_in_window(form_ts, cutoff):
                if form_st == "filled":
                    form_ok += 1
                else:
                    form_fail += 1
                nnc1 = parse_job_run_duration(str(row.get("nnc1_duration") or ""))
                if nnc1 is not None:
                    nnc1_secs.append(nnc1)

            if (
                status == "succeeded"
                and form_st == "filled"
                and _ts_in_window(_first_ts(form_at, updated_at), cutoff)
            ):
                e2e_filled += 1
                start_dt = _parse_iso_dt(created_at)
                end_dt = _parse_iso_dt(form_at)
                if start_dt and end_dt and end_dt >= start_dt:
                    e2e_secs.append(int(round((end_dt - start_dt).total_seconds())))

            if review == "rejected" and _ts_in_window(
                _first_ts(finished_at, updated_at), cutoff
            ):
                review_rejected += 1
            elif review == "approved" and _ts_in_window(
                _first_ts(finished_at, updated_at), cutoff
            ):
                review_approved += 1

            if int(row.get("id_already_registered") or 0) and _ts_in_window(
                _first_ts(updated_at, created_at), cutoff
            ):
                id_already += 1

            retries = int(row.get("nnc1_loading_retries") or 0)
            if retries and _ts_in_window(updated_at, cutoff):
                form_retries += retries

            _bump_daily(_iso_date(created_at), "created")
            if status == "succeeded":
                _bump_daily(_iso_date(_first_ts(finished_at, updated_at, created_at)), "succeeded")
            elif status == "failed":
                _bump_daily(_iso_date(reg_ts), "failed")
            if act_st == "activated":
                _bump_daily(_iso_date(_first_ts(act_at, updated_at)), "activated")
            if form_st == "filled":
                _bump_daily(_iso_date(_first_ts(form_at, updated_at)), "form_filled")

        daily_start = (
            datetime.now(timezone.utc) - timedelta(hours=daily_hours)
        ).date()
        daily_end = datetime.now(timezone.utc).date()
        daily: list[dict[str, Any]] = []
        cur = daily_start
        while cur <= daily_end:
            key = cur.isoformat()
            slot = daily_map.get(
                key,
                {
                    "created": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "activated": 0,
                    "form_filled": 0,
                },
            )
            daily.append({"date": key, **slot})
            cur += timedelta(days=1)

        e2e_stats = _seconds_stats(e2e_secs)
        e2e_rate = round(e2e_filled / created, 4) if created else 0.0
        review_done = review_rejected + review_approved
        reject_rate = (
            round(review_rejected / review_done, 4) if review_done else 0.0
        )

        return {
            "hours": hours_val,
            "backlog": backlog,
            "stages": {
                "register": _stage_payload(
                    reg_ok,
                    reg_fail,
                    _seconds_stats(run_secs),
                    extra={"s03a_duration": _seconds_stats(s03a_secs)},
                ),
                "activation": _stage_payload(
                    act_ok, act_fail, _seconds_stats(act_secs)
                ),
                "form": _stage_payload(form_ok, form_fail, _seconds_stats(nnc1_secs)),
            },
            "extras": {
                "created": created,
                "e2e_filled": e2e_filled,
                "e2e_rate": e2e_rate,
                "e2e_avg_minutes": e2e_stats["avg_minutes"],
                "review_rejected": review_rejected,
                "review_approved": review_approved,
                "review_reject_rate": reject_rate,
                "id_already_registered": id_already,
                "form_retries": form_retries,
                "source": source_counts,
            },
            "daily": daily,
        }

    def repair_rejected_jobs(self) -> int:
        """把已拒绝但仍为 pending/running 的僵尸单修回 failed。"""
        now = _utc_now()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE registration_jobs
                SET status = 'failed',
                    finished_at = CASE WHEN finished_at IS NULL OR finished_at = ''
                                       THEN ? ELSE finished_at END,
                    updated_at = ?
                WHERE IFNULL(review_status, '') = 'rejected'
                  AND status IN ('pending', 'running')
                """,
                (now, now),
            )
            return int(cur.rowcount or 0)

    def reset_stale_running_jobs(self, *, older_than_minutes: int = 120) -> int:
        """进程重启后把卡住的 running 回收为 pending。

        older_than_minutes=0 表示回收全部 running（启动时用）。
        """
        from datetime import timedelta

        now = _utc_now()
        if older_than_minutes <= 0:
            with self._conn() as conn:
                cur = conn.execute(
                    """
                    UPDATE registration_jobs
                    SET status = 'pending',
                        available_at = ?,
                        updated_at = ?,
                        last_error = CASE
                            WHEN last_error = '' THEN 'recovered running after restart'
                            ELSE last_error
                        END
                    WHERE status = 'running'
                      AND IFNULL(review_status, '') != 'rejected'
                    """,
                    (now, now),
                )
                return int(cur.rowcount or 0)

        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
        ).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE registration_jobs
                SET status = 'pending',
                    available_at = ?,
                    updated_at = ?,
                    last_error = CASE
                        WHEN last_error = '' THEN 'recovered stale running after restart'
                        ELSE last_error
                    END
                WHERE status = 'running'
                  AND IFNULL(review_status, '') != 'rejected'
                  AND (started_at IS NULL OR started_at < ?)
                """,
                (now, now, cutoff),
            )
            return int(cur.rowcount or 0)

