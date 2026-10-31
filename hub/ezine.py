"""Ezines: 100-300 page collections for darknet.ca, built from stories.

An ezine is a folder data/ezines/<id>/ with:
  meta.json   (title, issue, tagline, date)
  pieces/     (01.md, 02.md, ... — each piece is a story's compiled
               story.md, or a standalone article text file)
  ezine.md    (compiled, with TOC)
  ezine.html  (single self-contained HTML in darknet.ca style:
               deep navy #050912, cyan #17cfff, Courier New — so it
               posts on darknet.ca as-is for subscribers)

100 pages = 20,000 words, 300 pages = 60,000 words (200 words/page).
A storyblocks story at 256k tokens is ~190,000 words, so an ezine
is comfortably inside the same ceiling — it is a collection, not
one unbroken generation.
"""
import json
import os
import re
import time

from .config import data_dir as _data_dir
from .story import word_count, slugify

WORDS_PER_PAGE = 200


def ezines_root() -> str:
    root = os.path.join(_data_dir(), "ezines")
    os.makedirs(root, exist_ok=True)
    return root


def _write(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _md_to_html_body(md: str) -> str:
    """Minimal Markdown -> HTML for ezine export (headings, paragraphs,
    bold/italic, lists). No external dependency."""
    import html as _html
    out = []
    for block in re.split(r"\n\s*\n", md.strip()):
        block = block.strip()
        if not block:
            continue
        if block.startswith("### "):
            out.append(f"<h3>{_html.escape(block[4:].strip())}</h3>")
        elif block.startswith("## "):
            out.append(f"<h2>{_html.escape(block[3:].strip())}</h2>")
        elif block.startswith("# "):
            out.append(f"<h1>{_html.escape(block[2:].strip())}</h1>")
        elif block.startswith("---"):
            out.append("<hr>")
        else:
            text = _html.escape(block)
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
            text = text.replace("\n", "<br>\n")
            out.append(f"<p>{text}</p>")
    return "\n".join(out)


class Ezine:
    def __init__(self):
        self.root = ezines_root()

    def list_ezines(self):
        return sorted(d for d in os.listdir(self.root)
                      if os.path.isdir(os.path.join(self.root, d)))

    def _dir(self, ezine_id: str) -> str:
        d = os.path.join(self.root, ezine_id)
        if not os.path.isdir(d):
            raise ValueError(f"no such ezine: {ezine_id!r}")
        return d

    def new_ezine(self, title: str, issue: str = "", tagline: str = "") -> str:
        ezine_id = f"{slugify(title)}-{time.strftime('%Y%m%d')}"
        d = os.path.join(self.root, ezine_id)
        os.makedirs(os.path.join(d, "pieces"), exist_ok=True)
        meta = {"title": title, "issue": issue, "tagline": tagline,
                "created": time.strftime("%Y-%m-%d")}
        _write(os.path.join(d, "meta.json"), json.dumps(meta, indent=2) + "\n")
        return ezine_id

    def add_story(self, ezine_id: str, story_id: str) -> str:
        """Add a compiled story as the next piece. Returns piece filename."""
        d = self._dir(ezine_id)
        story_dir = os.path.join(_data_dir(), "stories", story_id)
        story_md = os.path.join(story_dir, "story.md")
        if not os.path.exists(story_md):
            raise ValueError(f"story {story_id!r} is not compiled yet (run: hub story compile {story_id})")
        pieces = sorted(f for f in os.listdir(os.path.join(d, "pieces")) if f.endswith(".md"))
        fname = f"{len(pieces) + 1:02d}-{slugify(story_id)}.md"
        _write(os.path.join(d, "pieces", fname), _read(story_md))
        return fname

    def add_text(self, ezine_id: str, title: str, text: str) -> str:
        d = self._dir(ezine_id)
        pieces = sorted(f for f in os.listdir(os.path.join(d, "pieces")) if f.endswith(".md"))
        fname = f"{len(pieces) + 1:02d}-{slugify(title)}.md"
        _write(os.path.join(d, "pieces", fname), f"# {title}\n\n{text}\n")
        return fname

    def status(self, ezine_id: str) -> dict:
        d = self._dir(ezine_id)
        meta = json.loads(_read(os.path.join(d, "meta.json")))
        pieces = sorted(f for f in os.listdir(os.path.join(d, "pieces")) if f.endswith(".md"))
        words = sum(word_count(_read(os.path.join(d, "pieces", f))) for f in pieces)
        return {"ezine_id": ezine_id, "title": meta.get("title", ezine_id),
                "issue": meta.get("issue", ""), "tagline": meta.get("tagline", ""),
                "pieces": len(pieces), "words": words,
                "pages": round(words / WORDS_PER_PAGE, 1),
                "est_tokens": int(words * 1.33), "dir": d, "meta": meta}

    def compile(self, ezine_id: str) -> dict:
        d = self._dir(ezine_id)
        meta = json.loads(_read(os.path.join(d, "meta.json")))
        pieces = sorted(f for f in os.listdir(os.path.join(d, "pieces")) if f.endswith(".md"))
        if not pieces:
            raise ValueError("no pieces yet")
        md_parts = [f"# {meta.get('title', ezine_id)}\n",
                    f"_Issue {meta.get('issue', '')} — {meta.get('tagline', '')}_\n",
                    "## Contents\n"]
        for i, f in enumerate(pieces, 1):
            first = _read(os.path.join(d, "pieces", f)).split("\n")[0].lstrip("# ").strip()
            md_parts.append(f"{i}. {first}\n")
        for f in pieces:
            md_parts.append("\n---\n\n" + _read(os.path.join(d, "pieces", f)).strip())
        ezine_md = "\n".join(md_parts) + "\n"
        _write(os.path.join(d, "ezine.md"), ezine_md)
        body = _md_to_html_body(ezine_md)
        html_doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{meta.get('title', ezine_id)}</title>
<style>body{{background:#050912;color:#e6edf3;font-family:"Courier New",monospace;max-width:760px;margin:40px auto;padding:0 20px;line-height:1.6}}h1,h2,h3{{color:#17cfff}}a{{color:#17cfff}}hr{{border:1px solid #17cfff55}}</style>
</head><body>{body}<hr><p><i>Published on darknet.ca — {meta.get('title', ezine_id)}, issue {meta.get('issue', '')}</i></p></body></html>
"""
        _write(os.path.join(d, "ezine.html"), html_doc)
        return {"ezine_id": ezine_id, "pieces": len(pieces),
                "words": word_count(ezine_md), "md": os.path.join(d, "ezine.md"),
                "html": os.path.join(d, "ezine.html")}
