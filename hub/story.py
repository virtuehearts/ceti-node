"""StoryBlocks-style long-form writing on the local uncensored model.

Why this reaches 256k output within a limited context window:
  - The context window never holds the whole story. Each block is
    generated from: story bible + rolling summary + (optionally) the
    last ~2000 chars of the previous block + EmbeddingGemma style refs.
  - The full story lives on disk, block by block, in data/stories/<id>/.
  - If generation stops, `story write <id>` resumes at the next block
    number — nothing already written is regenerated.

Block rules (StoryBlocks, adapted from ~/workspace/ceti/ceti/story.py
pattern, local model instead of a cloud oracle):
  - ~200 words per page, 1-10 pages per block (default 3 pages).
  - Retry/expand if a block is under 70% of its target word count.
  - Summary rolls forward after each block and is compressed when it
    passes ~3000 chars, so context stays bounded no matter how long
    the story gets.
  - Style refs come from the hub's vector memory via EmbeddingGemma-2:
    voice inspiration only, never reproduced or quoted.
  - Rolling memory (hub/rolling.py, v0.6.1): canon.md + vector block
    index + MEMORY footer in the same GPU call as the prose, so no
    extra summary call per chapter. Prompt stays ~5k tokens at any
    book length (see rolling.py for the budget).

256k tokens ≈ 190,000 words ≈ 950 pages at 200 words/page. At ~40 tok/s
on a laptop GPU that is roughly 1.5-2 hours of generation plus reasoning
overhead, in blocks. Run it in the background and resume if needed.
"""
import json
import os
import re
import time

from . import memory
from .config import data_dir as _data_dir
from .servers import chat_complete, embed_text

WORDS_PER_PAGE = 200
TOKENS_PER_WORD = 1.33  # rough English estimate, for progress reporting


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "story"


def word_count(text: str) -> int:
    return len(text.split())


def stories_root() -> str:
    root = os.path.join(_data_dir(), "stories")
    os.makedirs(root, exist_ok=True)
    return root


