"""Chat attachments: extract text from an uploaded file for embedding.

Text/code/markdown/HTML/JSON/CSV: UTF-8 decode (errors replaced).
PDF: hub.pdftext.extract_pdf_text on the stored file (pdftotext when
present, printable-strings fallback otherwise).
Images (png/jpg/gif/webp): no text — the file is stored and shown in
chat; a vision model reads it directly when selected. The embedded
text is just the filename, so the session knows the photo exists.
Cap: 10MB per file. chunk_text splits on 1500 chars at newlines.
"""

TEXT_EXTS = frozenset({
    ".txt", ".md", ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".htm",
    ".css", ".json", ".csv", ".xml", ".sh", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".log",
})
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
PDF_EXTS = frozenset({".pdf"})

MAX_BYTES = 10 * 1024 * 1024
CHUNK_CHARS = 1500


def kind_for(filename):
    """'text' | 'pdf' | 'image' | None (unsupported)."""
    ext = "." + (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if ext in TEXT_EXTS:
        return "text"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def extract_text(filename, data, stored_path=None):
    """(kind, text) for an uploaded file. PDF needs stored_path."""
    kind = kind_for(filename)
    if kind is None:
        return None, ""
    if kind == "image":
        return "image", ""
    if kind == "pdf":
        if not stored_path:
            return "pdf", ""
        try:
            from .pdftext import extract_pdf_text
            return "pdf", extract_pdf_text(stored_path) or ""
        except Exception:
            return "pdf", ""
    try:
        return "text", data.decode("utf-8", errors="replace")
    except Exception:
        return "text", ""


def chunk_text(text, size=CHUNK_CHARS):
    """Split text into <=size chunks, preferring newline boundaries."""
    text = text or ""
    if not text.strip():
        return []
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks
