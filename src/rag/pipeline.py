"""RAG 入库编排"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from config.settings import PROJECT_ROOT, settings
from src.rag.document_parser import (
    file_content_hash,
    iter_knowledge_files,
    parse_document,
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
        self.qdrant = qdrant or QdrantVectorStore(vector_size=self.embedder.vector_size)
        self.qdrant.ensure_collection()

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

    def ingest_file(self, path: Path) -> bool:
        path = path.resolve()
        content_hash = file_content_hash(path)
        rel_path = _relative_source_path(path)
        existing = self.sqlite.get_document_by_path(rel_path)
        if existing and str(existing["content_hash"]) == content_hash:
            logger.info("跳过未变更文档: %s", rel_path)
            return False

        if existing:
            old_chunk_ids = self.sqlite.delete_document(str(existing["id"]))
            self.qdrant.delete_by_chunk_ids(old_chunk_ids)

        doc_id = str(existing["id"]) if existing else str(uuid.uuid4())
        doc_id, chunks = parse_document(path, doc_id=doc_id)
        if not chunks:
            logger.warning("文档无有效分块: %s", rel_path)
            return False

        self.sqlite.upsert_document(
            doc_id=doc_id,
            source_path=rel_path,
            title=path.stem,
            content_hash=content_hash,
        )
        self.sqlite.insert_chunks(chunks)
        vectors = self.embedder.embed_texts([c.text for c in chunks])
        self.qdrant.upsert_chunks(chunks, vectors)
        logger.info("已入库 %s (%s chunks)", rel_path, len(chunks))
        return True


def _relative_source_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")
