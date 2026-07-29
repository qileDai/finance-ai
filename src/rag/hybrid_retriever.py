"""双路 RRF 融合检索"""

from __future__ import annotations

import logging

from config.settings import settings
from src.rag.embedder import Embedder
from src.rag.models import RetrievedChunk
from src.rag.qdrant_store import QdrantVectorStore
from src.rag.sqlite_index import RagSqliteIndex

logger = logging.getLogger(__name__)


class HybridRetriever:
    def __init__(
        self,
        *,
        sqlite_index: RagSqliteIndex | None = None,
        embedder: Embedder | None = None,
        qdrant: QdrantVectorStore | None = None,
    ) -> None:
        self.sqlite = sqlite_index or RagSqliteIndex()
        self.embedder = embedder or Embedder()
        self._qdrant = qdrant
        self._qdrant_unavailable = False

    def _get_qdrant(self) -> QdrantVectorStore | None:
        if self._qdrant_unavailable:
            return None
        if self._qdrant is None:
            try:
                self._qdrant = QdrantVectorStore(vector_size=self.embedder.vector_size)
            except ImportError as e:
                logger.warning("未安装 qdrant-client，仅使用关键词检索: %s", e)
                self._qdrant_unavailable = True
                return None
        return self._qdrant

    def retrieve(self, query: str, *, top_k: int | None = None) -> list[RetrievedChunk]:
        top_k = top_k or settings.rag_top_k
        fetch_k = max(top_k * 3, 20)
        k = settings.rag_rrf_k

        keyword_hits = self.sqlite.keyword_search(query, limit=fetch_k)
        vector_hits: list[tuple[str, float]] = []
        qdrant = self._get_qdrant()
        if qdrant is not None:
            try:
                query_vector = self.embedder.embed_query(query)
                vector_hits = qdrant.search(query_vector, limit=fetch_k)
            except Exception as e:
                logger.warning("Qdrant 向量检索失败，仅使用关键词: %s", e)

        fused_scores: dict[str, float] = {}
        channel_scores: dict[str, dict[str, float]] = {}

        for rank, (chunk_id, score) in enumerate(keyword_hits, start=1):
            fused_scores[chunk_id] = fused_scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            channel_scores.setdefault(chunk_id, {})["keyword"] = score

        for rank, (chunk_id, score) in enumerate(vector_hits, start=1):
            fused_scores[chunk_id] = fused_scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            channel_scores.setdefault(chunk_id, {})["vector"] = score

        if not fused_scores:
            return []

        ordered_ids = sorted(fused_scores, key=lambda cid: fused_scores[cid], reverse=True)[
            :top_k
        ]
        rows = self.sqlite.get_chunks_by_ids(ordered_ids)
        results: list[RetrievedChunk] = []
        for chunk_id in ordered_ids:
            row = rows.get(chunk_id)
            if not row:
                continue
            results.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    text=str(row["text"]),
                    source_path=str(row["source_path"]),
                    title=str(row["title"]),
                    score=fused_scores[chunk_id],
                    channels=channel_scores.get(chunk_id, {}),
                )
            )
        return results
