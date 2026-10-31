"""Rolling memory: how a 300-page book stays consistent on a 16k window.

The problem with a single rolling summary: it is one string that gets
compressed lossily, so specific details (a character's scar, a promise
made in chapter 3, a location's layout) vanish by chapter 20, and the
model starts inventing replacements. That is how long books drift.

Rolling memory fixes it with three layers, all small, all bounded:

1. CANON (canon.md, always in context, ~500 tokens): characters,
   locations, open threads, timeline — updated by a MEMORY footer
   the model writes at the end of each chapter, in the SAME GPU call
   as the prose. No extra summary call per chapter (the old code paid
   a full reasoning-model call just to summarize — now the footer IS
   the summary delta).
2. VECTOR BLOCK INDEX (EmbeddingGemma, CPU): every chapter is chunked
   and embedded (source story:<id>:block:NNN) plus a digest
   (source story:<id>:digest:NNN). Before the next chapter, the beat
   + summary are embedded and the top past chapters are retrieved —
   specific detail, not a compressed blur. At most one chunk per
   chapter, score > 0.5, so one chapter cannot flood the prompt.
3. ROLLING SUMMARY + LAST TAIL: the summary file grows by footer
   lines (~250 chars/chapter, not ~750), so compression runs every
   ~12 chapters instead of every ~4. The last chapter's tail (~1500
   chars) is in context for seamless voice/ending continuity.

Prompt budget (tokens, rough, at any book length):
  system 150 + bible 1000 + canon 500 + summary 750 + retrieved 750
  + style 900 + last tail 375 + plot/beat 500 = ~4,925 tokens.
At ctx 16384 that leaves ~11k for generation; at ctx 8192 it still
leaves ~3k, enough for a 600-word block plus reasoning. The prompt
does NOT grow with the book — the book grows on disk and in the
vector index, the prompt stays capped. That is the whole trick.

GPU idle: embedding runs on the separate CPU embed server (:11435),
~1-2s per chapter, against minutes of GPU generation per chapter.
The RTX is effectively always firing; it never waits on EmbeddingGemma
for more than a second or two between blocks.
"""
import os
import re

from . import memory
from .config import data_dir as _data_dir
from .servers import embed_text

CHUNK_CHARS = 1200


def split_prose_footer(text: str):
    """Split a generated chapter into (prose, memory_lines).
    The model is instructed to end with a MEMORY: footer. If it does
    not, prose is returned whole and memory_lines is empty — the book
    still works, it just does not gain a canon delta that chapter."""
    m = re.search(r"\n\s*MEMORY:\s*\n?(.*)$", text, flags=re.DOTALL | re.IGNORECASE)
    if not m:
        # Inline footer variant: "**MEMORY FOOTER: ..." or "**MEMORY: ..."
        # tacked onto the last prose paragraph instead of its own line.
        m2 = re.search(r"\s*\*\*MEMORY(?: FOOTER)?\*?\*?:\s*(.*)$", text, flags=re.DOTALL | re.IGNORECASE)
        if m2 and len(m2.group(1).strip()) < 600:
            return text[:m2.start()].strip(), [m2.group(1).strip()]
        return text.strip(), []
    prose = text[:m.start()].strip()
    lines = [ln.strip("- *\t ").strip() for ln in m.group(1).strip().split("\n")]
    lines = [ln for ln in lines if ln]
    return prose, lines


