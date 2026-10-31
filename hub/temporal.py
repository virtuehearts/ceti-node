"""Temporal lobe: story memory over time, as day partitions + vector links.

The vector block index (rolling.py) answers "what is similar to this
beat?" but has no order: it returns isolated chunks, newest and oldest
mixed, with no before/after. Long books break exactly there — a
character dead in chapter 3 reappears in chapter 7, someone knows
something they never learned, a thread closes and quietly reopens.

The temporal lobe adds the missing dimension with three cheap pieces,
all rule-based, no model calls anywhere:

1. DAY PARTITIONS. Every event carries a day_key and a sequence
   number. For stories the default day is ingest order: one generation
   unit (chapter/part) is one day_key ("block-007"). That is free and
   always correct as an ordering device. A story-internal day ("Day 3"
   parsed from a beat or a MEMORY footer line) is a second, optional
   index on the same events. Both are sqlite-indexed lookups, never
   rescans of the whole store.
2. VECTOR LINKS. At ingest, one vector search per new event finds its
   nearest past events (same story, cosine over stored event
   embeddings, top 3 above 0.5) and stores the edges. Queries walk
   links instead of rescanning: find an entry point, follow edges a
   bounded number of hops, order by sequence. Vectors do what they
   are good at (fuzzy match, at ingest); the graph does what they
   are bad at (order, sequence, causality).
3. ARCS + DIGESTS + FLAGS. Per-entity/per-thread arc records track
   status (open/developing/resolved) and state (dead/knows/...).
   Active arcs surface first in the timeline section. When a day
   slice gets old or large it compacts into a digest row — protected
   lines (death, knowledge, possession, location) survive verbatim,
   and the digest replaces the day's events at retrieval time; the
   underlying events are never deleted. When a new beat mentions an
   entity whose arc says dead, a contradiction flag goes into the
   prompt (fail loud, never silently resolved).

Cost discipline (the whole point):
- Ingest: one embed per footer line (footer lines are 3-6 per
  chapter) plus one brute-force cosine pass over that story's past
  events (hundreds, not thousands — milliseconds in pure Python).
- Query: one short embed for the beat (or zero — recent events +
  arcs work without any server), a bounded link walk (indexed row
  lookups), and a char-capped section. Nothing runs during a
  generation call, nothing touches the GPU path.
- Embed server down: events are still recorded (embedding NULL,
  links_pending=1) and backfilled later by backfill_links(). The
  timeline section degrades to recent events + arcs + flags and
  never raises into generation.

Toggle: HUB_TEMPORAL=1 (also true/yes/on) enables the lobe in
story.py. Default OFF. When off, story.py never imports this module
and prompt output is byte-identical to the pre-temporal build.

Tables are ADDITIVE (CREATE TABLE IF NOT EXISTS) in the same
memory.db the vector index uses. The `memories` table is never
altered, and temporal rows never appear in memory.search() results
(they live in their own tables, not in `memories`).
"""
import array
import math
import os
import re
import sqlite3
import time

from . import memory
from .config import data_dir as _data_dir

TEMPORAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS temporal_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identity_hash TEXT NOT NULL,
  story_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  day_key TEXT NOT NULL,
  story_day TEXT,
  text TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  embedding BLOB,
  links_pending INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tmp_story_seq ON temporal_events(identity_hash, story_id, seq);
CREATE INDEX IF NOT EXISTS idx_tmp_day ON temporal_events(identity_hash, story_id, day_key);
CREATE INDEX IF NOT EXISTS idx_tmp_storyday ON temporal_events(identity_hash, story_id, story_day);
CREATE TABLE IF NOT EXISTS temporal_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identity_hash TEXT NOT NULL,
  story_id TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  linked_event_id INTEGER NOT NULL,
  score REAL NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(event_id, linked_event_id)
);
CREATE INDEX IF NOT EXISTS idx_tmp_links_event ON temporal_links(event_id);
CREATE INDEX IF NOT EXISTS idx_tmp_links_linked ON temporal_links(linked_event_id);
CREATE TABLE IF NOT EXISTS temporal_arcs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identity_hash TEXT NOT NULL,
  story_id TEXT NOT NULL,
  arc_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'developing',
  state TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  first_seq INTEGER NOT NULL,
  last_seq INTEGER NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(identity_hash, story_id, arc_key)
);
CREATE TABLE IF NOT EXISTS temporal_digests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identity_hash TEXT NOT NULL,
  story_id TEXT NOT NULL,
  day_key TEXT NOT NULL,
  digest TEXT NOT NULL,
  event_ids TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  UNIQUE(identity_hash, story_id, day_key)
);
"""

LINK_THRESHOLD = 0.5
LINK_TOP_K = 3
WALK_MAX_HOPS = 2
WALK_MAX_EVENTS = 12
TIMELINE_MAX_CHARS = 1200
DIGEST_MAX_CHARS = 800


def temporal_enabled() -> bool:
    """HUB_TEMPORAL=1/true/yes/on enables the lobe. Default OFF."""
    return os.environ.get("HUB_TEMPORAL", "").strip().lower() in {"1", "true", "yes", "on"}


def connect(data_dir: str):
    """Same DB file as the vector index, plus the temporal tables."""
    conn = memory.connect(data_dir)
    conn.executescript(TEMPORAL_SCHEMA)
    return conn


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _blob(vec) -> bytes:
    return array.array("f", vec).tobytes()


def _unblob(blob):
    vec = array.array("f")
    vec.frombytes(blob)
    return vec


# --- Rule-based classification (no model calls, ever) --------------------

# Order matters: first matching group wins for the arc state.
_SIGNAL_PATTERNS = [
    ("dead", "resolved", re.compile(r"\b(dies|died|is dead|dead|killed|death of|passes away|passed away)\b", re.I)),
    ("knows", "developing", re.compile(r"\b(learns|learned|discovers|discovered|finds out|found out|knows|realizes|realises|uncovers|uncovered)\b", re.I)),
    ("possession", "developing", re.compile(r"\b(takes|took|gets|got|loses|lost|carries|holds|has the|picks up|picked up|steals|stole|grabs|grabbed)\b", re.I)),
    ("location", "developing", re.compile(r"\b(arrives|arrived|leaves|left|moves to|moved to|is at|reaches|reached|enters|entered|returns to|returned to)\b", re.I)),
    ("open", "open", re.compile(r"\b(promises|promised|vows|vowed|must|needs to|unresolved|mystery|unanswered|waiting|swears|swore)\b", re.I)),
    ("resolved", "resolved", re.compile(r"\b(resolved|settled|closed|finished|concluded|completed)\b", re.I)),
]

# Lines matching any of these are protected: digest compaction keeps
# them verbatim, never drops them.
PROTECTED_PATTERNS = {
    "death": _SIGNAL_PATTERNS[0][2],
    "knowledge": _SIGNAL_PATTERNS[1][2],
    "possession": _SIGNAL_PATTERNS[2][2],
    "location": _SIGNAL_PATTERNS[3][2],
}

_ENTITY_STOPWORDS = {"The", "A", "An", "In", "On", "At", "After", "Before", "When", "Then",
                      "And", "But", "So", "It", "Its", "This", "That", "There", "Here"}
_ENTITY_RE = re.compile(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?")
_STORY_DAY_RE = re.compile(r"\b[Dd]ay\s+(\d+)\b")


def classify_line(text: str):
    """Return (state, status) for a footer/beat line, or ('', 'developing')."""
    for state, status, rx in _SIGNAL_PATTERNS:
        if rx.search(text):
            return state, status
    return "", "developing"


def is_protected(text: str) -> bool:
    return any(rx.search(text) for rx in PROTECTED_PATTERNS.values())


def protected_kinds(text: str):
    return [name for name, rx in PROTECTED_PATTERNS.items() if rx.search(text)]


def extract_entity(text: str):
    """First capitalized name in the line, skipping sentence starters.
    'Lena dies in the vault' -> 'Lena'. 'The vault code is learned
    by Jake' -> 'Jake'. Returns '' when no name is found."""
    for m in _ENTITY_RE.finditer(text):
        name = m.group(0)
        if name not in _ENTITY_STOPWORDS:
            return name
    return ""


def parse_story_day(text: str):
    """'Day 3', 'day 12', 'DAY 3:' -> 'day-3'. None when absent."""
    m = _STORY_DAY_RE.search(text or "")
    return f"day-{m.group(1)}" if m else None


def _mention_in(text: str, entity: str) -> bool:
    return re.search(r"\b" + re.escape(entity) + r"\b", text or "", re.I) is not None


