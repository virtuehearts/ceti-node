"""CETI chat: the CETI answer ladder as a test layer in the node.

Tier order, first hit wins:
  1. facts — the fact store (facts.sqlite3) plus cassette <fact>
     elements (this is where the 195-country capitals live).
  2. aiml — cassette <category> pattern match (exact, plus the
     single `*` wildcard with <star/> substitution).
  3. local-model — vector memory as context, then the local chat
     server. Nothing here is an answer by itself: memory is
     context for the model, the way the CETI ladder runs it.
  4. cloud-suggested — NOT called. Every local tier missed, so
     the answer says so and suggests cloud. Nothing leaves this
     machine from this path.

Read-only by design, except the local-model call: CETI chat never
writes to vector memory (a test layer must not contaminate the
tier it tests), never writes to the brain stores, and never calls
a cloud API. Every answer carries its tier + evidence, so the
ladder can be verified tier by tier.
"""
import json
import os
import re
import sqlite3
import time

from . import brain

_READ_CAP = 1_000_000
_TAG_RE = re.compile(r"<[^>]+>")
_FACT_RE = re.compile(
    r"<fact\s+subject=\"([^\"]*)\"\s+predicate=\"([^\"]*)\"\s+object=\"([^\"]*)\"\s*/>",
    re.IGNORECASE)
_CAT_RE = re.compile(
    r"<category>\s*<pattern>(.*?)</pattern>\s*<template>(.*?)</template>\s*</category>",
    re.DOTALL | re.IGNORECASE)
_STAR_RE = re.compile(r"<star(?:\s+index=\"(\d+)\")?\s*/>", re.IGNORECASE)


