"""双路 RRF 融合检索"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from config.settings import settings
from src.rag.embedder import Embedder
from src.rag.models import RetrievedChunk
from src.rag.qdrant_store import QdrantVectorStore
from src.rag.sqlite_index import RagSqliteIndex

logger = logging.getLogger(__name__)
CAUTION_QUERY_RE = re.compile(r"注意|要注意|注意事项")
DURATION_QUERY_RE = re.compile(r"多久|周期|多长时间")
DURATION_CHUNK_MARKERS = ("审核周期", "3-4 周", "3-4周", "工作日", "正式提交")


def _bigram_set(text: str) -> set[str]:
    """提取 2 字 bigram 集合（去空白后）。"""
    t = re.sub(r"\s+", "", (text or ""))
    if len(t) < 2:
        return {t} if t else set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _bigram_jaccard(text_a: str, text_b: str) -> float:
    """两个文本的 2 字 bigram Jaccard 相似度。空集返回 0.0。"""
    a = _bigram_set(text_a)
    b = _bigram_set(text_b)
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _duration_supplement_queries(query: str) -> list[str]:
    if not _is_duration_query(query):
        return []
    return ["审核周期 开户审核 正式提交", "开户审核周期"]


def is_primary_source(source_path: str) -> bool:
    normalized = source_path.replace("\\", "/")
    for primary in settings.rag_primary_source_list():
        p = primary.replace("\\", "/")
        if normalized.endswith(p) or normalized == p:
            return True
    return False

def _is_caution_query(query: str) -> bool:
    return bool(CAUTION_QUERY_RE.search(query))

def _is_duration_query(query: str) -> bool:
    return bool(DURATION_QUERY_RE.search(query))

def _chunk_has_duration_content(text: str, step_title: str = "") -> bool:
    combined = f"{text}\n{step_title}"
    return any(marker in combined for marker in DURATION_CHUNK_MARKERS)

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
    def _apply_duration_boost(self, query: str, row, score: float) -> float:
        if not _is_duration_query(query):
            return score
        text = str(row["text"])
        step_title = str(row["step_title"] or "")
        if _chunk_has_duration_content(text, step_title):
            return score * 1.8
        return score
    def _rerank_with_query_boosts(
        self, query: str, results: list[RetrievedChunk],
    ) -> list[RetrievedChunk]:
        if not _is_caution_query(query) and not _is_duration_query(query):
            return results
        boosted: list[RetrievedChunk] = []
        for hit in results:
            score = hit.score
            if _is_caution_query(query) and (
                hit.chunk_kind == "caution" or "注意事项" in hit.text
            ):
                score *= 1.4
            if _is_duration_query(query) and _chunk_has_duration_content(
                hit.text, hit.step_title,
            ):
                score *= 1.8
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
            return self._rerank_with_query_boosts(query, results)
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
            if _is_duration_query(query) and _chunk_has_duration_content(
                str(row["text"]), str(row["step_title"] or ""),
            ):
                score = max(score, top_score * 1.3)
            expanded.append(
                self._row_to_chunk(
                    row,
                    score=score,
                    channels=channel_scores.get(chunk_id, {}),
                )
            )
        return self._rerank_with_query_boosts(query, expanded)

    def _mmr_rerank(
        self,
        candidates: list[RetrievedChunk],
        top_k: int,
        lambda_: float,
    ) -> list[RetrievedChunk]:
        """MMR 贪心选择：平衡相关性与多样性。

        mmr = lambda_ * score(d_i) - (1 - lambda_) * max(sim(d_i, d_j) for d_j in selected)
        首轮选 score 最高的；预计算候选对 bigram Jaccard 缓存。
        """
        if not candidates or len(candidates) <= top_k:
            return candidates
        bigrams = [_bigram_set(c.text) for c in candidates]
        n = len(candidates)
        # 预计算成对 Jaccard 相似度
        sim: list[list[float]] = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                union = bigrams[i] | bigrams[j]
                s = len(bigrams[i] & bigrams[j]) / len(union) if union else 0.0
                sim[i][j] = s
                sim[j][i] = s
        selected: list[int] = []
        remaining = set(range(n))
        while remaining and len(selected) < top_k:
            best_idx = -1
            best_score = -float("inf")
            for i in remaining:
                rel = candidates[i].score
                if not selected:
                    mmr = rel
                else:
                    max_sim = max(sim[i][j] for j in selected)
                    mmr = lambda_ * rel - (1.0 - lambda_) * max_sim
                if mmr > best_score:
                    best_score = mmr
                    best_idx = i
            if best_idx < 0:
                break
            selected.append(best_idx)
            remaining.discard(best_idx)
        return [candidates[i] for i in selected]

    def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        scope: str | None = None,
    ) -> list[RetrievedChunk]:
        top_k = top_k or settings.rag_top_k
        scope = (scope or settings.rag_scope or "all").strip().lower()
        search_queries = [query, *_duration_supplement_queries(query)]
        fetch_k = max(top_k * 4, 30)
        k = settings.rag_rrf_k
        fused_scores: dict[str, float] = {}
        channel_scores: dict[str, dict[str, float]] = {}

        def _keyword_all() -> list[list[tuple[str, float]]]:
            return [
                self.sqlite.keyword_search(sq, limit=fetch_k) for sq in search_queries
            ]

        def _vector_search() -> list[tuple[str, float]]:
            qdrant = self._get_qdrant()
            if qdrant is None:
                return []
            try:
                query_vector = self.embedder.embed_query(query)
                return qdrant.search(query_vector, limit=fetch_k)
            except Exception as e:
                logger.warning("Qdrant 向量检索失败，仅使用关键词: %s", e)
                return []

        # FTS 与 embed/向量检索并行，缩短检索墙钟时间
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_kw = pool.submit(_keyword_all)
            fut_vec = pool.submit(_vector_search)
            keyword_lists = fut_kw.result()
            vector_hits = fut_vec.result()

        for keyword_hits in keyword_lists:
            for rank, (chunk_id, score) in enumerate(keyword_hits, start=1):
                fused_scores[chunk_id] = fused_scores.get(chunk_id, 0.0) + 1.0 / (
                    k + rank
                )
                channel_scores.setdefault(chunk_id, {})["keyword"] = max(
                    channel_scores.get(chunk_id, {}).get("keyword", 0.0), score,
                )
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
            score = self._apply_duration_boost(query, row, score)
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
        expanded = self._expand_step_siblings(
            results,
            query=query,
            scope=scope,
            fused_scores=fused_scores,
            channel_scores=channel_scores,
        )
        if (
            getattr(settings, "rag_mmr_enabled", True)
            and len(expanded) > top_k
        ):
            pool_size = max(top_k * 2, 20)
            candidates = expanded[:pool_size]
            lambda_ = float(getattr(settings, "rag_mmr_lambda", 0.6) or 0.6)
            return self._mmr_rerank(candidates, top_k, lambda_)
        return expanded