class TemporalMemory:
    """Per-story temporal index. identity_hash scopes rows like the
    vector index does; pass ident (hub identity) or identity_hash."""

    def __init__(self, story_id: str, ident=None, identity_hash=None):
        self.story_id = story_id
        if identity_hash is not None:
            self.identity_hash = identity_hash
        elif ident is not None:
            self.identity_hash = ident.hash
        else:
            self.identity_hash = ""

    # -- ingest -----------------------------------------------------------

    def record_events(self, day_key: str, lines, embed_fn=None, story_day=None) -> dict:
        """Record footer/beat lines as events for one day_key.
        embed_fn(text) -> embedding or None; when None the event is
        stored with links_pending=1 and linked later by backfill_links.
        Returns counts."""
        lines = [ln.strip() for ln in (lines or []) if ln and ln.strip()]
        if not lines:
            return {"events": 0, "links": 0, "arcs": 0}
        conn = connect(_data_dir())
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM temporal_events WHERE identity_hash = ? AND story_id = ?",
            (self.identity_hash, self.story_id)).fetchone()
        seq = row[0] if row else 0
        n_links = 0
        arcs_touched = set()
        for ln in lines:
            seq += 1
            line_day = parse_story_day(ln) or story_day
            kinds = protected_kinds(ln)
            emb = None
            pending = 0
            if embed_fn is not None:
                try:
                    emb = embed_fn(ln)
                except Exception:
                    emb = None
                if emb is None:
                    pending = 1
            else:
                pending = 1
            cur = conn.execute(
                "INSERT INTO temporal_events (identity_hash, story_id, seq, day_key, story_day, text, kind, embedding, links_pending, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.identity_hash, self.story_id, seq, day_key, line_day, ln,
                 ",".join(kinds), _blob(emb) if emb is not None else None, pending, time.time()))
            event_id = cur.lastrowid
            if emb is not None:
                n_links += self._link_event(conn, event_id, emb)
            entity = extract_entity(ln)
            if entity:
                state, status = classify_line(ln)
                self._upsert_arc(conn, entity, state, status, ln, seq)
                arcs_touched.add(entity)
        conn.commit()
        return {"events": len(lines), "links": n_links, "arcs": len(arcs_touched)}

    def record_block(self, block_no: int, beat: str, memory_lines, label=None, embed_fn=None) -> dict:
        """Convenience wrapper for story.py: one generation unit is one
        day_key (ingest order, free and always correct); a story-day
        parsed from the beat is the optional second index."""
        day_key = label or f"block-{block_no:03d}"
        return self.record_events(day_key, memory_lines, embed_fn=embed_fn,
                                  story_day=parse_story_day(beat))

    def _link_event(self, conn, event_id: int, emb) -> int:
        """Link a new event to its nearest past events (same story,
        cosine over stored embeddings, top LINK_TOP_K above threshold).
        Returns the number of edges stored."""
        rows = conn.execute(
            "SELECT id, embedding FROM temporal_events WHERE identity_hash = ? AND story_id = ? AND id != ? AND embedding IS NOT NULL",
            (self.identity_hash, self.story_id, event_id)).fetchall()
        scored = []
        for oid, blob in rows:
            scored.append((_cosine(emb, _unblob(blob)), oid))
        scored.sort(reverse=True)
        n = 0
        for score, oid in scored:
            if score <= LINK_THRESHOLD or n >= LINK_TOP_K:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO temporal_links (identity_hash, story_id, event_id, linked_event_id, score, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (self.identity_hash, self.story_id, event_id, oid, score, time.time()))
            n += 1
        return n

    def backfill_links(self, embed_fn) -> dict:
        """Embed + link every pending event (embed server was down at
        ingest). Returns counts. Safe to call repeatedly."""
        conn = connect(_data_dir())
        rows = conn.execute(
            "SELECT id, text FROM temporal_events WHERE identity_hash = ? AND story_id = ? AND links_pending = 1 ORDER BY seq",
            (self.identity_hash, self.story_id)).fetchall()
        done = links = 0
        for eid, text in rows:
            try:
                emb = embed_fn(text)
            except Exception:
                emb = None
            if emb is None:
                continue
            conn.execute("UPDATE temporal_events SET embedding = ?, links_pending = 0 WHERE id = ?",
                         (_blob(emb), eid))
            links += self._link_event(conn, eid, emb)
            done += 1
        conn.commit()
        return {"backfilled": done, "links": links}

    # -- arcs ---------------------------------------------------------------

    def _upsert_arc(self, conn, entity: str, state: str, status: str, line: str, seq: int):
        """Death is sticky: once an arc is dead, later lines update
        last_seq/summary but never un-dead it. That is what makes the
        contradiction flag sound — resurrection needs an explicit
        human decision, not a quiet overwrite."""
        row = conn.execute(
            "SELECT status, state, summary, first_seq FROM temporal_arcs WHERE identity_hash = ? AND story_id = ? AND arc_key = ?",
            (self.identity_hash, self.story_id, entity)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO temporal_arcs (identity_hash, story_id, arc_key, status, state, summary, first_seq, last_seq, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.identity_hash, self.story_id, entity, status, state, line, seq, seq, time.time()))
            return
        old_status, old_state, old_summary, first_seq = row
        if old_state == "dead":
            new_status, new_state = old_status, old_state
        else:
            new_status, new_state = status, state
        summary = (old_summary + " | " + line) if old_summary else line
        if len(summary) > 400:
            summary = summary[-400:]
        conn.execute(
            "UPDATE temporal_arcs SET status = ?, state = ?, summary = ?, last_seq = ?, updated_at = ? WHERE identity_hash = ? AND story_id = ? AND arc_key = ?",
            (new_status, new_state, summary, seq, time.time(),
             self.identity_hash, self.story_id, entity))

    def active_arcs(self):
        conn = connect(_data_dir())
        rows = conn.execute(
            "SELECT arc_key, status, state, summary FROM temporal_arcs WHERE identity_hash = ? AND story_id = ? ORDER BY last_seq",
            (self.identity_hash, self.story_id)).fetchall()
        return [{"arc_key": k, "status": s, "state": st, "summary": sm} for k, s, st, sm in rows]

    # -- retrieval ------------------------------------------------------------

    def day_events(self, day_key: str):
        conn = connect(_data_dir())
        rows = conn.execute(
            "SELECT id, seq, day_key, story_day, text, kind FROM temporal_events WHERE identity_hash = ? AND story_id = ? AND day_key = ? ORDER BY seq",
            (self.identity_hash, self.story_id, day_key)).fetchall()
        return [{"id": r[0], "seq": r[1], "day_key": r[2], "story_day": r[3], "text": r[4], "kind": r[5]} for r in rows]

    def events_by_story_day(self, story_day: str):
        conn = connect(_data_dir())
        rows = conn.execute(
            "SELECT id, seq, day_key, story_day, text, kind FROM temporal_events WHERE identity_hash = ? AND story_id = ? AND story_day = ? ORDER BY seq",
            (self.identity_hash, self.story_id, story_day)).fetchall()
        return [{"id": r[0], "seq": r[1], "day_key": r[2], "story_day": r[3], "text": r[4], "kind": r[5]} for r in rows]

    def _fetch_events(self, conn, ids):
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT id, seq, day_key, story_day, text, kind FROM temporal_events WHERE id IN ({placeholders}) ORDER BY seq",
            (*ids,)).fetchall()
        return [{"id": r[0], "seq": r[1], "day_key": r[2], "story_day": r[3], "text": r[4], "kind": r[5]} for r in rows]

    def walk(self, entry_ids, max_hops=WALK_MAX_HOPS, max_events=WALK_MAX_EVENTS):
        """Follow link edges from entry events, both directions,
        bounded hops, visited set (no cycles), ordered by sequence."""
        conn = connect(_data_dir())
        seen = set(entry_ids)
        frontier = list(entry_ids)
        for _ in range(max_hops):
            if not frontier:
                break
            placeholders = ",".join("?" for _ in frontier)
            rows = conn.execute(
                f"SELECT event_id, linked_event_id FROM temporal_links WHERE event_id IN ({placeholders}) OR linked_event_id IN ({placeholders})",
                (*frontier, *frontier)).fetchall()
            nxt = []
            for a, b in rows:
                for x in (a, b):
                    if x not in seen:
                        seen.add(x)
                        nxt.append(x)
            frontier = nxt
        events = self._fetch_events(conn, sorted(seen))
        return events[:max_events]

    def nearest_events(self, query_embedding, top_k=3):
        conn = connect(_data_dir())
        rows = conn.execute(
            "SELECT id, embedding FROM temporal_events WHERE identity_hash = ? AND story_id = ? AND embedding IS NOT NULL",
            (self.identity_hash, self.story_id)).fetchall()
        scored = sorted(((_cosine(query_embedding, _unblob(blob)), eid) for eid, blob in rows),
                        reverse=True)
        return [eid for score, eid in scored[:top_k] if score > LINK_THRESHOLD]

    def recent_events(self, n=8):
        conn = connect(_data_dir())
        rows = conn.execute(
            "SELECT id, seq, day_key, story_day, text, kind FROM temporal_events WHERE identity_hash = ? AND story_id = ? ORDER BY seq DESC LIMIT ?",
            (self.identity_hash, self.story_id, n)).fetchall()
        events = [{"id": r[0], "seq": r[1], "day_key": r[2], "story_day": r[3], "text": r[4], "kind": r[5]} for r in rows]
        return list(reversed(events))

    # -- contradiction flags ---------------------------------------------------

    def check_contradictions(self, beat: str):
        """Fail loud, never silently resolve. Flags when a new beat
        mentions an entity whose arc says dead, or reopens a thread
        whose arc says resolved."""
        flags = []
        for arc in self.active_arcs():
            entity = arc["arc_key"]
            if not _mention_in(beat, entity):
                continue
            if arc["state"] == "dead":
                flags.append(
                    f"CONTRADICTION RISK: {entity} is recorded as dead "
                    f"(arc: {arc['summary'][:120]}), but the new beat mentions "
                    f"them — do NOT have them act, speak, or appear alive "
                    f"unless the beat explicitly says they were revived.")
            elif arc["status"] == "resolved" and arc["state"] == "resolved":
                flags.append(
                    f"CONTRADICTION RISK: the thread '{entity}' is recorded as "
                    f"resolved ({arc['summary'][:120]}), but the new beat "
                    f"reopens it — confirm this is intentional before continuing it.")
        return flags

    # -- digests -----------------------------------------------------------------

    # -- beat-gate / output check (enforcement, not prompt text) --------
    #
    # Last turn's A/B proved the point: a 518-char timeline section +
    # CONTRADICTION RISK flag in the prompt did NOT stop a 1.5B coder
    # model from staging a dead character speaking. Prompt text is a
    # recall aid, not enforcement. Enforcement lives here, outside the
    # model, in two places: check_beat runs BEFORE any GPU call (a
    # blocked beat spends zero tokens), check_prose runs AFTER
    # generation and BEFORE the prose is accepted into the chapter.
    # Both read the same arc tables; neither ever asks a model
    # whether something is contradictory.

    def check_beat(self, beat_text: str) -> dict:
        """Pre-generation gate over a beat. Same checker as
        check_prose; the difference is call-site only."""
        return self._check_text(beat_text)

    def check_prose(self, prose_text: str) -> dict:
        """Post-generation check over finished prose, before it is
        accepted. Same checker as check_beat."""
        return self._check_text(prose_text)

    def _check_text(self, text: str) -> dict:
        """Rule-based contradiction check. Returns {"blocked",
        "violations", "warnings", "entities"}.

        BLOCKS (generation must not proceed / prose must not be
        accepted without an explicit human override):
        - a DEAD arc's entity STAGED as acting/speaking in the present
          (name + action/speech verb, or interaction verb + entity).
        - a RESOLVED arc's key mentioned in a non-exempt sentence
          (reopening a closed thread).
        WARNINGS (reported, do NOT block):
        - the same staged/reopen hits when the sentence, or the beat
          as a whole, is explicitly framed as flashback/dream/memory/
          recording (a dead character appearing is sometimes
          legitimate writing; the human decides).
        - possession-holder conflicts and location mismatches. Both
          can happen legitimately off-screen (theft, gifts, travel),
          which is why they warn instead of blocking.
        NEVER fires:
        - mere mention ("Jake remembers Lena", "the vault where Lena
          died", possessive-only "Lena's key", restating the death).
        - anything when the store holds no arcs or events.

        LIMITS, stated plainly — this is a heuristic, not understanding:
        - Named mentions only. Pronouns ("she argues") are invisible
          to it; a beat staging a dead character purely by pronoun
          will pass. Entity lists come from arc keys only; canon.md
          is NOT parsed for state (canon is context, and parsing
          "Lena — dead" out of free text would be guessing).
        - Verb lists are finite. An unusual verb ("Lena pirouettes")
          will not match and will pass. Widen the lists when a miss
          is found in the wild; do not pretend they are complete.
        - Ambiguous sentences default to MENTION, not staged: a false
          block stops legitimate writing, while a miss is caught
          later by the output check or the human.
        """
        violations, warnings = [], []
        arcs = self.active_arcs()
        entities = [a["arc_key"] for a in arcs]
        sentences = _split_sentences(text)
        framed_whole = _beat_is_framed(text)
        for arc in arcs:
            entity = arc["arc_key"]
            for sent in sentences:
                if not _mention_in(sent, entity):
                    continue
                if arc["state"] == "dead":
                    if classify_mention(sent, entity) != "staged":
                        continue
                    framed = framed_whole or _sentence_is_framed(sent)
                    msg = (f"DEAD ENTITY STAGED: {entity} is recorded as dead "
                           f"(arc: {arc['summary'][:120]}), but is staged as "
                           f"acting/speaking here: '{sent[:120]}'")
                    if framed:
                        msg += " [framed as flashback/dream/memory — human decides]"
                        warnings.append(msg)
                    else:
                        violations.append(msg)
                elif arc["status"] == "resolved" and arc["state"] == "resolved":
                    if _sentence_is_exempt_mention(sent, entity):
                        continue
                    framed = framed_whole or _sentence_is_framed(sent)
                    msg = (f"RESOLVED THREAD REOPENED: '{entity}' is recorded "
                           f"as resolved ({arc['summary'][:120]}), but is "
                           f"treated as live here: '{sent[:120]}'")
                    if framed:
                        msg += " [framed as flashback/dream/memory — human decides]"
                        warnings.append(msg)
                    else:
                        violations.append(msg)
        warnings.extend(self._possession_warnings(sentences))
        warnings.extend(self._location_warnings(sentences))
        # Cap reporting volume on long prose; the count is preserved.
        if len(violations) > 20:
            violations = violations[:20] + [f"(... and {len(violations) - 20} more violations.)"]
        if len(warnings) > 20:
            warnings = warnings[:20] + [f"(... and {len(warnings) - 20} more warnings.)"]
        return {"blocked": bool(violations), "violations": violations,
                "warnings": warnings, "entities": entities}

    def _possession_warnings(self, sentences):
        """Current-holder tracking from recorded possession lines.
        A claim stages a different holder with no transfer shown ->
        warning (off-screen handoffs are normal fiction, so this
        warns instead of blocking). Transfer lines ("takes X from
        <holder>") and possessive attributions ("Lena's key") never
        warn."""
        holders = self._recorded_holders()
        if not holders:
            return []
        warnings = []
        for sent in sentences:
            words = _tokens(sent)
            for idx, w in enumerate(words):
                if w.lower() not in _POSSESSION_VERBS:
                    continue
                subject = _subject_before(words, idx)
                if not subject:
                    continue
                item = _noun_phrase_after(words, idx + 1)
                if not item or "'" in item:
                    continue  # possessive attribution ("Lena's key"), not a claim
                holder = holders.get(item)
                if holder is None or holder.lower() == subject.lower():
                    continue
                # "takes X from <holder>" is a legitimate transfer.
                m = re.search(r"\bfrom\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b", sent)
                if m and m.group(1).lower() == holder.lower():
                    continue
                warnings.append(
                    f"POSSESSION NOTE: {item} is recorded with {holder}, but "
                    f"here {subject} holds/takes it with no transfer shown — "
                    f"confirm the handoff happened off-screen: '{sent[:120]}'")
        return warnings

    def _recorded_holders(self):
        """item -> current holder (or None when last seen lost), from
        possession-kind event lines in sequence order. Last line wins;
        a lose/drop line clears the holder."""
        conn = connect(_data_dir())
        rows = conn.execute(
            "SELECT text FROM temporal_events WHERE identity_hash = ? AND story_id = ? AND kind LIKE '%possession%' ORDER BY seq",
            (self.identity_hash, self.story_id)).fetchall()
        holders = {}
        for (line,) in rows:
            holder = extract_entity(line)
            if not holder:
                continue
            m = re.search(r"\b(loses|lost|lose|drops|dropped|drop)\b", line, re.I)
            if m:
                item = _item_after_verb(line, m.end())
                if item:
                    holders[item] = None
                continue
            m = re.search(r"\b(" + "|".join(sorted(_POSSESSION_VERBS)) + r")\b", line, re.I)
            if m:
                item = _item_after_verb(line, m.end())
                if item:
                    holders[item] = holder
        return holders

    def _location_warnings(self, sentences):
        """Recorded-location tracking. A static "is at/in" claim for an
        entity whose recorded location differs, with no movement verb
        in the sentence, warns (travel across chapters is normal, so
        this warns instead of blocking). Movement verbs explain the
        change and never warn."""
        locs = self._recorded_locations()
        if not locs:
            return []
        warnings = []
        for sent in sentences:
            words = _tokens(sent)
            if any(w.lower() in _MOVEMENT_VERBS for w in words):
                continue
            for idx, w in enumerate(words):
                if w.lower() != "is" or idx + 2 >= len(words):
                    continue
                if words[idx + 1].lower() not in ("at", "in"):
                    continue
                name = words[idx - 1] if idx > 0 and words[idx - 1][:1].isupper() else None
                if not name or name not in locs or locs[name] is None:
                    continue
                claimed = _noun_phrase_after(words, idx + 2)
                if claimed and claimed != locs[name]:
                    warnings.append(
                        f"LOCATION NOTE: {name} is recorded at {locs[name]}, "
                        f"but here is at {claimed} with no movement shown — "
                        f"confirm the travel happened off-screen: '{sent[:120]}'")
        return warnings

    def _recorded_locations(self):
        """entity -> last recorded location (or None after a leave),
        from location-kind event lines in sequence order."""
        conn = connect(_data_dir())
        rows = conn.execute(
            "SELECT text FROM temporal_events WHERE identity_hash = ? AND story_id = ? AND kind LIKE '%location%' ORDER BY seq",
            (self.identity_hash, self.story_id)).fetchall()
        locs = {}
        for (line,) in rows:
            entity = extract_entity(line)
            if not entity:
                continue
            if re.search(r"\b(leaves|left|leave)\b", line, re.I):
                locs[entity] = None
                continue
            m = re.search(r"\b(?:arrives?|arrived|reaches?|reached|enters?|entered|is at|is in|returns? to|returned to)\s+(?:(?:at|in|to)\s+)?(?:the\s+)?([A-Za-z ]+?)(?:\.|,| and | to |$)", line, re.I)
            if m:
                loc = " ".join(m.group(1).strip().split()[:3]).lower()
                if loc:
                    locs[entity] = loc
        return locs


    # -- digests -----------------------------------------------------------------

    def compact_day(self, day_key: str, max_chars=DIGEST_MAX_CHARS) -> str:
        """Compact one day into a digest row. Protected lines survive
        verbatim; the first line survives; the rest collapse to a
        count. Events are NEVER deleted — the digest only replaces
        them at retrieval time for compacted days."""
        events = self.day_events(day_key)
        if not events:
            return ""
        kept = []
        for ev in events:
            if is_protected(ev["text"]) or ev is events[0]:
                kept.append(ev["text"])
        dropped = len(events) - len({t for t in kept})
        digest = "\n".join(f"- {t}" for t in kept)
        if dropped > 0:
            digest += f"\n- (... and {dropped} more events that day.)"
        digest = digest[:max_chars]
        conn = connect(_data_dir())
        conn.execute(
            "INSERT INTO temporal_digests (identity_hash, story_id, day_key, digest, event_ids, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(identity_hash, story_id, day_key) DO UPDATE SET digest = excluded.digest, event_ids = excluded.event_ids",
            (self.identity_hash, self.story_id, day_key, digest,
             ",".join(str(ev["id"]) for ev in events), time.time()))
        conn.commit()
        return digest

    def _digest_for_day(self, day_key: str):
        conn = connect(_data_dir())
        row = conn.execute(
            "SELECT digest FROM temporal_digests WHERE identity_hash = ? AND story_id = ? AND day_key = ?",
            (self.identity_hash, self.story_id, day_key)).fetchone()
        return row[0] if row else None

    # -- timeline section -----------------------------------------------------------

    def timeline_section(self, beat: str, max_chars=TIMELINE_MAX_CHARS, embed_fn=None,
                         include_arcs=True, include_flags=True) -> str:
        """Bounded timeline section for the chapter prompt. Order:
        contradiction flags first (fail loud), then active arcs, then
        the event sequence (link-walked when an embedding is
        available, recent events otherwise). Empty string when there
        is nothing to say — callers append nothing in that case.
        include_arcs/include_flags exist for ablation testing: the
        sequence-only variant omits both."""
        flags = self.check_contradictions(beat) if include_flags else []
        arcs = self.active_arcs() if include_arcs else []
        events = []
        if embed_fn is not None:
            try:
                emb = embed_fn(beat)
            except Exception:
                emb = None
            if emb is not None:
                entry = self.nearest_events(emb)
                if entry:
                    events = self.walk(entry)
        if not events:
            events = self.recent_events()
        if not flags and not arcs and not events:
            return ""
        parts = []
        for f in flags:
            parts.append(f)
        active = [a for a in arcs if a["status"] in ("open", "developing")]
        closed = [a for a in arcs if a["status"] == "resolved"]
        if active:
            parts.append("ACTIVE ARCS: " + "; ".join(
                f"{a['arc_key']} ({a['status']}" + (f", {a['state']}" if a["state"] else "") + ")"
                for a in active))
        if closed:
            parts.append("CLOSED ARCS: " + "; ".join(
                f"{a['arc_key']} ({a['state'] or a['status']})" for a in closed))
        # Sequence, with digest substitution for compacted days.
        seq_lines = []
        seen_digests = set()
        for ev in events:
            digest = self._digest_for_day(ev["day_key"])
            if digest is not None:
                if ev["day_key"] in seen_digests:
                    continue
                seen_digests.add(ev["day_key"])
                seq_lines.append(f"[{ev['day_key']} digest]\n{digest}")
            else:
                label = f"[{ev['day_key']}]"
                if ev["story_day"]:
                    label = f"[{ev['day_key']} / {ev['story_day']}]"
                seq_lines.append(f"{label} {ev['text']}")
        if seq_lines:
            parts.append("TIMELINE (oldest to newest):\n" + "\n".join(seq_lines))
        section = "\n".join(parts)
        if len(section) > max_chars:
            # Keep flags + arcs whole (they are short and load-bearing);
            # trim the sequence from the oldest end.
            head, _, seq_text = section.partition("TIMELINE (oldest to newest):\n")
            if seq_text:
                keep = max_chars - len(head) - len("TIMELINE (oldest to newest):\n") - 3
                section = head + "TIMELINE (oldest to newest):\n" + ("..." + seq_text[-keep:] if keep > 0 else seq_text[:max_chars])
                section = section[:max_chars]
            else:
                section = section[:max_chars]
        return section


