"""知识文档解析与分块"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from src.rag.models import TextChunk

SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf", ".docx"}
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150


def file_content_hash(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest()


def load_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        parts = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(parts)
    if suffix == ".docx":
        from docx import Document

        doc = Document(str(path))
        return "\n\n".join(p.text.strip() for p in doc.paragraphs if p.text.strip())
    raise ValueError(f"不支持的文件类型: {path.suffix}")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_into_chunks(text: str) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []

    sections: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if re.match(r"^#{1,6}\s", line) and current:
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())

    chunks: list[str] = []
    for section in sections:
        if len(section) <= CHUNK_SIZE:
            if section:
                chunks.append(section)
            continue
        start = 0
        while start < len(section):
            end = min(start + CHUNK_SIZE, len(section))
            piece = section[start:end].strip()
            if piece:
                chunks.append(piece)
            if end >= len(section):
                break
            start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def parse_document(path: Path, *, doc_id: str | None = None) -> tuple[str, list[TextChunk]]:
    path = path.resolve()
    doc_id = doc_id or str(uuid.uuid4())
    title = path.stem
    raw = load_text(path)
    pieces = split_into_chunks(raw)
    rel_path = _relative_source_path(path)
    chunks: list[TextChunk] = []
    for idx, piece in enumerate(pieces):
        chunks.append(
            TextChunk(
                chunk_id=str(uuid.uuid4()),
                doc_id=doc_id,
                chunk_index=idx,
                text=piece,
                source_path=rel_path,
                title=title,
                token_count=len(piece),
            )
        )
    return doc_id, chunks


def _relative_source_path(path: Path) -> str:
    from config.settings import PROJECT_ROOT

    try:
        return str(path.resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def iter_knowledge_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files: list[Path] = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(path)
    return files
