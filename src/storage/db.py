"""企业微信外部群 SQLite 存储"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config.settings import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "wework_external.db"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExternalGroupStore:
    """外部客户群、消息收件箱、AI 回复审计"""

    db_path: Path = DB_PATH

    def __post_init__(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
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
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cursor TEXT NOT NULL DEFAULT '',
                    token TEXT NOT NULL DEFAULT ''
                );

                INSERT OR IGNORE INTO kf_cursor (id, cursor, token) VALUES (1, '', '');

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
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_runs_roomid
                    ON agent_runs(roomid);
                """
            )
            self._migrate_columns(conn)

    def _migrate_columns(self, conn: sqlite3.Connection) -> None:
        """增量添加 Phase 2/3 列"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(external_groups)")}
        migrations = {
            "form_token": "TEXT NOT NULL DEFAULT ''",
            "company_name": "TEXT NOT NULL DEFAULT ''",
            "package_dir": "TEXT NOT NULL DEFAULT ''",
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

    def get_recent_messages(self, roomid: str, *, limit: int = 10) -> list[str]:
        """取群最近消息与 AI 回复，供上下文兜底。"""
        with self._conn() as conn:
            inbox_rows = conn.execute(
                """
                SELECT content FROM message_inbox
                WHERE roomid = ? AND msgtype = 'text' AND content != ''
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (roomid, limit),
            ).fetchall()
            reply_rows = conn.execute(
                """
                SELECT reply_text FROM ai_replies
                WHERE roomid = ? AND reply_text != ''
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (roomid, max(limit // 2, 3)),
            ).fetchall()

        messages: list[str] = []
        for row in reversed(inbox_rows):
            text = str(row["content"]).strip()
            if text and text not in messages:
                messages.append(text)
        for row in reversed(reply_rows):
            text = str(row["reply_text"]).strip()
            if text and text not in messages:
                messages.append(text)
        return messages[-limit:]

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
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO agent_runs (
                    id, roomid, question, final_answer,
                    retrieval_score, answer_score, confidence,
                    action, retries, trace_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, roomid, question, final_answer,
                    retrieval_score, answer_score, confidence,
                    action, retries, trace_json, _utc_now(),
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

    def get_archive_seq(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT seq FROM archive_cursor WHERE id = 1").fetchone()
            return int(row["seq"]) if row else 0

    def set_archive_seq(self, seq: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE archive_cursor SET seq = ? WHERE id = 1", (seq,))

    def get_kf_cursor(self) -> tuple[str, str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT cursor, token FROM kf_cursor WHERE id = 1",
            ).fetchone()
            if not row:
                return "", ""
            return str(row["cursor"] or ""), str(row["token"] or "")

    def set_kf_cursor(self, cursor: str, token: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE kf_cursor SET cursor = ?, token = ? WHERE id = 1",
                (cursor, token),
            )

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
    ) -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO group_materials
                    (roomid, field_key, field_value, file_path, source, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(roomid, field_key) DO UPDATE SET
                    field_value = excluded.field_value,
                    file_path = CASE WHEN excluded.file_path != '' THEN excluded.file_path ELSE file_path END,
                    source = excluded.source,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (roomid, field_key, field_value, file_path, source, status, now, now),
            )

    def get_materials(self, roomid: str) -> dict[str, dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM group_materials WHERE roomid = ?",
                (roomid,),
            ).fetchall()
        return {str(r["field_key"]): dict(r) for r in rows}

    def list_all_materials_summary(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT g.roomid, g.name, g.status, g.company_name,
                       COUNT(m.id) AS material_count
                FROM external_groups g
                LEFT JOIN group_materials m ON g.roomid = m.roomid
                GROUP BY g.roomid
                ORDER BY g.updated_at DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