def check_beat(story_id: str, beat_text: str, ident=None, identity_hash=None) -> dict:
    """Module-level convenience: TemporalMemory(story_id, ...).check_beat."""
    return TemporalMemory(story_id, ident=ident, identity_hash=identity_hash).check_beat(beat_text)


def check_prose(story_id: str, prose_text: str, ident=None, identity_hash=None) -> dict:
    """Module-level convenience: TemporalMemory(story_id, ...).check_prose."""
    return TemporalMemory(story_id, ident=ident, identity_hash=identity_hash).check_prose(prose_text)


# --- Gate heuristic helpers (module level, unit-testable) ----------------

def _split_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip(" \t\"'“”") for p in parts if p.strip()]


def _tokens(sentence):
    return re.findall(r"[A-Za-z']+", sentence or "")


_GATE_SPEECH_VERBS = {
    "says", "said", "asks", "asked", "replies", "replied", "whispers", "whispered",
    "shouts", "shouted", "argues", "argued", "laughs", "laughed", "cries", "cried",
    "speaks", "spoke", "tells", "told", "calls", "called", "answers", "answered",
    "mutters", "muttered", "screams", "screamed", "exclaims", "exclaimed",
    "pleads", "pleaded", "begs", "begged", "warns", "warned", "demands", "demanded",
}
_GATE_ACTION_VERBS = {
    "walks", "walked", "runs", "ran", "sits", "sat", "stands", "stood", "opens", "opened",
    "closes", "closed", "looks", "looked", "turns", "turned", "reaches", "reached",
    "grabs", "grabbed", "takes", "took", "enters", "entered", "leaves", "left",
    "watches", "watched", "waits", "waited", "smiles", "smiled", "nods", "nodded",
    "shakes", "shook", "follows", "followed", "leads", "led", "helps", "helped",
    "fights", "fought", "hides", "hid", "listens", "listened", "moves", "moved",
    "arrives", "arrived", "returns", "returned", "steps", "stepped",
    "picks", "picked", "holds", "held", "carries", "carried", "pushes", "pushed",
    "pulls", "pulled", "writes", "wrote", "reads", "sleeps", "slept",
    "wakes", "woke", "eats", "ate", "drinks", "drank",
}
_GATE_INTERACTION_VERBS = {
    "talks", "talked", "speaks", "spoke", "looks", "looked", "sees", "saw",
    "finds", "found", "meets", "met", "joins", "joined", "follows", "followed",
    "calls", "called", "tells", "told", "asks", "asked", "helps", "helped",
    "watches", "watched", "greets", "greeted", "faces", "faced",
}
_POSSESSION_VERBS = {
    "takes", "took", "take", "holds", "hold", "held", "has", "have",
    "carries", "carried", "carry", "picks", "picked", "grabs", "grabbed",
    "steals", "stole", "gets", "got",
}
_MOVEMENT_VERBS = {
    "arrives", "arrived", "leaves", "left", "travels", "travelled", "traveled",
    "returns", "returned", "moves", "moved", "enters", "entered", "heads", "headed",
    "goes", "went", "comes", "came",
}
_ARTICLE_WORDS = {"the", "a", "an", "his", "her", "their", "its"}
_STOP_WORDS = {"and", "but", "with", "from", "to", "in", "on", "at", "before",
               "after", "while", "because", "as", "for", "of", "the"}


