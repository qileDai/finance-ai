"""Qdrant 向量存储与检索"""

from __future__ import annotations

import logging
import uuid
from typing import Any, TYPE_CHECKING

from config.settings import settings
from src.rag.models import TextChunk

if TYPE_CHECKING:
    from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)


def _require_qdrant_client():
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qmodels

        return QdrantClient, qmodels
    except ImportError as e:
        raise ImportError(
            "缺少 qdrant-client，请执行: pip install qdrant-client"
        ) from e


class QdrantVectorStore:
    def __init__(self, *, vector_size: int) -> None:
        QdrantClient, qmodels = _require_qdrant_client()
        self._qmodels = qmodels
        self.vector_size = vector_size
        self.collection = settings.qdrant_collection
        kwargs: dict[str, Any] = {"url": settings.qdrant_url}
        if settings.qdrant_api_key:
            kwargs["api_key"] = settings.qdrant_api_key
        self.client: QdrantClient = QdrantClient(**kwargs)

    def ensure_collection(self) -> None:
        names = {c.name for c in self.client.get_collections().collections}
        if self.collection in names:
            return
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=self._qmodels.VectorParams(
                size=self.vector_size,
                distance=self._qmodels.Distance.COSINE,
            ),
        )
        logger.info("created qdrant collection %s", self.collection)

    def upsert_chunks(
        self,
        chunks: list[TextChunk],
        vectors: list[list[float]],
    ) -> None:
        if not chunks:
            return
        if len(chunks) != len(vectors):
            raise ValueError("chunks 与 vectors 数量不一致")
        points = []
        for chunk, vector in zip(chunks, vectors):
            points.append(
                self._qmodels.PointStruct(
                    id=_point_id(chunk.chunk_id),
                    vector=vector,
                    payload={
                        "chunk_id": chunk.chunk_id,
                        "doc_id": chunk.doc_id,
                        "source_path": chunk.source_path,
                        "title": chunk.title,
                        "chunk_index": chunk.chunk_index,
                        "text": chunk.text[:500],
                    },
                )
            )
        self.client.upsert(collection_name=self.collection, points=points)

    def delete_by_chunk_ids(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        point_ids = [_point_id(cid) for cid in chunk_ids]
        self.client.delete(
            collection_name=self.collection,
            points_selector=self._qmodels.PointIdsList(points=point_ids),
        )

    def search(self, vector: list[float], *, limit: int = 20) -> list[tuple[str, float]]:
        hits = self.client.search(
            collection_name=self.collection,
            query_vector=vector,
            limit=limit,
            with_payload=True,
        )
        results: list[tuple[str, float]] = []
        for hit in hits:
            payload = hit.payload or {}
            chunk_id = str(payload.get("chunk_id") or hit.id)
            results.append((chunk_id, float(hit.score or 0.0)))
        return results


def _point_id(chunk_id: str) -> str:
    return str(uuid.UUID(chunk_id))
