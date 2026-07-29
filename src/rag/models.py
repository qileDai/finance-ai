"""RAG 数据模型"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TextChunk:
    chunk_id: str
    doc_id: str
    chunk_index: int
    text: str
    source_path: str
    title: str
    token_count: int = 0


@dataclass
class RetrievedChunk:
    chunk_id: str
    text: str
    source_path: str
    title: str
    score: float
    channels: dict[str, float] = field(default_factory=dict)
