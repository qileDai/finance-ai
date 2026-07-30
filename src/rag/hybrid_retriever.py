"""双路 RRF 融合检索"""

from __future__ import annotations

import logging
import re

from config.settings import settings
from src.rag.embedder import Embedder
from src.rag.models import RetrievedChunk
from src.rag.qdrant_store import QdrantVectorStore
from src.rag.sqlite_index import RagSqliteIndex

logger = logging.getLogger(__name__)

CAUTION_QUERY_RE = re.compile(r"注意|要注意|注意事项")


def is_primary_source(source_path: str) -> bool:
    normalized = source_path.replace("\\", "/")
    for primary in settings.rag_primary_source_list():
        p = primary.replace("\\", "/")
        if normalized.endswith(p) or normalized == p:
            return True
    return False


def _is_caution_query(query: str) -> bool:
    return bool(CAUTION_QUERY_RE.search(query))


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

    def _row_to_chunk(
        self,
        row,
        *,
        score: float,
        channels: dict[str, float],
    ) -> RetrievedChunk:
        return RetrievedChunk(
            chunk_id=str(row["id"]),
            text=str(row["text"]),
            source_path=str(row["source_path"]),
            title=str(row["title"]),
            score=score,
            channels=channels,
            region=str(row["region"] or ""),
            step_title=str(row["step_title"] or ""),
            step_id=str(row["step_id"] or ""),
            chunk_kind=str(row["chunk_kind"] or "script"),
        )

    def _apply_caution_boost(self, query: str, row, score: float) -> float:
        if not _is_caution_query(query):
            return score
        chunk_kind = str(row["chunk_kind"] or "script")
        text = str(row["text"])
        if chunk_kind == "caution" or "注意事项" in text:
            return score * 1.4
        return score

    def _rerank_for_caution_query(
        self, query: str, results: list[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        if not _is_caution_query(query):
            return results
        boosted: list[RetrievedChunk] = []
        for hit in results:
            score = hit.score
            if hit.chunk_kind == "caution" or "注意事项" in hit.text:
                score *= 1.4
            boosted.append(
                RetrievedChunk(
                    chunk_id=hit.chunk_id,
                    text=hit.text,
                    source_path=hit.source_path,
                    title=hit.title,
                    score=score,
                    channels=hit.channels,
                    region=hit.region,
                    step_title=hit.step_title,
                    step_id=hit.step_id,
                    chunk_kind=hit.chunk_kind,
                )
            )
        boosted.sort(key=lambda r: r.score, reverse=True)
        return boosted

    def _expand_step_siblings(
        self,
        results: list[RetrievedChunk],
        *,
        query: str,
        scope: str,
        fused_scores: dict[str, float],
        channel_scores: dict[str, dict[str, float]],
    ) -> list[RetrievedChunk]:
        seen = {r.chunk_id for r in results}
        step_ids = list({r.step_id for r in results if r.step_id})
        if not step_ids:
            return self._rerank_for_caution_query(query, results)

        expanded = list(results)
        top_score = max((r.score for r in results), default=0.0)
        for row in self.sqlite.get_chunks_by_step_ids(step_ids):
            chunk_id = str(row["id"])
            if chunk_id in seen:
                continue
            region = str(row["region"] or "")
            if scope == "hk" and region == "cn":
                continue
            if scope == "cn" and region == "hk":
                continue
            seen.add(chunk_id)
            score = fused_scores.get(chunk_id, 0.0) * 0.95
            chunk_kind = str(row["chunk_kind"] or "script")
            if _is_caution_query(query) and (
                chunk_kind == "caution" or "注意事项" in str(row["text"])
            ):
                score = max(score, top_score * 1.2)
            expanded.append(
                self._row_to_chunk(
                    row,
                    score=score,
                    channels=channel_scores.get(chunk_id, {}),
                )
            )
        return self._rerank_for_caution_query(query, expanded)

    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        scope: str | None = None,
    ) -> list[RetrievedChunk]:
        top_k = top_k or settings.rag_top_k
        scope = (scope or settings.rag_scope or "all").strip().lower()
        fetch_k = max(top_k * 4, 30)
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

        ordered_ids = sorted(
            fused_scores, key=lambda cid: fused_scores[cid], reverse=True,
        )
        rows = self.sqlite.get_chunks_by_ids(ordered_ids)

        results: list[RetrievedChunk] = []
        for chunk_id in ordered_ids:
            row = rows.get(chunk_id)
            if not row:
                continue
            region = str(row["region"] or "")
            if scope == "hk" and region == "cn":
                continue
            if scope == "cn" and region == "hk":
                continue

            score = fused_scores[chunk_id]
            source_path = str(row["source_path"])
            if is_primary_source(source_path):
                score *= settings.rag_primary_boost
            score = self._apply_caution_boost(query, row, score)

            results.append(
                self._row_to_chunk(
                    row,
                    score=score,
                    channels=channel_scores.get(chunk_id, {}),
                )
            )
            if len(results) >= top_k:
                break

        results.sort(key=lambda r: r.score, reverse=True)
        return self._expand_step_siblings(
            results,
            query=query,
            scope=scope,
            fused_scores=fused_scores,
            channel_scores=channel_scores,
        )