def _norm(text):
    """Uppercase, punctuation to spaces, collapse whitespace. `*` survives."""
    text = text.upper()
    text = re.sub(r"[^A-Z0-9* ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _words(text):
    return _norm(text).split() if _norm(text) else []


def _iter_cassettes(roots):
    cdir = (roots or {}).get("cassettes_dir")
    if not cdir or not os.path.isdir(cdir):
        return
    for dirpath, _dirnames, filenames in os.walk(cdir):
        for fn in sorted(filenames):
            if not fn.endswith(".aiml"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    yield os.path.relpath(full, cdir).replace(os.sep, "/"), f.read(_READ_CAP)
            except OSError:
                continue


def _db_facts(roots):
    path = (roots or {}).get("facts_db")
    if not path or not os.path.exists(path):
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        rows = conn.execute(
            "SELECT subject, predicate, object FROM facts "
            "WHERE superseded = 0 OR superseded IS NULL").fetchall()
        conn.close()
        return [(s, p, o) for s, p, o in rows if s and p and o]
    except sqlite3.Error:
        return []


def _fact_hit(query_words, subject):
    """A fact fires when every word of its subject appears in the query."""
    sw = _words(subject)
    return bool(sw) and all(w in query_words for w in sw)


def _facts_tier(query, roots):
    qw = _words(query)
    for subj, pred, obj in _db_facts(roots):
        if _fact_hit(qw, subj):
            return {"tier": "facts",
                    "answer": "%s — %s: %s" % (subj, pred, obj),
                    "evidence": "fact store: %s %s %s" % (subj, pred, obj)}
    for name, text in _iter_cassettes(roots):
        for subj, pred, obj in _FACT_RE.findall(text):
            if _fact_hit(qw, subj):
                return {"tier": "facts",
                        "answer": "%s — %s: %s" % (subj, pred, obj),
                        "evidence": "cassette %s fact: %s %s %s" % (name, subj, pred, obj)}
    return None


def _pattern_match(pattern, query_norm):
    """Exact match, or `*` wildcard match returning captured groups."""
    p = _norm(pattern)
    if not p:
        return None
    if "*" not in p:
        return [] if p == query_norm else None
    parts = [_norm(seg) for seg in p.split("*")]
    regex = "^" + "(.*)".join(re.escape(seg) for seg in parts) + "$"
    m = re.match(regex, query_norm)
    if not m:
        return None
    return [g.strip() for g in m.groups()]


def _render_template(template, stars):
    def star_sub(m):
        idx = int(m.group(1) or 1) - 1
        return stars[idx] if 0 <= idx < len(stars) else ""
    text = _STAR_RE.sub(star_sub, template)
    return _TAG_RE.sub("", text).strip()


def _aiml_tier(query, roots):
    qn = _norm(query)
    for name, text in _iter_cassettes(roots):
        for pattern, template in _CAT_RE.findall(text):
            stars = _pattern_match(pattern, qn)
            if stars is None:
                continue
            return {"tier": "aiml",
                    "answer": _render_template(template, stars),
                    "evidence": "cassette %s pattern: %s" % (name, _norm(pattern))}
    return None


def _memory_hits(query, ident_hash):
    """Vector memory hits for context. Fail-soft: [] when unavailable."""
    if not ident_hash:
        return []
    try:
        from .config import data_dir
        from .servers import embed_text
        from . import memory
        emb = embed_text(query)
        if emb is None:
            return []
        conn = memory.connect(data_dir())
        hits = memory.search(conn, ident_hash, emb, top_k=3)
        return [{"id": rid, "text": t, "source": s, "model": mdl,
                 "score": round(sc, 3)}
                for sc, t, s, rid, mdl in hits if sc > 0.5]
    except Exception:
        return []


def _local_model(messages):
    from .servers import chat_complete
    answer, _reasoning = chat_complete(messages)
    return answer


def answer(query, roots=None, ident_hash=None):
    """Run the ladder. Returns tier, answer, evidence, cloud_suggested,
    thinking (the ladder trace, shown in the chat thinking panel),
    and memories (local-model tier only)."""
    if roots is None:
        roots = brain.resolve_roots()
    query = (query or "").strip()
    if not query:
        return {"tier": "empty", "answer": "(empty question)",
                "evidence": "", "cloud_suggested": False, "thinking": ""}
    steps = []
    hit = _facts_tier(query, roots)
    if hit:
        steps.append("FACTS — hit: " + hit["evidence"])
        hit["cloud_suggested"] = False
        hit["thinking"] = "\n".join(steps)
        return hit
    steps.append("FACTS — miss: no fact subject matched the question")
    hit = _aiml_tier(query, roots)
    if hit:
        steps.append("AIML — hit: " + hit["evidence"])
        hit["cloud_suggested"] = False
        hit["thinking"] = "\n".join(steps)
        return hit
    steps.append("AIML — miss: no category pattern matched")
    memories = _memory_hits(query, ident_hash)
    if memories:
        steps.append("VECTOR MEMORY — %d node(s) in context: %s" % (
            len(memories),
            ", ".join("#%s" % m["id"] for m in memories)))
    else:
        steps.append("VECTOR MEMORY — no nodes retrieved")
    messages = []
    if memories:
        ctx = ("Relevant local knowledge (use it if it helps, ignore it "
               "if not):\n" + "\n".join("- " + m["text"] for m in memories))
        messages.append({"role": "system", "content": ctx})
    messages.append({"role": "user", "content": query})
    try:
        text = _local_model(messages)
    except Exception as e:
        text = None
        model_error = str(e)
    if text:
        steps.append("LOCAL MODEL — answered (chat server)")
        return {"tier": "local-model", "answer": text,
                "evidence": ("memories used: %d" % len(memories)) if memories
                            else "no memory hits; local model alone",
                "memories": memories, "cloud_suggested": False,
                "thinking": "\n".join(steps)}
    steps.append("LOCAL MODEL — unavailable: %s" % model_error)
    steps.append("CLOUD — suggested only, never called")
    return {"tier": "cloud-suggested",
            "answer": ("Local tiers missed: no fact, no AIML pattern, and "
                       "the local model is unavailable (%s). Cloud "
                       "suggestion: retry this question in a regular chat "
                       "session with a cloud provider selected — the CETI "
                       "layer itself never calls a cloud API." % model_error),
            "evidence": "facts: miss · aiml: miss · local-model: unavailable",
            "cloud_suggested": True,
            "thinking": "\n".join(steps)}


def facts_search(query, roots=None):
    """Fact-engine tester: every fact whose subject words appear in the
    question, from the fact store and from cassette <fact> elements."""
    if roots is None:
        roots = brain.resolve_roots()
    qw = _words(query)
    matches = []
    for subj, pred, obj in _db_facts(roots):
        if _fact_hit(qw, subj):
            matches.append({"subject": subj, "predicate": pred,
                            "object": obj, "source": "fact store"})
    for name, text in _iter_cassettes(roots):
        for subj, pred, obj in _FACT_RE.findall(text):
            if _fact_hit(qw, subj):
                matches.append({"subject": subj, "predicate": pred,
                                "object": obj, "source": name})
    return {"query": query, "matches": matches}


_SUGGESTIONS_FILE = "ceti-suggestions.json"
_SUGGESTIONS_CAP = 50


def _suggestions_path(data_dir):
    return os.path.join(data_dir, _SUGGESTIONS_FILE)


def read_suggestions(data_dir):
    """Missed CETI questions awaiting research, newest first."""
    try:
        with open(_suggestions_path(data_dir), encoding="utf-8") as f:
            items = json.load(f)
        return [i for i in items if isinstance(i, dict) and i.get("query")]
    except (OSError, ValueError):
        return []


def record_miss(data_dir, query):
    """Record a cloud-suggested miss as a research suggestion.

    Operator-local JSON in the node's own data dir — not vector
    memory, so it never feeds back into the ladder's memory tier.
    Deduped by question, newest first, capped.
    """
    items = [i for i in read_suggestions(data_dir)
             if i["query"] != query]
    items.insert(0, {"query": query, "created": time.time()})
    items = items[:_SUGGESTIONS_CAP]
    try:
        with open(_suggestions_path(data_dir), "w", encoding="utf-8") as f:
            json.dump(items, f)
    except OSError:
        pass
    return items