def _subject_before(words, idx):
    """Last capitalized name (up to two adjacent words) before token idx."""
    j = idx - 1
    parts = []
    while j >= 0 and len(parts) < 2 and words[j][:1].isupper():
        parts.append(words[j])
        j -= 1
    return " ".join(reversed(parts)) if parts else ""


def _noun_phrase_after(words, start, max_words=3):
    """Lowercase noun phrase after a verb: skip articles, take up to
    max_words content words, stop at stopwords. '' when none."""
    out = []
    i = start
    while i < len(words) and words[i].lower() in _ARTICLE_WORDS:
        i += 1
    while i < len(words) and len(out) < max_words:
        lw = words[i].lower()
        if lw in _STOP_WORDS:
            break
        out.append(lw)
        i += 1
    return " ".join(out)


def _item_after_verb(line: str, verb_end: int):
    """Item noun phrase after a verb position in a recorded line."""
    tail = line[verb_end:]
    words = _tokens(tail)
    return _noun_phrase_after(words, 0)


def _sentence_is_exempt_mention(sentence: str, entity: str) -> bool:
    """True when the sentence's mention of the entity is mention-only
    for resolved-thread purposes: the entity follows a memory verb
    ("Jake remembers the signal"), or every mention is possessive
    ("the signal's frequency")."""
    ent = re.escape(entity)
    if re.search(rf"(?:remembers?|remembering|remembered|thought of|thinking of|dreamed of|dreamt of|dreaming of|misses|missed|missing|mourns?|mourned|mourning|recalls?|recalled|recalling|haunted by|memories of|memory of)\s+(the\s+)?{ent}\b",
                 sentence, re.I):
        return True
    # Possessive-only: strip "{entity}'s" and see if any bare mention remains.
    bare = re.sub(rf"\b{ent}'s\b", "", sentence, flags=re.I)
    return not _mention_in(bare, entity)