def _write(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _temporal_enabled() -> bool:
    """HUB_TEMPORAL=1/true/yes/on enables the temporal lobe. Default
    OFF — when off, story.py never imports hub.temporal and prompt
    output is byte-identical to the pre-temporal build."""
    return os.environ.get("HUB_TEMPORAL", "").strip().lower() in {"1", "true", "yes", "on"}


def _with_timeline(prompt: str, section: str) -> str:
    """Insert a timeline section before the 'Write Chapter'
    instruction. Returns the prompt UNCHANGED when the section is
    empty — this is what keeps toggle-off output byte-identical."""
    if not section:
        return prompt
    block = "TIMELINE — events in order (do not contradict this sequence):\n" + section + "\n\n"
    marker = "Write Chapter "
    idx = prompt.find(marker)
    if idx == -1:
        return prompt + "\n\n" + block
    return prompt[:idx] + block + prompt[idx:]


def _run_gate(story_id: str, ident, beat: str):
    """Beat-gate, fail open. Returns (gate_dict_or_None, note_str).
    None + '' when the toggle is off (caller must not add gate keys
    then — toggle-off results stay identical to today). None + note
    when the checker itself errored: generation proceeds anyway, and
    the note travels in the result so the failure is visible, never
    silent."""
    if not _temporal_enabled():
        return None, ""
    try:
        from .temporal import TemporalMemory
        return TemporalMemory(story_id, ident=ident).check_beat(beat), ""
    except Exception as e:
        return None, f"beat-gate check failed, proceeding anyway: {e}"


def _run_output_check(story_id: str, ident, text: str):
    """Output check, fail open. Same contract as _run_gate."""
    if not _temporal_enabled():
        return None, ""
    try:
        from .temporal import TemporalMemory
        return TemporalMemory(story_id, ident=ident).check_prose(text), ""
    except Exception as e:
        return None, f"output check failed, proceeding anyway: {e}"


def _save_flagged_draft(draft_dir: str, fname: str, text: str, violations, warnings) -> str:
    """Save held prose where compile()/status() can never pick it up:
    a drafts/ subdirectory (both list blocks/ and chapters/chNN/
    non-recursively, and neither matches the block/part filename
    patterns). The header says NOT COMPILED in plain language."""
    os.makedirs(draft_dir, exist_ok=True)
    path = os.path.join(draft_dir, fname)
    header = ("# FLAGGED DRAFT — NOT COMPILED\n\n"
              "_This part was held by the story-engine output check and is "
              "NOT part of the chapter. Review it, or re-run with force to "
              "accept it._\n\n## Violations\n")
    for v in violations:
        header += f"- {v}\n"
    if warnings:
        header += "\n## Warnings\n"
        for w in warnings:
            header += f"- {w}\n"
    _write(path, header + f"\n{text}\n")
    return path


_BIBLE_SYSTEM = "You are a novelist's plotting assistant. Write tight, concrete story bibles."
_BIBLE_USER = """Premise: {premise}

Story settings (use these, do not contradict them):
- Genre: {genre}
- Storyline: {storyline}
- Perspective: {perspective}
- Plot: {plot}
- Outline: {outline}
- Content rating: {rating}

Write a story bible with:
- Title and one-line logline
- Setting (2-3 sentences)
- Main characters (3-6, one line each: who they are, what they want)
- Three-act arc (a paragraph)
- 12 chapter beats (one line each, numbered, following the outline above if one was given)

Keep it tight. This bible will guide chapter-by-block generation."""

_BLOCK_SYSTEM = ("You are a novelist writing an ORIGINAL novel. Follow the perspective exactly. Do not repeat a sentence or paragraph — write each scene once, then move forward. End the part cleanly, then write the MEMORY footer, then stop. Do not add a new chapter heading. Write vivid, "
                 "concrete prose — show, don't tell. The STYLE REFERENCE "
                 "below is voice inspiration only: never reproduce it, "
                 "never quote it, write your own story.")
_BLOCK_USER = """STORY BIBLE:
{bible}

Story settings (keep these consistent in every chapter):
- Genre: {genre}
- Perspective: {perspective}
- Content rating: {rating}
- Plot: {plot}

PERSPECTIVE (non-negotiable): {perspective} — if first person, narrate as "I" in every sentence, never "he/she/Jake" as the narrator; if third person, never use "I" as the narrator.

CANON — characters, locations, open threads (do not contradict this):
{canon}

STORY SO FAR (summary of previous chapters):
{summary}

RETRIEVED PAST CHAPTERS (specific details — reuse names, places, promises exactly as written here, do not invent replacements):
{retrieved}

LAST CHAPTER'S ENDING (continue seamlessly from this voice and moment):
{tail}

STYLE REFERENCE (voice inspiration from memory — do not copy):
{style}

Write Chapter {n}: {beat}
About {pages} pages (~{words} words). Continue the story seamlessly
from where the summary leaves off. No preamble, no meta-commentary —
just the chapter.

After the chapter, on a new line, write exactly:
MEMORY:
- one short line per thing that changed: characters who changed and how, new threads opened, threads closed, location/time changes (3-6 lines).
This MEMORY footer is not part of the chapter — it builds the canon."""

_EXPAND_USER = """Continue the chapter seamlessly for about {words} more
words. No preamble, no recap — just continue the prose."""

_SUMMARY_SYSTEM = "You summarize novel chapters for continuity. Be concrete: names, events, what changed."
_SUMMARY_USER = "Summarize this chapter in ~150 words — who, what happened, what changed, any new threads:\n\n{block}"


def _complete(system: str, user: str, max_tokens: int, temperature: float, on_chunk=None) -> str:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if on_chunk is None:
        content, _reasoning = chat_complete(messages, max_tokens=max_tokens, temperature=temperature, repeat_penalty=1.15)
        return (content or "").strip()
    from .servers import stream_chat
    parts = []
    for chunk in stream_chat(messages, max_tokens=max_tokens, temperature=temperature, repeat_penalty=1.15):
        parts.append(chunk)
        on_chunk(chunk)
    return "".join(parts).strip()


class StoryWriter:
    def __init__(self, ident=None):
        self.root = stories_root()
        self.ident = ident

    def _dir(self, story_id: str) -> str:
        d = os.path.join(self.root, story_id)
        if not os.path.isdir(d):
            raise ValueError(f"no such story: {story_id!r} (see: hub story list)")
        return d

    def list_stories(self):
        return sorted(d for d in os.listdir(self.root)
                      if os.path.isdir(os.path.join(self.root, d)))

    @staticmethod
    def normalize_perspective(p: str) -> str:
        p = (p or "").strip().lower()
        mapping = {
            "1st": "first person", "1": "first person", "first": "first person",
            "first person": "first person", "first-person": "first person",
            "2nd": "second person", "2": "second person", "second": "second person",
            "second person": "second person", "second-person": "second person",
            "3rd": "third person limited", "3": "third person limited",
            "third": "third person limited", "third person": "third person limited",
            "third-person": "third person limited", "limited": "third person limited",
            "omniscient": "third person omniscient", "third person omniscient": "third person omniscient",
            "3rd-omniscient": "third person omniscient",
        }
        return mapping.get(p, p or "third person limited")

    @staticmethod
    def normalize_rating(r: str) -> str:
        r = (r or "").strip().upper()
        return r or "PG-13"

    def new_story(self, premise: str, title=None, genre="", storyline="",
                  perspective="third person limited", plot="", outline="",
                  rating="PG-13") -> str:
        premise = (premise or "").strip()
        if not premise:
            raise ValueError("empty premise")
        meta = {
            "genre": genre or "unspecified",
            "storyline": storyline or premise,
            "perspective": self.normalize_perspective(perspective),
            "plot": plot or "follow the bible's three-act arc",
            "outline": outline or "none given — bible generates 12 beats",
            "rating": self.normalize_rating(rating),
        }
        story_id = f"{slugify(title or premise[:40])}-{time.strftime('%Y%m%d-%H%M%S')}"
        d = os.path.join(self.root, story_id)
        os.makedirs(os.path.join(d, "blocks"))
        bible = _complete(_BIBLE_SYSTEM,
                          _BIBLE_USER.format(premise=premise, **meta),
                          max_tokens=1500, temperature=0.7)
        _write(os.path.join(d, "bible.md"), bible)
        _write(os.path.join(d, "summary.md"), "(no chapters written yet)\n")
        _write(os.path.join(d, "premise.txt"), premise + "\n")
        _write(os.path.join(d, "meta.json"), json.dumps(meta, indent=2) + "\n")
        return story_id

    def _meta(self, story_id: str) -> dict:
        path = os.path.join(self._dir(story_id), "meta.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return {"genre": "unspecified", "storyline": "", "perspective": "third person limited",
                "plot": "follow the bible's three-act arc", "outline": "none given",
                "rating": "PG-13"}

    def _beats(self, story_id: str):
        bible = _read(os.path.join(self._dir(story_id), "bible.md"))
        beats = []
        for line in bible.split("\n"):
            m = re.match(r"\s*\d+[.)]\s+(.*)", line)
            if m:
                beats.append(m.group(1).strip())
        return beats

    def _block_no(self, story_id: str) -> int:
        bdir = os.path.join(self._dir(story_id), "blocks")
        nums = [int(f[:2]) for f in os.listdir(bdir)
                if re.fullmatch(r"\d{2}\.md", f)]
        # blocks/99.md is the last two-digit slot; beyond that, use 3 digits
        return max(nums, default=0) + 1

    def _style_ref(self, query: str, sources=None) -> str:
        if self.ident is None:
            return "(no identity — pure imagination)"
        try:
            conn = memory.connect(_data_dir())
            emb = embed_text(query)
            if emb is None:
                return "(embedding server not running — pure imagination)"
            hits = memory.search(conn, self.ident.hash, emb, top_k=3, sources=sources)
        except Exception:
            return "(memory search failed — pure imagination)"
        if not hits:
            return "(no relevant memories)"
        parts = []
        for _score, text, source, _rid, _mdl in hits:
            parts.append(f"[from {source}]\n{text[:1200]}")
        return "\n\n---\n\n".join(parts)

    def next_block(self, story_id: str, pages: int = 3, on_chunk=None, sources=None, force: bool = False) -> dict:
        from .rolling import RollingMemory, split_prose_footer
        d = self._dir(story_id)
        pages = max(1, min(int(pages), 10))
        n = self._block_no(story_id)
        bible = _read(os.path.join(d, "bible.md"))
        summary = _read(os.path.join(d, "summary.md"))
        beats = self._beats(story_id)
        beat = beats[n - 1] if n - 1 < len(beats) else "continue the story toward its climax"
        # Beat-gate (toggle-gated, pre-generation): a staged dead
        # entity or a reopened resolved thread blocks generation
        # BEFORE any GPU call — zero tokens spent. force=True is the
        # explicit human override. Checker errors fail open (note).
        gate, gate_note = _run_gate(story_id, self.ident, beat)
        if gate and gate["blocked"] and not force:
            return {"story_id": story_id, "block": n, "words": 0, "pages": pages,
                    "path": None, "memory_lines": 0, "indexed_chunks": 0,
                    "blocked": True, "accepted": False,
                    "violations": gate["violations"], "warnings": gate["warnings"],
                    "gate": {"beat": gate, "output": None, "note": gate_note}}
        target_words = pages * WORDS_PER_PAGE
        style = self._style_ref(f"{beat} {summary[-500:]}", sources=sources)
        meta = self._meta(story_id)
        rolling = RollingMemory(story_id, ident=self.ident)
        # Rolling memory: canon + vector past chapters + last tail.
        # Prompt stays ~5k tokens at any book length (see rolling.py).
        canon = rolling.canon_text()
        retrieved = rolling.retrieve(f"{beat} {summary[-500:]}", exclude_block=n - 1)
        tail = rolling.last_tail()
        # Temporal lobe (toggle-gated): bounded timeline section, and
        # a zero-cost fallback to recent events + arcs + flags when the
        # embed server is down. Never raises into generation.
        timeline = ""
        if _temporal_enabled():
            try:
                from .temporal import TemporalMemory
                timeline = TemporalMemory(story_id, ident=self.ident).timeline_section(
                    beat, max_chars=1200, embed_fn=embed_text)
            except Exception:
                timeline = ""
        user_prompt = _BLOCK_USER.format(bible=bible[:4000], summary=summary[-3000:],
                                         style=style[:3600], n=n, beat=beat,
                                         pages=pages, words=target_words,
                                         genre=meta.get("genre", "unspecified"),
                                         perspective=meta.get("perspective", "third person limited"),
                                         rating=meta.get("rating", "PG-13"),
                                         plot=meta.get("plot", "")[:2000],
                                         canon=canon[:2000], retrieved=retrieved[:3000],
                                         tail=tail)
        raw = _complete(_BLOCK_SYSTEM,
                        _with_timeline(user_prompt, timeline),
                        max_tokens=min(8000, target_words * 2 + 1000), temperature=0.8,
                        on_chunk=on_chunk)
        # Single-pass memory: the MEMORY footer IS the summary delta.
        # No extra GPU summary call per chapter (old code paid one).
        text, memory_lines = split_prose_footer(raw)
        words = word_count(text)
        if words < target_words * 0.7:
            more = _complete(_BLOCK_SYSTEM,
                             _EXPAND_USER.format(words=target_words - words) + "\n\n" + text[-2000:],
                             max_tokens=4000, temperature=0.8)
            more, _extra = split_prose_footer(more)
            text = text + "\n\n" + more
            words = word_count(text)
        # Output check (toggle-gated, post-generation): the same
        # checker over the finished prose, BEFORE it is accepted.
        # Violations -> flagged draft (never compiled, never indexed,
        # summary untouched, same block number retried next run).
        # force=True accepts anyway. Checker errors fail open (note).
        out_check, out_note = _run_output_check(story_id, self.ident, text)
        if out_check and out_check["blocked"] and not force:
            draft_path = _save_flagged_draft(
                os.path.join(d, "blocks", "drafts"),
                f"{n:02d}.draft.md" if n < 100 else f"{n:03d}.draft.md",
                text, out_check["violations"], out_check["warnings"])
            return {"story_id": story_id, "block": n, "words": words, "pages": pages,
                    "path": None, "flagged_draft": draft_path,
                    "memory_lines": 0, "indexed_chunks": 0,
                    "blocked": False, "accepted": False,
                    "violations": out_check["violations"], "warnings": out_check["warnings"],
                    "gate": {"beat": gate, "output": out_check,
                             "note": " ".join(x for x in (gate_note, out_note) if x)}}
        # two-digit names for the first 99 blocks, then three digits
        fname = f"{n:02d}.md" if n < 100 else f"{n:03d}.md"
        path = os.path.join(d, "blocks", fname)
        _write(path, f"# Chapter {n}\n\n_{words} words_\n\n{text}\n")
        # Index this chapter (CPU embed, ~1-2s) + append canon delta.
        indexed = rolling.add_block(n, beat, text, memory_lines)
        # Temporal lobe: record this chapter's footer lines as events.
        # Degrades silently (never breaks the chapter) when off or when
        # the embed server is down — events link later via backfill.
        if _temporal_enabled():
            try:
                from .temporal import TemporalMemory
                TemporalMemory(story_id, ident=self.ident).record_block(
                    n, beat, memory_lines, embed_fn=embed_text)
            except Exception:
                pass
        if memory_lines:
            new_summary = (summary.rstrip() + f"\n\nChapter {n}: " + "; ".join(memory_lines)).strip()
        else:
            new_summary = summary
        if len(new_summary) > 3000:
            new_summary = _complete(_SUMMARY_SYSTEM,
                                    "Compress this running story summary to ~400 words, keeping every plot thread:\n\n" + new_summary,
                                    max_tokens=600, temperature=0.3)
        _write(os.path.join(d, "summary.md"), new_summary + "\n")
        result = {"story_id": story_id, "block": n, "words": words, "pages": pages, "path": path,
                  "memory_lines": len(memory_lines), "indexed_chunks": indexed.get("chunks", 0)}
        if _temporal_enabled():
            result["accepted"] = True
            result["blocked"] = False
            result["gate"] = {"beat": gate, "output": out_check,
                              "note": " ".join(x for x in (gate_note, out_note) if x)}
        return result


    def write_chapter_part(self, story_id: str, chapter_no: int, part_no: int, total_parts: int,
                           pages: int = 5, on_chunk=None, sources=None, force: bool = False) -> dict:
        """One part of a long chapter. A 35-50 page chapter is 7-10 parts
        of 5 pages each, same beat, rolling memory between parts. 5 pages
        matches the model's natural chunk (~950 words before it loops);
        10-page parts looped badly in testing.
        A single 40-page generation call would not fit in 16k context
        (prompt ~5k + prose ~13k + reasoning), so parts are the only
        honest way to do long chapters on this hardware."""
        from .rolling import RollingMemory, split_prose_footer
        d = self._dir(story_id)
        pages = max(1, min(int(pages), 10))
        beats = self._beats(story_id)
        beat = beats[chapter_no - 1] if chapter_no - 1 < len(beats) else "continue the story toward its climax"
        # Beat-gate, same contract as next_block: blocked beats spend
        # zero GPU tokens; force=True overrides; checker errors fail open.
        gate, gate_note = _run_gate(story_id, self.ident, beat)
        if gate and gate["blocked"] and not force:
            return {"story_id": story_id, "chapter": chapter_no, "part": part_no,
                    "words": 0, "pages": pages, "path": None,
                    "memory_lines": 0, "indexed_chunks": 0,
                    "blocked": True, "accepted": False,
                    "violations": gate["violations"], "warnings": gate["warnings"],
                    "gate": {"beat": gate, "output": None, "note": gate_note}}
        bible = _read(os.path.join(d, "bible.md"))
        summary = _read(os.path.join(d, "summary.md"))
        style = self._style_ref(f"{beat} {summary[-500:]}", sources=sources)
        meta = self._meta(story_id)
        rolling = RollingMemory(story_id, ident=self.ident)
        canon = rolling.canon_text()
        retrieved = rolling.retrieve(f"{beat} {summary[-500:]}")
        tail = rolling.last_tail()
        timeline = ""
        if _temporal_enabled():
            try:
                from .temporal import TemporalMemory
                timeline = TemporalMemory(story_id, ident=self.ident).timeline_section(
                    beat, max_chars=1200, embed_fn=embed_text)
            except Exception:
                timeline = ""
        part_prompt = _BLOCK_USER.format(
            bible=bible[:4000], summary=summary[-3000:], style=style[:3600],
            n=chapter_no, beat=beat, pages=pages, words=pages * WORDS_PER_PAGE,
            genre=meta.get("genre", "unspecified"),
            perspective=meta.get("perspective", "third person limited"),
            rating=meta.get("rating", "PG-13"), plot=meta.get("plot", "")[:2000],
            canon=canon[:2000], retrieved=retrieved[:3000], tail=tail)
        part_prompt = _with_timeline(part_prompt, timeline)
        # Part framing: same chapter, not a new chapter.
        if part_no == 1:
            part_prompt += f"\n\nThis is Chapter {chapter_no}, Part 1 of {total_parts}. Open the chapter here."
        elif part_no < total_parts:
            part_prompt += f"\n\nThis is Chapter {chapter_no}, Part {part_no} of {total_parts}. Continue seamlessly from the previous part — do NOT start a new chapter, do NOT repeat the opening, do NOT conclude the chapter."
        else:
            part_prompt += f"\n\nThis is Chapter {chapter_no}, Part {part_no} of {total_parts} (final part). Bring this chapter to a close — a beat, a reveal, or a cliffhanger — but do not start a new chapter."
        raw = _complete(_BLOCK_SYSTEM, part_prompt,
                        max_tokens=min(8000, pages * WORDS_PER_PAGE * 2 + 1000),
                        temperature=0.8, on_chunk=on_chunk)
        text, memory_lines = split_prose_footer(raw)
        from .dedup import dedup_truncate
        text = dedup_truncate(text)
        words = word_count(text)
        if words < pages * WORDS_PER_PAGE * 0.7:
            more = _complete(_BLOCK_SYSTEM,
                             _EXPAND_USER.format(words=pages * WORDS_PER_PAGE - words) + "\n\n" + text[-2000:],
                             max_tokens=4000, temperature=0.8)
            more, _extra = split_prose_footer(more)
            text = text + "\n\n" + dedup_truncate(more)
            words = word_count(text)
        # Output check, same contract as next_block: violations ->
        # flagged draft under chapters/chNN/drafts/ (never compiled),
        # force=True accepts anyway, checker errors fail open.
        out_check, out_note = _run_output_check(story_id, self.ident, text)
        if out_check and out_check["blocked"] and not force:
            draft_path = _save_flagged_draft(
                os.path.join(d, "chapters", f"ch{chapter_no:02d}", "drafts"),
                f"part{part_no:02d}.draft.md",
                text, out_check["violations"], out_check["warnings"])
            return {"story_id": story_id, "chapter": chapter_no, "part": part_no,
                    "words": words, "pages": pages, "path": None,
                    "flagged_draft": draft_path,
                    "memory_lines": 0, "indexed_chunks": 0,
                    "blocked": False, "accepted": False,
                    "violations": out_check["violations"], "warnings": out_check["warnings"],
                    "gate": {"beat": gate, "output": out_check,
                             "note": " ".join(x for x in (gate_note, out_note) if x)}}
        cdir = os.path.join(d, "chapters", f"ch{chapter_no:02d}")
        os.makedirs(cdir, exist_ok=True)
        path = os.path.join(cdir, f"part{part_no:02d}.md")
        _write(path, f"# Chapter {chapter_no}, Part {part_no}\n\n_{words} words_\n\n{text}\n")
        label = f"Chapter {chapter_no} Part {part_no}"
        indexed = rolling.add_block((chapter_no - 1) * 10 + part_no, beat, text, memory_lines, label=label)
        if _temporal_enabled():
            try:
                from .temporal import TemporalMemory
                TemporalMemory(story_id, ident=self.ident).record_block(
                    (chapter_no - 1) * 10 + part_no, beat, memory_lines,
                    label=label, embed_fn=embed_text)
            except Exception:
                pass
        if memory_lines:
            new_summary = (summary.rstrip() + f"\n\n{label}: " + "; ".join(memory_lines)).strip()
        else:
            new_summary = summary
        if len(new_summary) > 3000:
            new_summary = _complete(_SUMMARY_SYSTEM,
                                    "Compress this running story summary to ~400 words, keeping every plot thread:\n\n" + new_summary,
                                    max_tokens=600, temperature=0.3)
        _write(os.path.join(d, "summary.md"), new_summary + "\n")
        result = {"story_id": story_id, "chapter": chapter_no, "part": part_no,
                  "words": words, "pages": pages, "path": path,
                  "memory_lines": len(memory_lines), "indexed_chunks": indexed.get("chunks", 0)}
        if _temporal_enabled():
            result["accepted"] = True
            result["blocked"] = False
            result["gate"] = {"beat": gate, "output": out_check,
                              "note": " ".join(x for x in (gate_note, out_note) if x)}
        return result

    def compile_chapter(self, story_id: str, chapter_no: int) -> dict:
        d = self._dir(story_id)
        cdir = os.path.join(d, "chapters", f"ch{chapter_no:02d}")
        parts = sorted(f for f in os.listdir(cdir) if f.startswith("part") and f.endswith(".md"))
        if not parts:
            raise ValueError(f"no parts written for chapter {chapter_no}")
        texts = []
        total_words = 0
        for pf in parts:
            txt = _read(os.path.join(cdir, pf))
            txt = re.sub(r"^# Chapter [^\n]+\n\n_\d+ words_\n\n", "", txt, count=1)
            texts.append(txt.strip())
            total_words += word_count(txt)
        out = f"# Chapter {chapter_no}\n\n" + "\n\n".join(texts) + "\n"
        path = os.path.join(cdir, f"chapter{chapter_no:02d}.md")
        _write(path, out)
        return {"story_id": story_id, "chapter": chapter_no, "parts": len(parts),
                "words": total_words, "pages": round(total_words / WORDS_PER_PAGE, 1), "path": path}

    def status(self, story_id: str) -> dict:
        d = self._dir(story_id)
        blocks = sorted(f for f in os.listdir(os.path.join(d, "blocks")) if f.endswith(".md"))
        words = 0
        for b in blocks:
            txt = _read(os.path.join(d, "blocks", b))
            txt = re.sub(r"^(# Chapter \d+\n\n)_\d+ words_\n\n", r"\1", txt, count=1)
            txt = re.sub(r"^# Chapter \d+\n\n", "", txt, count=1)
            words += word_count(txt)
        # Chapter parts (long chapters) live under chapters/chNN/partNN.md,
        # not blocks/ — count them too, or chapter-written stories show 0 words.
        chapters_dir = os.path.join(d, "chapters")
        chapter_parts = 0
        if os.path.isdir(chapters_dir):
            for ch in sorted(os.listdir(chapters_dir)):
                cdir = os.path.join(chapters_dir, ch)
                if not os.path.isdir(cdir):
                    continue
                for pf in sorted(f for f in os.listdir(cdir) if f.startswith("part") and f.endswith(".md")):
                    txt = _read(os.path.join(cdir, pf))
                    txt = re.sub(r"^# Chapter [^\n]+\n\n_\d+ words_\n\n", "", txt, count=1)
                    words += word_count(txt)
                    chapter_parts += 1
        est_tokens = int(words * TOKENS_PER_WORD)
        return {"story_id": story_id, "blocks": len(blocks) + chapter_parts, "words": words,
                "pages": round(words / WORDS_PER_PAGE, 1),
                "est_tokens": est_tokens, "pct_of_256k": round(est_tokens / 256000 * 100, 1),
                "dir": d, "meta": self._meta(story_id)}

    def compile(self, story_id: str) -> dict:
        d = self._dir(story_id)
        bdir = os.path.join(d, "blocks")
        blocks = sorted(f for f in os.listdir(bdir) if f.endswith(".md"))
        if not blocks:
            raise ValueError("no blocks written yet")
        bible = _read(os.path.join(d, "bible.md"))
        title = bible.split("\n")[0].lstrip("# ").strip() or story_id
        md = [f"# {title}\n", f"_A hub story — {len(blocks)} chapters_\n"]
        for b in blocks:
            txt = _read(os.path.join(bdir, b))
            txt = re.sub(r"^(# Chapter \d+\n\n)_\d+ words_\n\n", r"\1", txt, count=1)
            md.append(txt.strip())
        story_md = "\n\n---\n\n".join(md) + "\n"
        md_path = os.path.join(d, "story.md")
        _write(md_path, story_md)
        txt_path = os.path.join(d, "story.txt")
        _write(txt_path, re.sub(r"[#*_`>]", "", story_md))
        return {"story_id": story_id, "chapters": len(blocks),
                "words": word_count(story_md), "md": md_path, "txt": txt_path}
