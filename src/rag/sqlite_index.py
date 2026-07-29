"""SQLite 元数据与 FTS5 关键词检索"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from config.settings import PROJECT_ROOT, settings
from src.rag.models import TextChunk


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RagSqliteIndex:
    db_path: Path | None = None

    def __post_init__(self) -> None:
        if self.db_path is None:
            self.db_path = Path(settings.rag_db_path)
        if not self.db_path.is_absolute():
            self.db_path = PROJECT_ROOT / self.db_path
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
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    ingested_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    token_count INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    qdrant_point_id TEXT NOT NULL,
                    FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id);

                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    text,
                    tokenize = 'unicode61'
                );
                """
            )

    def get_document_by_path(self, source_path: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE source_path = ?",
                (source_path,),
            ).fetchone()
            return row

    def upsert_document(
        self,
        doc_id: str,
        source_path: str,
        title: str,
        content_hash: str,
    ) -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO documents (id, source_path, title, content_hash, ingested_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    title = excluded.title,
                    content_hash = excluded.content_hash,
                    ingested_at = excluded.ingested_at
                """,
                (doc_id, source_path, title, content_hash, now),
            )

    def delete_document(self, doc_id: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM chunks WHERE doc_id = ?",
                (doc_id,),
            ).fetchall()
            chunk_ids = [str(r["id"]) for r in rows]
            for chunk_id in chunk_ids:
                conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            return chunk_ids

    def insert_chunks(self, chunks: list[TextChunk]) -> None:
        with self._conn() as conn:
            for chunk in chunks:
                conn.execute(
                    """
                    INSERT INTO chunks (
                        id, doc_id, chunk_index, text, token_count,
                        metadata_json, qdrant_point_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk.chunk_id,
                        chunk.doc_id,
                        chunk.chunk_index,
                        chunk.text,
                        chunk.token_count,
                        "{}",
                        chunk.chunk_id,
                    ),
                )
                conn.execute(
                    "INSERT INTO chunks_fts (chunk_id, text) VALUES (?, ?)",
                    (chunk.chunk_id, chunk.text),
                )

    def get_chunks_by_ids(self, chunk_ids: list[str]) -> dict[str, sqlite3.Row]:
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" * len(chunk_ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT c.id, c.text, c.chunk_index, d.source_path, d.title
                FROM chunks c
                JOIN documents d ON d.id = c.doc_id
                WHERE c.id IN ({placeholders})
                """,
                chunk_ids,
            ).fetchall()
        return {str(r["id"]): r for r in rows}

    def keyword_search(self, query: str, *, limit: int = 20) -> list[tuple[str, float]]:
        q = (query or "").strip()
        if not q:
            return []
        results: list[tuple[str, float]] = []
        seen: set[str] = set()

        fts_query = _build_fts_query(q)
        if fts_query:
            with self._conn() as conn:
                rows = conn.execute(
                    """
                    SELECT chunk_id, bm25(chunks_fts) AS rank
                    FROM chunks_fts
                    WHERE chunks_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
            for row in rows:
                chunk_id = str(row["chunk_id"])
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                rank = float(row["rank"])
                score = 1.0 / (1.0 + max(rank, 0.0))
                results.append((chunk_id, score))

        if len(results) < limit:
            with self._conn() as conn:
                like_rows = conn.execute(
                    """
                    SELECT id AS chunk_id
                    FROM chunks
                    WHERE text LIKE ?
                    LIMIT ?
                    """,
                    (f"%{q}%", limit),
                ).fetchall()
            for row in like_rows:
                chunk_id = str(row["chunk_id"])
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                results.append((chunk_id, 0.5))
                if len(results) >= limit:
                    break
        return results

    def list_documents(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM documents ORDER BY source_path"
            ).fetchall()


def _build_fts_query(q: str) -> str:
    tokens = [t for t in q.split() if t.strip()]
    if len(tokens) > 1:
        return " OR ".join(f'"{token}"' for token in tokens)
    return f'"{q}"'