def _sentence_is_framed(sentence: str) -> bool:
    """True when this sentence is explicitly framed as not-present:
    'Flashback:', 'In a dream, ...', 'dream sequence', 'recording'."""
    return re.search(r"\b(flashback|in a dream|dream sequence|recording)\b", sentence, re.I) is not None


def _beat_is_framed(text: str) -> bool:
    """True when the whole beat opens with an explicit frame marker
    ('Flashback: ...', 'In a dream ...', 'Dream sequence ...'). A
    marker buried mid-beat does NOT frame the whole beat — framing is
    sentence-local or beat-opening, never word-soup."""
    head = (text or "").strip()[:80]
    if re.search(r"\b(flashback|dream|memory|recording)\b\s*[:\-—]", head, re.I):
        return True
    return re.match(r"(?i)\s*(flashback to|in a dream|dream sequence)\b", text or "") is not None


def classify_mention(sentence: str, entity: str) -> str:
    """'staged' if the entity is depicted acting/speaking in the
    present in this sentence; 'mention' otherwise. Token-based: the
    entity must appear as bare tokens, so possessives ("Lena's key")
    never match. Ambiguous sentences default to 'mention' — a false
    block stops legitimate writing, while a miss is caught later by
    the output check or the human. Pronouns are invisible here (see
    _check_text limits)."""
    ent = re.escape(entity)
    if not _mention_in(sentence, entity):
        return "mention"
    # Death restatement, not staging: "Lena died", "Lena is dead".
    if re.search(rf"\b{ent}\s+(dies|died|is dead|was killed|was dead|had died)\b", sentence, re.I):
        return "mention"
    if re.search(rf"\bdeath of {ent}\b", sentence, re.I):
        return "mention"
    # Memory frame: entity as object of a remembering verb.
    if re.search(rf"(?:remembers?|remembering|remembered|thought of|thinking of|dreamed of|dreamt of|dreaming of|misses|missed|missing|mourns?|mourned|mourning|recalls?|recalled|recalling|haunted by|memories of|memory of)\s+(the\s+)?{ent}\b",
                 sentence, re.I):
        return "mention"
    words = _tokens(sentence)
    ent_words = [w.lower() for w in _tokens(entity)]
    staged_verbs = _GATE_SPEECH_VERBS | _GATE_ACTION_VERBS
    for i in range(len(words) - len(ent_words) + 1):
        if [w.lower() for w in words[i:i + len(ent_words)]] != ent_words:
            continue
        # Subject position: verb within the next 4 tokens.
        for j in range(i + len(ent_words), min(len(words), i + len(ent_words) + 4)):
            if words[j].lower() in staged_verbs:
                return "staged"
        # Copula-present: "Lena is here/back/alive/waiting".
        for j in range(i + len(ent_words), min(len(words), i + len(ent_words) + 3)):
            if words[j].lower() == "is" and j + 1 < len(words) and words[j + 1].lower() in {
                    "here", "there", "back", "alive", "waiting", "standing", "sitting", "watching"}:
                return "staged"
        # Object position: interaction verb within the previous 4 tokens.
        for j in range(max(0, i - 4), i):
            if words[j].lower() in _GATE_INTERACTION_VERBS:
                return "staged"
    return "mention"
