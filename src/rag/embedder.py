"""OpenAI Embedding 封装"""

from __future__ import annotations

import hashlib
import logging
import threading

from openai import OpenAI

from config.settings import settings

logger = logging.getLogger(__name__)

# text-embedding-3-small 默认维度
EMBEDDING_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}

_QUERY_CACHE: dict[str, list[float]] = {}
_QUERY_CACHE_LOCK = threading.Lock()
_QUERY_CACHE_MAX = 256


class Embedder:
    def __init__(self) -> None:
        timeout = float(getattr(settings, "openai_timeout_seconds", 20.0) or 20.0)
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_api_base,
            timeout=timeout,
        )
        self.model = settings.rag_embedding_model

    @property
    def vector_size(self) -> int:
        return EMBEDDING_DIMENSIONS.get(self.model, 1536)

    def embed_texts(self, texts: list[str], *, batch_size: int = 32) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            response = self.client.embeddings.create(model=self.model, input=batch)
            ordered = sorted(response.data, key=lambda x: x.index)
            vectors.extend(item.embedding for item in ordered)
            logger.debug("embedded batch %s-%s", i, i + len(batch))
        return vectors

    def embed_query(self, query: str) -> list[float]:
        key = hashlib.sha256(
            f"{self.model}\0{(query or '').strip()}".encode("utf-8")
        ).hexdigest()
        with _QUERY_CACHE_LOCK:
            cached = _QUERY_CACHE.get(key)
            if cached is not None:
                return cached
        vec = self.embed_texts([query])[0]
        with _QUERY_CACHE_LOCK:
            if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
                # 简单淘汰：清一半
                for k in list(_QUERY_CACHE.keys())[: _QUERY_CACHE_MAX // 2]:
                    _QUERY_CACHE.pop(k, None)
            _QUERY_CACHE[key] = vec
        return vec
