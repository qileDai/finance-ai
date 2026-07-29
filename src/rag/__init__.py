"""RAG 检索：SQLite FTS5 关键词 + Qdrant 向量"""

__all__ = ["HybridRetriever", "RetrievedChunk"]


def __getattr__(name: str):
    if name == "HybridRetriever":
        from src.rag.hybrid_retriever import HybridRetriever

        return HybridRetriever
    if name == "RetrievedChunk":
        from src.rag.models import RetrievedChunk

        return RetrievedChunk
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
