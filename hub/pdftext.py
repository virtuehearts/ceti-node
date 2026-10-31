"""Extract text from a PDF, stdlib only, with a pdftotext fallback.

Method: find stream objects, decompress FlateDecode streams with zlib,
then pull text from Tj / TJ operators (literal strings and hex strings).
This handles the simple, text-based PDFs that travel guides, articles
and reports usually are. It does not do OCR, and it will not recover
text from scanned-image PDFs — for those, pdftotext (if installed)
is tried first, because it handles more PDF variants.
"""
import re
import shutil
import subprocess
import zlib


def _pdftotext(path: str):
    tool = shutil.which("pdftotext")
    if not tool:
        return None
    try:
        out = subprocess.run([tool, "-layout", path, "-"], capture_output=True, text=True, timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout
    except Exception:
        pass
    return None


def _extract_strings(data: bytes) -> str:
    texts = []
    # Tj: (text) Tj  and  [...] TJ
    for m in re.finditer(rb"\((?:[^()\\]|\\.)*\)\s*Tj", data):
        s = m.group(0)
        inner = s[1:s.rfind(b")")]
        inner = inner.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
        texts.append(inner.decode("utf-8", errors="ignore"))
    for m in re.finditer(rb"\[([^\]]*)\]\s*TJ", data):
        arr = m.group(1)
        for sm in re.finditer(rb"\((?:[^()\\]|\\.)*\)", arr):
            inner = sm.group(0)[1:-1]
            texts.append(inner.decode("utf-8", errors="ignore"))
    # Hex strings: <48656c6c6f> Tj
    for m in re.finditer(rb"<([0-9a-fA-F]+)>\s*Tj", data):
        try:
            texts.append(bytes.fromhex(m.group(1).decode()).decode("utf-8", errors="ignore"))
        except Exception:
            continue
    return " ".join(texts)


def extract_pdf_text(path: str) -> str:
    via_tool = _pdftotext(path)
    if via_tool:
        return via_tool
    with open(path, "rb") as f:
        raw = f.read()
    parts = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, flags=re.DOTALL):
        stream = m.group(1)
        try:
            decompressed = zlib.decompress(stream)
        except Exception:
            decompressed = stream
        parts.append(_extract_strings(decompressed))
    text = "\n".join(p for p in parts if p.strip())
    if not text.strip():
        # Last resort: raw Tj strings in the whole file (uncompressed PDFs).
        text = _extract_strings(raw)
    return text.strip()
