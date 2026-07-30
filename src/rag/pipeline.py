"""RAG 入库编排"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from config.settings import PROJECT_ROOT, settings
from src.rag.document_parser import (
    chunk_stats,
    file_content_hash,
    iter_knowledge_files,
    parse_document,
    should_exclude_path,
)
from src.rag.embedder import Embedder
from src.rag.qdrant_store import QdrantVectorStore
from src.rag.sqlite_index import RagSqliteIndex

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    ingested: int = 0
    skipped: int = 0
    deleted: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


@dataclass
class IngestFileResult:
    changed: bool
    chunk_count: int = 0
    char_total: int = 0
    region_stats: dict[str, int] | None = None
    source_path: str = ""
    preview: str = ""


class RagPipeline:
    def __init__(
        self,
        *,
        sqlite_index: RagSqliteIndex | None = None,
        embedder: Embedder | None = None,
        qdrant: QdrantVectorStore | None = None,
    ) -> None:
        self.sqlite = sqlite_index or RagSqliteIndex()
        self.embedder = embedder or Embedder()
        self.qdrant: QdrantVectorStore | None = qdrant
        if self.qdrant is None:
            try:
                self.qdrant = QdrantVectorStore(vector_size=self.embedder.vector_size)
                self.qdrant.ensure_collection()
            except Exception as e:
                logger.warning("Qdrant 不可用，入库仅写入 SQLite/FTS: %s", e)
                self.qdrant = None

    def ingest_directory(self, directory: Path | None = None) -> IngestResult:
        directory = directory or Path(settings.rag_knowledge_dir)
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        result = IngestResult()
        files = iter_knowledge_files(directory)
        if not files:
            logger.warning("知识库目录为空: %s", directory)
            return result
        for path in files:
            try:
                if self.ingest_file(path):
                    result.ingested += 1
                else:
                    result.skipped += 1
            except Exception as e:
                msg = f"{path}: {e}"
                logger.exception("入库失败 %s", path)
                result.errors.append(msg)
        return result

    def ingest_file(self, path: Path, *, force: bool = False) -> bool:
        return self.ingest_file_detail(path, force=force).changed

    def ingest_file_detail(self, path: Path, *, force: bool = False) -> IngestFileResult:
        path = path.resolve()
        rel_path = _relative_source_path(path)
        if should_exclude_path(path):
            logger.info("跳过排除文件: %s", rel_path)
            return IngestFileResult(changed=False, source_path=rel_path)

        content_hash = file_content_hash(path)
        existing = self.sqlite.get_document_by_path(rel_path)
        if existing and str(existing["content_hash"]) == content_hash and not force:
            logger.info("跳过未变更文档: %s", rel_path)
            return IngestFileResult(changed=False, source_path=rel_path)

        if existing:
            old_chunk_ids = self.sqlite.delete_document(str(existing["id"]))
            if self.qdrant is not None:
                self.qdrant.delete_by_chunk_ids(old_chunk_ids)

        doc_id = str(existing["id"]) if existing else str(uuid.uuid4())
        doc_id, chunks = parse_document(path, doc_id=doc_id)
        if not chunks:
            logger.warning("文档无有效分块: %s", rel_path)
            return IngestFileResult(changed=False, source_path=rel_path)

        self.sqlite.upsert_document(
            doc_id=doc_id,
            source_path=rel_path,
            title=path.stem,
            content_hash=content_hash,
        )
        self.sqlite.insert_chunks(chunks)
        if self.qdrant is not None:
            vectors = self.embedder.embed_texts([c.text for c in chunks])
            self.qdrant.upsert_chunks(chunks, vectors)
        else:
            logger.info("已跳过 Qdrant 向量写入（服务不可用）")
        logger.info("已入库 %s (%s chunks)", rel_path, len(chunks))
        char_total = sum(c.token_count for c in chunks)
        preview = chunks[0].text[:200] + ("…" if len(chunks[0].text) > 200 else "")
        return IngestFileResult(
            changed=True,
            chunk_count=len(chunks),
            char_total=char_total,
            region_stats=chunk_stats(chunks),
            source_path=rel_path,
            preview=preview,
        )


def _relative_source_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")
