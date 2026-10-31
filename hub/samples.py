"""Sample texts for vector memory: drop files in a folder, hub ingests them.

A sample is never copied into a story. It is chunked, embedded with
EmbeddingGemma-2, and stored in the same sqlite memory DB as chat
memories, tagged source="sample:<filename>". Story generation then
retrieves top-k sample chunks as STYLE REFERENCE — voice inspiration
only, with an explicit do-not-copy instruction in the block prompt.

Usage:
  hub samples ingest [folder]   (default: data/samples/)
  hub samples list              (counts by source)
"""
import os

from . import memory
from . import regula
from .config import data_dir as _data_dir
from .servers import embed_text

CHUNK_CHARS = 1200


def samples_dir() -> str:
    d = os.path.join(_data_dir(), "samples")
    os.makedirs(d, exist_ok=True)
    return d


def _chunks(text: str):
    text = text.strip()
    for i in range(0, len(text), CHUNK_CHARS):
        piece = text[i:i + CHUNK_CHARS].strip()
        if piece:
            yield piece


def ingest_folder(folder: str, ident) -> dict:
    """Embed every .txt/.md/.pdf file in folder into this identity's memory.
    Returns {files, chunks, skipped}."""
    conn = memory.connect(_data_dir())
    files = chunks = skipped = 0
    if not os.path.isdir(folder):
        raise ValueError(f"no such folder: {folder}")
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith((".txt", ".md", ".pdf")):
            continue
        path = os.path.join(folder, name)
        try:
            if name.lower().endswith(".pdf"):
                from .pdftext import extract_pdf_text
                text = extract_pdf_text(path)
            else:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
        except OSError:
            continue
        files += 1
        try:
            regula.record_file(_data_dir(), "sample", path, name)
        except Exception:
            pass
        for piece in _chunks(text):
            emb = embed_text(piece)
            if emb is None:
                skipped += 1
                continue
            memory.add(conn, ident.hash, piece, emb, source=f"sample:{name}", tags="sample")
            chunks += 1
    return {"files": files, "chunks": chunks, "skipped": skipped}


def sample_counts(ident) -> dict:
    conn = memory.connect(_data_dir())
    rows = conn.execute(
        "SELECT source, COUNT(*) FROM memories WHERE identity_hash = ? AND source LIKE 'sample:%' GROUP BY source",
        (ident.hash,)).fetchall()
    return {src: n for src, n in rows}