class RollingMemory:
    def __init__(self, story_id: str, ident=None):
        self.story_id = story_id
        self.ident = ident
        self.dir = os.path.join(_data_dir(), "stories", story_id)
        self.canon_path = os.path.join(self.dir, "canon.md")

    def canon_text(self, max_chars=2000) -> str:
        if not os.path.exists(self.canon_path):
            return "(no canon yet — first chapters will build it)"
        with open(self.canon_path, encoding="utf-8") as f:
            text = f.read()
        # Keep the most recent canon (tail), since later chapters
        # supersede earlier status. Head (setup) is in the bible.
        return text[-max_chars:] if len(text) > max_chars else text

    def last_tail(self, max_chars=1500) -> str:
        # Most recent prose, whether it is a block or a chapter part.
        candidates = []
        bdir = os.path.join(self.dir, "blocks")
        if os.path.isdir(bdir):
            for f in os.listdir(bdir):
                if f.endswith(".md"):
                    candidates.append(os.path.join(bdir, f))
        cdir = os.path.join(self.dir, "chapters")
        if os.path.isdir(cdir):
            for root, _dirs, files in os.walk(cdir):
                for f in files:
                    if f.startswith("part") and f.endswith(".md"):
                        candidates.append(os.path.join(root, f))
        if not candidates:
            return ""
        latest = max(candidates, key=lambda fp: os.path.getmtime(fp))
        with open(latest, encoding="utf-8") as f:
            txt = f.read()
        txt = re.sub(r"^# Chapter [^\n]+\n\n_\d+ words_\n\n", "", txt, count=1)
        return txt[-max_chars:] if len(txt) > max_chars else txt

    def add_block(self, block_no: int, beat: str, prose: str, memory_lines, label: str = None) -> dict:
        """Index a finished chapter + append its canon delta.
        Returns counts. Runs on CPU via the embed server; GPU is idle
        only for the ~1-2s this takes, against minutes of generation."""
        if self.ident is None:
            return {"chunks": 0, "canon_lines": 0, "note": "no identity — not indexed"}
        # Canon delta first (cheap, disk only, never lost if embed is down).
        if memory_lines:
            with open(self.canon_path, "a", encoding="utf-8") as f:
                f.write(f"\n## {label or f'Chapter {block_no}'}: {beat}\n")
                for ln in memory_lines:
                    f.write(f"- {ln}\n")
        conn = memory.connect(_data_dir())
        chunks = 0
        clean = prose.strip()
        for i in range(0, len(clean), CHUNK_CHARS):
            piece = clean[i:i + CHUNK_CHARS].strip()
            if not piece:
                continue
            emb = embed_text(piece)
            if emb is None:
                break
            memory.add(conn, self.ident.hash, piece, emb,
                       source=f"story:{self.story_id}:block:{block_no:03d}", tags="story-block")
            chunks += 1
        if memory_lines:
            digest = f"{label or f'Chapter {block_no}'}: {beat}\n" + "\n".join(memory_lines)
            emb = embed_text(digest)
            if emb is not None:
                memory.add(conn, self.ident.hash, digest, emb,
                           source=f"story:{self.story_id}:digest:{block_no:03d}", tags="story-digest")
        return {"chunks": chunks, "canon_lines": len(memory_lines)}

    def retrieve(self, query: str, exclude_block: int = None, top_k: int = 5) -> str:
        """Past chapters relevant to the next beat, one chunk max per
        chapter, score > 0.5, best first. Excludes the last chapter
        (already in context as the tail)."""
        if self.ident is None:
            return "(no identity — no past chapters to retrieve)"
        emb = embed_text(query)
        if emb is None:
            return "(embedding server not running — no past chapters retrieved)"
        conn = memory.connect(_data_dir())
        hits = memory.search(conn, self.ident.hash, emb, top_k=50)
        prefix = f"story:{self.story_id}:"
        best_per_block = {}
        for score, text, source, _rid, _mdl in hits:
            if not source.startswith(prefix) or score <= 0.5:
                continue
            # source is story:<id>:block:NNN or story:<id>:digest:NNN
            block = source.rsplit(":", 1)[-1]
            if exclude_block is not None and block == f"{exclude_block:03d}":
                continue
            if block not in best_per_block or score > best_per_block[block][0]:
                best_per_block[block] = (score, text, source)
        ranked = sorted(best_per_block.values(), key=lambda x: -x[0])[:top_k]
        if not ranked:
            return "(no relevant past chapters)"
        parts = []
        for score, text, source in ranked:
            parts.append(f"[past chapter {source.rsplit(':', 1)[-1]}, score {score:.2f}]\n{text[:800]}")
        return "\n\n---\n\n".join(parts)
