"""CETI local reasoning layer — wired in as the CETI chat ladder.

`ceti_chat.answer()` delegates here. Unified-memory design (it reuses
`ceti_chat`'s fact/AIML helpers so the two cannot drift apart):

  All-layers-reason (2026-11-15, Jero's call): every layer outputs
  what it found, and the local model constructs the answer from all
  of it. No layer returns its hit directly any more — the model
  always reasons over the assembled knowledge. The direct answers
  survive only as the fallback when the model is down.

  Tier 0  MATH      — deterministic Python arithmetic (safe AST parser,
                      no eval of arbitrary code). Numbers are exact.
                      The result is a context fact for the model.
  Tier 1  FACTS     — all matching facts, predicate-aware, plus
                      two-hop COMPOSITION ("capital of the country
                      whose currency is yen" -> yen is Japan's
                      currency -> capital Tokyo). Competing facts
                      are all fed to the model, not arbitrated
                      separately — composition IS the arbitration.
  Tier 2  AIML      — every matching cassette pattern, as context.
  Tier 3  LEXICON   — synonym/definition expansion BEFORE vector
                      search, so "dial-up forums" also searches
                      "bulletin board system". Built-in seed from the
                      cassette vocabulary + a loader for a real
                      WordNet-style file (see load_lexicon).
  Tier 4  VECTOR    — embed the EXPANDED query, search vector memory.
  Tier 5  CONSTRUCT — the local model builds the answer from ALL the
                      assembled context (math + facts + AIML +
                      lexicon + vector hits) under a strict
                      "answer only from this context" prompt, plus
                      a numbers-grounded guard: every number in the
                      answer must appear in the context or the
                      question. A refusal goes to Tier 5b.
  Tier 5b UNGROUNDED — when Tier 5 ran and the model refused
                      ("I don't know from local knowledge"): the
                      loaded local model is asked again from its own
                      knowledge, and the answer is labelled
                      UNGROUNDED in tier and evidence. Never runs
                      when no grounded context existed (the model
                      is never called on an empty context), and
                      never recorded into vector memory.
  Tier 6  CLOUD     — suggested only, never called. Reached when no
                      local tier produced anything AND there was no
                      local context worth handing to the model (the
                      model is not called on an empty context: that
                      is just paying a 1.7B model to hallucinate).
                      Also the fallback when the model is down and
                      no direct hit exists: the best direct hit
                      (math, composition, fact, AIML) answers
                      instead of failing the question.

Stdlib only. Read-only except the local-model call (same as ceti_chat).
"""
import ast
import json
import os
import re
import time

from . import brain
from . import ceti_chat

# ---------------------------------------------------------------- math ---

_WORD_OPS = [
    (re.compile(r"\bdivided\s+by\b", re.I), "/"),
    (re.compile(r"\bmultiplied\s+by\b", re.I), "*"),
    (re.compile(r"\bplus\b", re.I), "+"),
    (re.compile(r"\bminus\b", re.I), "-"),
    (re.compile(r"\btimes\b", re.I), "*"),
]
_EXPR_RE = re.compile(r"[\d\.\s\+\-\*\/\%\(\)]{3,}")
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*of\s+(\d+(?:\.\d+)?)", re.I)


def _fmt_num(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return ("%.6f" % value).rstrip("0").rstrip(".")
    return str(value)


def _safe_eval(expr):
    """Evaluate + - * / % ( ) over numbers only. No names, no calls,
    no attribute access, no power — an AST whitelist, not eval()."""
    tree = ast.parse(expr, mode="eval")
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod,
               ast.USub, ast.UAdd)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError("unsupported syntax: %s" % type(node).__name__)
        if isinstance(node, ast.Constant) and not isinstance(
                node.value, (int, float)):
            raise ValueError("non-numeric constant")
    code = compile(tree, "<math>", "eval")
    return eval(code, {"__builtins__": {}}, {})


def _math_tier(query):
    """Return a math result dict, or None when this is not a math Q."""
    m = _PCT_RE.search(query)
    if m:
        pct, base = float(m.group(1)), float(m.group(2))
        value = pct / 100.0 * base
        shown = "%s%% of %s = %s" % (_fmt_num(pct), _fmt_num(base),
                                     _fmt_num(value))
        return {"tier": "math", "answer": _fmt_num(value),
                "evidence": "python: " + shown,
                "calculation": shown}
    text = query
    for pat, rep in _WORD_OPS:
        text = pat.sub(" %s " % rep, text)
    text = text.replace("×", "*").replace("÷", "/")
    # Letter x/X as a multiplication sign, only between digits
    # ("17x23", "17 x 23"). A bare replace would mangle words
    # containing x ("box", "six"), so the digits are the guard.
    text = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", " * ", text)
    for cand in _EXPR_RE.findall(text):
        cand = cand.strip(" .")
        if not re.search(r"\d", cand) or not re.search(r"[\+\-\*/%]", cand):
            continue
        if len(re.findall(r"\d+(?:\.\d+)?", cand)) < 2:
            continue
        if not re.fullmatch(r"[\d\.\s\+\-\*\/\%\(\)]+", cand):
            continue
        try:
            value = _safe_eval(cand)
        except Exception:
            continue
        shown = "%s = %s" % (re.sub(r"\s+", " ", cand).strip(),
                             _fmt_num(value))
        return {"tier": "math", "answer": _fmt_num(value),
                "evidence": "python: " + shown,
                "calculation": shown}
    return None


# --------------------------------------------------------------- facts ---

# Predicate words in a question -> fact predicate. Order matters:
# "currency_code"/"code" must be checked before bare "currency".
_PREDICATE_WORDS = [
    (("currency_code", "currency code"), "currency_code"),
    (("capital",), "capital"),
    (("currency", "money"), "currency"),
    (("population", "how many people", "how many inhabitants"), "population"),
    (("language", "languages"), "languages"),
    (("region", "continent"), "region"),
    (("iso_alpha3", "iso alpha3"), "iso_alpha3"),
    (("iso_alpha2", "iso alpha2"), "iso_alpha2"),
    (("code",), "currency_code"),
]

# "capital of the country whose currency is the yen"
# "currency of the country whose capital is Paris"
_COMPOSE_RE = re.compile(
    r"(capital|currency|population|language|region)\s+of\s+"
    r"(?:the\s+)?country\s+whose\s+"
    r"(capital|currency|population|language|region)\s+is\s+"
    r"(?:the\s+)?(.+?)\s*[?.!]*$",
    re.IGNORECASE)
_COMPOSE_RE2 = re.compile(
    r"(capital|currency|population|language|region)\s+of\s+"
    r"(?:the\s+)?country\s+(?:with|that\s+has)\s+"
    r"(?:a\s+)?(capital|currency|population|language|region)\s+"
    r"(?:of\s+|is\s+)?(.+?)\s*[?.!]*$",
    re.IGNORECASE)


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ]+", " ", text or "")
                  ).strip().lower()


def facts_search_all(query, roots=None):
    """Every fact whose subject words appear in the question (same
    matcher as ceti_chat.facts_search, returned as a plain list)."""
    if roots is None:
        roots = brain.resolve_roots()
    return ceti_chat.facts_search(query, roots=roots)["matches"]


def _detect_predicate(query):
    qn = " " + _norm(query) + " "
    for words, pred in _PREDICATE_WORDS:
        for w in words:
            if " " + w + " " in qn or qn.strip().endswith(" " + w) \
                    or (" " + w + " ") in qn:
                return pred
    return None


def _facts_tier(query, roots=None):
    """Predicate-aware facts tier. Returns (result, trace_lines,
    candidates).

    result is the clear-hit answer (or None). candidates holds the
    facts to arbitrate over when the hit is ambiguous — several facts
    share the detected predicate, or no predicate was detected and
    several subject matches compete. It is empty on a clear hit and
    on a miss, so the ladder can tell "return directly" from
    "arbitrate" from "continue"."""
    trace = []
    matches = facts_search_all(query, roots)
    if not matches:
        trace.append("FACTS — miss: no fact subject matched the question")
        return None, trace, []
    trace.append("FACTS — %d fact(s) matched the subject: %s" % (
        len(matches),
        ", ".join("%s %s %s" % (m["subject"], m["predicate"], m["object"])
                  for m in matches)))
    pred = _detect_predicate(query)
    if pred:
        wanted = [m for m in matches if m["predicate"] == pred]
        if len(wanted) == 1:
            m = wanted[0]
            trace.append("FACTS — predicate %r matched: %s %s %s" % (
                pred, m["subject"], m["predicate"], m["object"]))
            return {"tier": "facts",
                    "answer": "%s — %s: %s" % (m["subject"], m["predicate"],
                                               m["object"]),
                    "evidence": "fact: %s %s %s (source: %s)" % (
                        m["subject"], m["predicate"], m["object"],
                        m["source"]),
                    "facts": matches, "predicate_matched": True}, trace, []
        if wanted:
            trace.append("FACTS — %d fact(s) share predicate %r; "
                         "arbitration needed" % (len(wanted), pred))
            return None, trace, wanted
        trace.append("FACTS — subject matched but no %r fact exists; "
                     "refusing the first unrelated fact" % pred)
        return None, trace, []
    if len(matches) == 1:
        trace.append("FACTS — no predicate word in the question; first "
                     "subject match used (ambiguous — ask for a predicate)")
        m = matches[0]
        return {"tier": "facts",
                "answer": "%s — %s: %s" % (m["subject"], m["predicate"],
                                           m["object"]),
                "evidence": "fact: %s %s %s (source: %s)" % (
                    m["subject"], m["predicate"], m["object"], m["source"]),
                "facts": matches, "predicate_matched": False}, trace, []
    trace.append("FACTS — no predicate word in the question; %d subject "
                 "matches compete, arbitration needed" % len(matches))
    return None, trace, matches


def _composition_tier(query, roots=None):
    """Two-hop facts: identify a country by one fact, answer with
    another fact about that country. Returns (result, trace_lines)."""
    trace = []
    m = _COMPOSE_RE.search(query) or _COMPOSE_RE2.search(query)
    if not m:
        return None, trace
    target_pred, sel_pred, sel_value = (m.group(1).lower(),
                                        m.group(2).lower(),
                                        _norm(m.group(3)))
    if target_pred == "language":
        target_pred = "languages"
    if sel_pred == "language":
        sel_pred = "languages"
    trace.append("COMPOSITION — target %r of country whose %r is %r" % (
        target_pred, sel_pred, sel_value))
    if roots is None:
        roots = brain.resolve_roots()
    all_facts = []
    for subj, pred, obj in ceti_chat._db_facts(roots):
        all_facts.append({"subject": subj, "predicate": pred,
                          "object": obj, "source": "fact store"})
    for _name, text in ceti_chat._iter_cassettes(roots):
        for subj, pred, obj in ceti_chat._FACT_RE.findall(text):
            all_facts.append({"subject": subj, "predicate": pred,
                              "object": obj, "source": _name})
    sel_words = [w for w in sel_value.split() if w not in ("the", "a")]
    hop1 = None
    for f in all_facts:
        if f["predicate"] != sel_pred:
            continue
        obj_words = _norm(f["object"]).split()
        if sel_words and all(w in obj_words or w in _norm(f["object"])
                             for w in sel_words):
            hop1 = f
            break
    if not hop1:
        trace.append("COMPOSITION — hop 1 failed: no country with %s = %s"
                     % (sel_pred, sel_value))
        return None, trace
    country = hop1["subject"]
    trace.append("COMPOSITION — hop 1: %s %s %s -> country is %s" % (
        country, sel_pred, hop1["object"], country))
    hop2 = next((f for f in all_facts
                 if f["subject"] == country
                 and f["predicate"] == target_pred), None)
    if not hop2:
        trace.append("COMPOSITION — hop 2 failed: no %s fact for %s"
                     % (target_pred, country))
        return None, trace
    trace.append("COMPOSITION — hop 2: %s %s %s" % (
        country, target_pred, hop2["object"]))
    return {"tier": "facts-composition",
            "answer": "%s — %s: %s" % (country, target_pred, hop2["object"]),
            "evidence": ("hop 1: %s %s %s; hop 2: %s %s %s"
                         % (country, sel_pred, hop1["object"],
                            country, target_pred, hop2["object"])),
            "facts": [hop1, hop2]}, trace


# ------------------------------------------------------------- lexicon ---

# Built-in seed, from the cassette vocabulary itself. Definitions are
# grounded in cassette templates (first BBS, FidoNet, 56k, C64 date).
# This seed is the fallback; a real WordNet file loaded with
# load_lexicon() overrides/adds to it.
LEXICON_SEED = {
    "bbs": {
        "synonyms": ["bulletin board system", "bulletin board",
                     "dial-up forum", "message board"],
        "definition": ("A bulletin board system: a computer you dialled "
                       "into over a phone line to read messages, download "
                       "files and play door games. The first BBS, CBBS, "
                       "launched 16 February 1978 in Chicago.")},
    "bulletin board system": {
        "synonyms": ["bbs", "bulletin board", "dial-up forum"],
        "definition": ("A bulletin board system (BBS): a computer you "
                       "dialled into over a phone line to read messages, "
                       "download files and play door games.")},
    "forum": {
        "synonyms": ["bulletin board", "bbs", "message board",
                     "discussion board"],
        "definition": ("An online place where people post messages for "
                       "others to read and reply to; on dial-up systems "
                       "this was a BBS.")},
    "dial-up": {
        "synonyms": ["dialup", "modem", "dial-up modem"],
        "definition": ("Connecting over an ordinary phone line with a "
                       "modem. The fastest common dial-up speed was 56k, "
                       "and connections made a distinctive chirping "
                       "sound.")},
    "dialup": {
        "synonyms": ["dial-up", "modem"],
        "definition": ("Connecting over an ordinary phone line with a "
                       "modem; 56k was the fastest common dial-up "
                       "speed.")},
    "modem": {
        "synonyms": ["dial-up modem", "dialup"],
        "definition": ("A device that connects a computer to a phone line "
                       "so it can call another computer. 2400 baud and "
                       "56k were common speeds.")},
    "sysop": {
        "synonyms": ["system operator", "operator", "board operator"],
        "definition": ("The system operator: the person who ran a BBS — "
                       "its owner, moderator and keeper of the rules.")},
    "fidonet": {
        "synonyms": ["fido net", "fido"],
        "definition": ("FidoNet: the global network that linked BBSes so "
                       "messages and files could travel between boards.")},
    "commodore 64": {
        "synonyms": ["c64", "commodore"],
        "definition": ("The Commodore 64, introduced January 1982 by "
                       "Commodore International.")},
    "baud": {
        "synonyms": [],
        "definition": ("Baud: the speed of a modem connection. 2400 baud "
                       "was a common early dial-up speed.")},
    "door game": {
        "synonyms": ["door games", "door"],
        "definition": ("A game played on a BBS, such as TradeWars and "
                       "Legend of the Red Dragon.")},
}


# Stopwords for the empty-context guard: a vector hit only counts as
# grounded context when it shares a real content word with the question
# (or with the question's lexicon synonyms).
_STOPWORDS = frozenset(
    "the a an is are was were of for to in on at by with what who when "
    "where why how did do does".split())

_LEXICON_CACHE = {}


def _content_words(text):
    """Lowercased words longer than 2 chars, minus stopwords."""
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOPWORDS}


def _query_want_words(query):
    """Words to look up in a real lexicon: content words plus their
    singular forms ("forums" -> "forum"), so a plural question still
    finds the singular WordNet lemma."""
    words = _content_words(query)
    want = set(words)
    for w in words:
        if w.endswith("s") and len(w) > 3 and not w.endswith("ss"):
            want.add(w[:-1])
    return want


def get_lexicon(roots, want=None):
    """Cached lazy loader for the shipped lexicon.

    Looks for a WordNet WNDB directory at
    <ceti_dir>/lexicon/wordnet/ (or its dict/ subdir) and parses it
    with load_lexicon(), limited to the `want` lemmas when given, so a
    query-time load stays small. Returns {} when no lexicon dir exists
    — callers then run on the built-in seed alone. Results are cached
    per (directory, want-set); a missing dir is NOT cached, so a
    lexicon dropped in later is picked up.
    """
    ceti_dir = (roots or {}).get("ceti_dir")
    if not ceti_dir:
        return {}
    for cand in (os.path.join(ceti_dir, "lexicon", "wordnet"),
                 os.path.join(ceti_dir, "lexicon", "wordnet", "dict")):
        if os.path.isfile(os.path.join(cand, "index.noun")):
            key = (cand, frozenset(w.lower() for w in want)
                   if want else None)
            if key not in _LEXICON_CACHE:
                _LEXICON_CACHE[key] = load_lexicon(cand, want=want)
            return _LEXICON_CACHE[key]
    return {}


def load_lexicon(path, want=None):
    """Load a WordNet-style lexicon file into {word: {synonyms, definition}}.

    Supported, stdlib-only, no downloads here:
      * a directory holding WordNet WNDB files (index.noun/data.noun,
        index.verb/data.verb, ...): real synsets + glosses. `want`
        (a set of lowercase words) limits the load to those lemmas.
      * Moby Thesaurus mthesaur.txt: one CSV line per root word,
        first field is the root, the rest are synonyms.
      * JSON: {"word": {"synonyms": [...], "definition": "..."}}.
      * TSV: word<TAB>synonym, synonym<TAB>definition  (one per line).
    Returns {} when the path is missing or unrecognised. File entries
    override the built-in seed when merged by the caller.
    """
    if not path or not os.path.exists(path):
        return {}
    if os.path.isdir(path):
        return _load_wndb(path, want)
    if path.endswith(".json"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            out = {}
            for word, entry in (data or {}).items():
                if isinstance(entry, dict):
                    out[word.lower()] = {
                        "synonyms": [str(s) for s in
                                     entry.get("synonyms", [])],
                        "definition": str(entry.get("definition", ""))}
            return out
        except (OSError, ValueError):
            return {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
            f.seek(0)
            if "\t" in head:
                out = {}
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 2 and parts[0].strip():
                        out[parts[0].strip().lower()] = {
                            "synonyms": [s.strip() for s in
                                         parts[1].split(",") if s.strip()],
                            "definition": parts[2].strip()
                            if len(parts) > 2 else ""}
                return out
            out = {}  # Moby: comma-separated, root first, many synonyms
            for line in f:
                fields = [x.strip() for x in line.split(",") if x.strip()]
                if len(fields) >= 3:
                    out[fields[0].lower()] = {
                        "synonyms": fields[1:21],
                        "definition": ""}
            return out
    except OSError:
        return {}


def _load_wndb(directory, want=None):
    """Minimal WordNet DB reader: index.<pos> -> synset offsets,
    data.<pos> -> the words in each synset + its gloss. Only the
    lemmas in `want` are kept when `want` is given, so a query-time
    load stays small."""
    out = {}
    want = {w.lower() for w in want} if want else None
    for pos in ("noun", "verb", "adj", "adv"):
        idx_path = os.path.join(directory, "index." + pos)
        data_path = os.path.join(directory, "data." + pos)
        if not (os.path.isfile(idx_path) and os.path.isfile(data_path)):
            continue
        offsets_by_lemma = {}
        try:
            with open(idx_path, encoding="utf-8",
                      errors="replace") as f:
                for line in f:
                    if line.startswith("  ") or not line.strip():
                        continue
                    parts = line.split()
                    if len(parts) < 7:
                        continue
                    lemma = parts[0].replace("_", " ").lower()
                    if want is not None and lemma not in want:
                        continue
                    try:
                        n_syn = int(parts[2])
                    except ValueError:
                        continue
                    offsets_by_lemma[lemma] = parts[-n_syn:]
        except OSError:
            continue
        if not offsets_by_lemma:
            continue
        wanted_offsets = {o for offs in offsets_by_lemma.values()
                          for o in offs}
        synsets = {}
        try:
            with open(data_path, encoding="utf-8",
                      errors="replace") as f:
                for line in f:
                    if line.startswith("  ") or not line.strip():
                        continue
                    off = line.split(None, 1)[0]
                    if off not in wanted_offsets:
                        continue
                    body, _, gloss = line.partition("|")
                    toks = body.split()
                    try:
                        w_cnt = int(toks[3], 16)
                    except (IndexError, ValueError):
                        continue
                    words = [toks[4 + 2 * i].replace("_", " ")
                             for i in range(w_cnt)
                             if 4 + 2 * i < len(toks)]
                    synsets[off] = (words, gloss.strip())
        except OSError:
            continue
        for lemma, offs in offsets_by_lemma.items():
            syns, defs = [], []
            for off in offs:
                words, gloss = synsets.get(off, ([], ""))
                syns.extend(w for w in words if w.lower() != lemma)
                if gloss:
                    defs.append(gloss)
            entry = out.setdefault(lemma, {"synonyms": [], "definition": ""})
            entry["synonyms"] = list(dict.fromkeys(
                entry["synonyms"] + syns))[:10]
            if not entry["definition"] and defs:
                entry["definition"] = defs[0]
    return out


def expand_query(query, lexicon=None):
    """Expand a query with lexicon synonyms BEFORE vector search.
    Returns (expanded_query, expansions, definitions) where expansions
    maps each matched word to its synonyms and definitions lists the
    (word, definition) pairs found. Multi-word keys ("bulletin board
    system", "dial-up") are matched before single words."""
    lex = dict(LEXICON_SEED)
    if lexicon:
        for word, entry in lexicon.items():
            merged = dict(lex.get(word.lower(), {"synonyms": [],
                                                 "definition": ""}))
            merged["synonyms"] = list(dict.fromkeys(
                merged["synonyms"] + list(entry.get("synonyms", []))))[:10]
            # Definitions: the built-in seed wins when it has one — it
            # is domain-grounded in the cassettes ("forum" = a place
            # where people post messages, on dial-up a BBS), while a
            # general dictionary gloss ("a public meeting...") is too
            # generic for the strict only-from-context prompt to use.
            # The loaded file fills in definitions the seed lacks.
            merged["definition"] = (merged["definition"]
                                    or entry.get("definition", ""))
            lex[word.lower()] = merged
    ql = " " + _norm(query).replace("-", " ") + " "
    # Plural stemming for matching: "forums" should hit the "forum"
    # entry, "sysops" the "sysop" entry.
    _toks = ql.split()
    _sing = [t[:-1] for t in _toks
             if t.endswith("s") and len(t) > 3 and not t.endswith("ss")]
    if _sing:
        ql = ql + " ".join(_sing) + " "
    expansions, definitions = {}, []
    matched_spans = []
    for key in sorted(lex, key=len, reverse=True):
        knorm = " " + key.replace("-", " ") + " "
        if knorm.strip() and knorm in ql:
            entry = lex[key]
            expansions[key] = entry["synonyms"]
            if entry["definition"]:
                definitions.append((key, entry["definition"]))
            matched_spans.append(key)
    extra = []
    for key, syns in expansions.items():
        extra.extend(s for s in syns[:3] if s.lower() not in ql)
    expanded = query.strip()
    if extra:
        expanded = expanded + " | related: " + ", ".join(extra)
    return expanded, expansions, definitions


# -------------------------------------------------------------- vector ---

def _vector_tier(expanded_query, ident_hash):
    """Embed the EXPANDED query and search vector memory. Returns
    (hits, trace_lines) — the lines are a FRESH list; the caller
    extends its own trace with them (never extend a list with
    itself). Fail-soft: [] when unavailable."""
    hits = []
    lines = []
    if not ident_hash:
        lines.append("VECTOR MEMORY — skipped: no identity hash")
        return hits, lines
    try:
        from .config import data_dir
        from .servers import embed_text
        from . import memory
        emb = embed_text(expanded_query)
        if emb is None:
            lines.append("VECTOR MEMORY — embedding server unavailable")
            return hits, lines
        conn = memory.connect(data_dir())
        try:
            raw = memory.search(conn, ident_hash, emb, top_k=5)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        hits = [{"id": rid, "text": t, "source": s, "model": mdl,
                 "score": round(sc, 3)}
                for sc, t, s, rid, mdl in raw if sc > 0.35]
        if hits:
            lines.append("VECTOR MEMORY — %d node(s) above 0.35: %s" % (
                len(hits),
                ", ".join("#%s (%.2f)" % (h["id"], h["score"])
                          for h in hits)))
        else:
            best = round(raw[0][0], 3) if raw else None
            lines.append("VECTOR MEMORY — no nodes above 0.35 "
                         "(best score %s over %d row(s))" % (best, len(raw)))
    except Exception as e:
        lines.append("VECTOR MEMORY — error: %s" % e)
    return hits, lines


# Question scaffolding, not subject words: dropped from the
# retrieval query only (the vector filter keeps them).
_AIML_RETRIEVAL_STOP = frozenset({"about", "tell"})


def _attachment_hits(query, ident_hash, session_id, top_k=3):
    """Session-file context: embed the question and search ONLY this
    session's attachment nodes (source "attachment:<session>:<file>").
    Fail-soft: [] when unavailable, and a hit only counts when it
    shares a content word with the question (same bar as the vector
    filter — an embedding alone is not relevance)."""
    if not ident_hash or not session_id:
        return [], []
    try:
        from .config import data_dir
        from .servers import embed_text
        from . import memory
        emb = embed_text(query)
        if emb is None:
            return [], ["SESSION FILES — embedding server unavailable"]
        conn = memory.connect(data_dir())
        try:
            sources = memory.attachment_sources(conn, ident_hash, session_id)
            if not sources:
                return [], []
            raw = memory.search(conn, ident_hash, emb, top_k=top_k,
                                sources=sources)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        qw = _content_words(query)
        hits = [{"id": rid, "text": t, "source": s, "score": round(sc, 3)}
                for sc, t, s, rid, _m in raw
                if sc > 0.5 and _content_words(t) & qw]
        lines = []
        if hits:
            lines.append("SESSION FILES — %d attachment chunk(s) from this "
                         "session: %s" % (
                             len(hits),
                             ", ".join(h["source"] for h in hits)))
        return hits, lines
    except Exception as e:
        return [], ["SESSION FILES — error: %s" % e]


def _aiml_search(orig_words, roots, top_n=4):
    """AIML retrieval (not exact match): score every cassette
    category by content-word overlap with the question and return
    the top matches as grounded context for the local model.

    Pure Python set overlap — no embedding call, no server round
    trip: the whole step runs in milliseconds on CPU (budget
    250ms), and it works even when the embed server is down.
    Pattern words count double: a match in the pattern is a
    stronger signal than a match in the template body. A category
    needs score >= 4 (one pattern-word hit, or two template-word
    hits) — the same "share a content word" bar as the vector
    filter.
    Deliberately scored on the ORIGINAL question words only, NOT
    the lexicon synonyms: synonym expansion is too noisy for
    keyword retrieval. Twice it crowded the atomic facts out —
    first 'tell' -> 'state' let "CURRENT STATE OF ..." patterns
    score on the synonym, then 'about' -> 'around' matched the
    narrative "around 2014" inside long telnet templates, pushing
    "1984" out of the top 4 both times. Synonyms still feed the
    vector tier (their original purpose); retrieval trusts only
    what the user actually asked.
    Two guards keep the ranking honest: "about"/"tell" are
    question scaffolding, not subject words (nearly every "WHAT
    IS ... ABOUT ..." pattern contains "about", so they would let
    unrelated categories score on one generic word), and
    identical templates are de-duplicated after ranking (several
    patterns often share one summary — feeding it twice adds
    nothing). Among equal scores the most concise template wins:
    short templates are atomic facts ("1984", "Over 40,000"),
    exactly what grounds the model's numbers."""
    qwords = {w for w in orig_words if w not in _AIML_RETRIEVAL_STOP}
    if not qwords:
        return []
    scored = []
    for cassette, pattern, template in ceti_chat._iter_categories(roots):
        pw = _content_words(pattern)
        tw = _content_words(template)
        matched = (qwords & pw) | (qwords & tw)
        if not matched:
            continue
        score = 4 * len(qwords & pw) + 2 * len(qwords & tw)
        if score < 4:
            continue
        scored.append({"cassette": cassette, "pattern": pattern,
                       "template": template, "score": score,
                       "matched": sorted(matched)})
    scored.sort(key=lambda c: (-c["score"], len(c["template"]),
                               c["cassette"], c["pattern"]))
    out, seen = [], set()
    for c in scored:
        if c["template"] in seen:
            continue
        seen.add(c["template"])
        out.append(c)
        if len(out) >= top_n:
            break
    return out


# --------------------------------------------------------------- model ---

_SYSTEM = (
    "You are CETI, a local brain. Below is LOCAL KNOWLEDGE assembled "
    "from every local store: the math result (computed in Python, "
    "never by you), facts, AIML templates, lexicon definitions, and "
    "vector memories. Draw on ALL of it and construct the fullest "
    "answer the knowledge supports, in plain words — combine the "
    "pieces into a coherent answer, not a bare one-line fact. Answer "
    "ONLY using the LOCAL CONTEXT below. If the context does not "
    "contain the answer, reply exactly: "
    "I don't know from local knowledge. "
    "Do not use outside knowledge. Do not invent numbers, dates or "
    "names. Keep the answer short, in plain words.")

_NUM_RE = re.compile(r"\d[\d,\.]*")

# The exact refusal the grounded Tier 5 system prompt demands when the
# assembled local context does not contain the answer. Tier 5b below
# treats this as "the model was called but local knowledge has no
# answer" — the trigger to ask the loaded model from its own
# knowledge instead of stopping there.
_REFUSAL_TEXT = "I don't know from local knowledge."


def _is_refusal(answer):
    """True when the answer is the grounded-tier refusal (ignoring
    case, surrounding whitespace, and a trailing period)."""
    if not isinstance(answer, str):
        return False
    text = answer.strip().casefold().rstrip(".").strip()
    return text == _REFUSAL_TEXT.rstrip(".").casefold()


def _numbers_grounded(answer, context_text, question):
    """Every number in the answer must appear in the context or the
    question (commas/decimal points normalised). Returns
    (grounded_bool, [ungrounded_numbers])."""
    pool = re.sub(r"[,\s]", "", context_text + " " + question)
    bad = []
    for num in _NUM_RE.findall(answer or ""):
        if re.sub(r"[,\s]", "", num) not in pool:
            bad.append(num)
    return (not bad), bad


def _model_tier(query, context_sections, system=None, max_tokens=2048,
                reasoning=True):
    """Local model with assembled context. Returns (result, lines) —
    lines are a FRESH list (see _vector_tier note).

    system overrides the default system prompt (the arbitration step
    uses its own). max_tokens bounds the answer; reasoning=False asks
    the server to disable thinking for this call, which keeps short
    selection-style calls fast."""
    lines = []
    context_text = "\n".join(
        "%s:\n%s" % (title, body)
        for title, body in context_sections if body.strip())
    if not context_text.strip():
        lines.append("LOCAL MODEL — skipped: no local context assembled; "
                     "calling the model on nothing would be asking it to "
                     "hallucinate")
        return None, lines
    messages = [
        {"role": "system",
         "content": (system or _SYSTEM) + "\n\nLOCAL CONTEXT:\n" + context_text},
        {"role": "user", "content": query},
    ]
    try:
        from .servers import chat_complete
        text, _reasoning = chat_complete(messages, temperature=0.0,
                                         max_tokens=max_tokens,
                                         reasoning=reasoning)
    except Exception as e:
        lines.append("LOCAL MODEL — unavailable: %s" % e)
        return None, lines
    grounded, bad = _numbers_grounded(text, context_text, query)
    if grounded:
        lines.append("LOCAL MODEL — answered; numbers guard passed "
                     "(every number in the answer is in the context)")
    else:
        lines.append("LOCAL MODEL — answered BUT numbers guard FAILED: "
                     "ungrounded number(s) %s — do not trust them" % bad)
    return {"tier": "local-model", "answer": text,
            "evidence": "local model, grounded in: " + ", ".join(
                t for t, _b in context_sections if _b.strip()),
            "numbers_grounded": grounded,
            "ungrounded_numbers": bad}, lines


# NOTE (2026-11-15, all-layers-reason): _arbitrate_tier and
# _ARBITRATE_SYSTEM were deleted here. Arbitration was a separate
# model call that picked between competing facts/templates; the
# construct call (Tier 5) now gets ALL of them and composes from
# them, so a separate selection step no longer exists.


_UNGROUNDED_SYSTEM = (
    "You are CETI, a local brain. The local stores were checked and "
    "do not contain the answer to this question, so answer it from "
    "your own knowledge, in plain words, keeping the answer short. "
    "The LOCAL CONTEXT below is background only: it is related, but "
    "it does not contain the answer, so do not quote it as the answer. "
    "Do not invent numbers, dates or names — if you are unsure of a "
    "specific fact, say so instead of guessing.")


def _model_tier_ungrounded(query, context_sections):
    """Tier 5b — the loaded local model, from its own knowledge.

    Called ONLY when the grounded Tier 5 call ran and returned the
    exact refusal: the model was already consulted, local context
    existed, and none of it answers the question. The model is asked
    again with a prompt that permits answering from its weights, and
    the result is labelled UNGROUNDED in its tier and evidence, so a
    reader can always tell it apart from a grounded local answer.

    Deliberately NOT used when no grounded context existed at all:
    the empty-context guard (answer_with_reasoning, Tier 6) never
    calls the model, and tests guard that rule — a question with no
    local context still suggests cloud instead of asking the model
    to answer from nothing.

    Returns (result, lines) like _model_tier, or (None, lines) when
    the model is unavailable, answers empty, or refuses again. The
    numbers guard does not apply here — numbers in an ungrounded
    answer come from the model's weights, not local context — so
    numbers_grounded is always False and the trace says so. The
    result is never recorded into vector memory (ceti_chat's record
    gate only accepts facts/aiml/local-model/math/facts-composition),
    so ungrounded model output can never become local knowledge."""
    lines = []
    hit, mtrace = _model_tier(query, context_sections,
                              system=_UNGROUNDED_SYSTEM,
                              max_tokens=2048, reasoning=True)
    # The numbers-guard line _model_tier appended does not apply to
    # an ungrounded answer: drop it and say so plainly.
    mtrace = [l for l in mtrace if "numbers guard" not in l]
    lines.extend(mtrace)
    if hit is None:
        return None, lines
    if not (hit.get("answer") or "").strip():
        lines.append("LOCAL MODEL (UNGROUNDED) — model returned an "
                     "empty answer; falling through")
        return None, lines
    if _is_refusal(hit.get("answer")):
        lines.append("LOCAL MODEL (UNGROUNDED) — model refused again "
                     "from its own knowledge; falling through")
        return None, lines
    hit = dict(hit)
    hit["tier"] = "local-model-ungrounded"
    hit["evidence"] = (
        "local model, UNGROUNDED — from the model's own knowledge, "
        "not local stores (no fact, AIML pattern, or memory "
        "answered this)")
    hit["numbers_grounded"] = False
    hit["ungrounded_numbers"] = hit.pop("ungrounded_numbers", [])
    lines.append("LOCAL MODEL (UNGROUNDED) — answered from the "
                 "model's own knowledge; numbers guard not "
                 "applicable (numbers come from weights, not local "
                 "context) — labelled, not grounded")
    return hit, lines


# NOTE (2026-11-15, all-layers-reason): _elaborate_tier,
# _wants_elaboration, _ELABORATE_RE and _ELABORATE_SYSTEM were
# deleted here. Elaboration (Tier 1c, 2026-11-14) fed a fact hit +
# related templates to the model only when the sysop explicitly
# asked for more ("tell me more", "explain", ...). The construct
# call (Tier 5) now does that for EVERY question — the model always
# gets every matched fact and template and builds the fuller answer
# — so the phrase trigger and the separate elaboration step no
# longer exist. A precise question still costs one model call now;
# that is the trade-off Jero chose.


def _direct_fallback(math_hit, comp, fact_hit, aiml_matches,
                     fact_candidates):
    """Best direct answer when the construct model is unavailable
    (model down / empty answer). Old ladder priority: math, then
    two-hop composition, then a clear fact, then an AIML template,
    then the first competing fact. Returns the old-style result
    dict (tier/answer/evidence) or None when no layer hit at all.
    The caller adds reasoning_trace, memories, cloud_suggested and
    numbers_grounded."""
    if math_hit:
        return dict(math_hit)
    if comp:
        return dict(comp)
    if fact_hit:
        return dict(fact_hit)
    if aiml_matches:
        m = aiml_matches[0]
        return {"tier": "aiml", "answer": m["answer"],
                "evidence": "cassette %s pattern: %s" % (m["cassette"],
                                                          m["pattern"])}
    if fact_candidates:
        m = fact_candidates[0]
        return {"tier": "facts",
                "answer": "%s — %s: %s" % (m["subject"], m["predicate"],
                                           m["object"]),
                "evidence": "fact: %s %s %s (source: %s)" % (
                    m["subject"], m["predicate"], m["object"],
                    m["source"])}
    return None


# ------------------------------------------------------------ entrypoint ---

def answer_with_reasoning(query, roots=None, ident_hash=None,
                          lexicon=None, session_id=None):
    """Run the construct ladder (all-layers-reason, 2026-11-15).
    Every layer outputs what it found — math result, facts, AIML
    templates, lexicon definitions, vector memories, session files —
    and the local model constructs the answer from all of it. No
    layer returns its hit directly any more. Returns tier, answer,
    evidence, reasoning_trace (one line per layer, saying what it
    contributed), memories (vector hits), cloud_suggested,
    numbers_grounded.

    The direct answers survive only as the fallback when the model
    is down: the best direct hit (math, composition, fact, AIML)
    answers instead of failing the question. When no layer found
    anything, the model is never called and the cloud suggestion
    stands, as before.

    When `lexicon` is None the shipped lexicon (if any) is loaded
    lazily for this query's words via get_lexicon(); pass {} explicitly
    for seed-only behaviour."""
    if roots is None:
        roots = brain.resolve_roots()
    query = (query or "").strip()
    if lexicon is None:
        lexicon = get_lexicon(roots, want=_query_want_words(query))
    trace = []
    if not query:
        return {"tier": "empty", "answer": "(empty question)",
                "evidence": "", "reasoning_trace": [],
                "memories": [], "cloud_suggested": False,
                "numbers_grounded": True}

    # Tier 0 — math, computed in Python (never the model). The
    # result is a context fact for the construct call, and the
    # direct answer if the model is down.
    math_hit = _math_tier(query)
    if math_hit:
        trace.append("MATH — hit: " + math_hit["evidence"])
    else:
        trace.append("MATH — miss: no arithmetic in the question")

    # Tier 1 — facts, predicate-aware, with two-hop composition first
    # (composition needs its own parse; plain facts second). A clear
    # hit, competing candidates, and the composition result ALL
    # become context for the construct call — nothing returns
    # directly any more.
    comp, ctrace = _composition_tier(query, roots)
    trace.extend(ctrace)
    fact_hit, ftrace, fact_candidates = _facts_tier(query, roots)
    trace.extend(ftrace)

    # Tier 2 — AIML: every matching pattern becomes context.
    aiml_matches = ceti_chat._aiml_all_matches(query, roots)
    if aiml_matches:
        trace.append("AIML — %d pattern(s) matched: %s" % (
            len(aiml_matches),
            "; ".join("%s: %s" % (m["cassette"], m["pattern"])
                      for m in aiml_matches)))
    else:
        trace.append("AIML — miss: no category pattern matched")

    # Tier 3 — lexicon expansion (feeds Tier 4 and Tier 5).
    expanded, expansions, definitions = expand_query(query, lexicon)
    if expansions:
        trace.append("LEXICON — expanded %s -> synonyms %s" % (
            ", ".join(sorted(expansions)),
            {k: v[:3] for k, v in expansions.items()}))
    else:
        trace.append("LEXICON — no seed/loaded entry matched the question")
    lexicon_text = "\n".join("- %s: %s" % (w, d) for w, d in definitions)

    # Tier 4 — vector memory over the EXPANDED query.
    memories, vtrace = _vector_tier(expanded, ident_hash)
    trace.extend(vtrace)

    # Empty-context guard (fix-before-wiring, 2026-11-06): a vector hit
    # only counts as grounded context when it shares a content word
    # with the question or with the question's lexicon synonyms.
    # With a tiny vector memory the raw cosine threshold is decoration
    # — the only two rows in the store clear it for every question —
    # so unfiltered hits used to count as "context" and the model was
    # called on nothing. Lexicon definitions count as real context on
    # their own. If neither exists, the model is skipped entirely.
    query_words = _content_words(query)
    for syns in expansions.values():
        for syn in syns:
            query_words |= _content_words(syn)
    relevant = [h for h in memories
                if _content_words(h["text"]) & query_words]
    dropped = len(memories) - len(relevant)
    if dropped:
        trace.append("VECTOR MEMORY — %d hit(s) dropped as irrelevant: "
                     "no content word shared with the question" % dropped)
    memories = relevant

    # AIML retrieval: the exact-match tier (Tier 2) only fires on a
    # pattern hit, so an open-ended question never sees the stores.
    # Score the cassettes by content-word overlap (same shared-word
    # bar as the vector filter) and feed the best templates to the
    # model as context in Tier 5. Scored on the original question
    # words only — lexicon synonyms are too noisy here (see
    # _aiml_search); they still feed the vector tier above.
    t0 = time.time()
    aiml_ctx = _aiml_search(_content_words(query), roots)
    aiml_ms = (time.time() - t0) * 1000
    if aiml_ctx:
        trace.append("AIML — no exact pattern, %d template(s) fed to "
                     "local model as context (%.0fms): %s" % (
                         len(aiml_ctx), aiml_ms,
                         "; ".join("%s: %s" % (c["cassette"], c["pattern"])
                                  for c in aiml_ctx)))
    else:
        trace.append("AIML — no relevant template found for context "
                     "(%.0fms)" % aiml_ms)

    # Session files: this session's attachments, embedded at upload
    # time, searched by the same content-word bar as vector memory.
    sess_hits, strace = _attachment_hits(query, ident_hash, session_id)
    trace.extend(strace)

    # Assemble every layer's output into context sections. Nothing
    # here is an answer yet — the construct call below builds the
    # answer from all of it.
    math_text = ("- %s (computed in Python)"
                 % math_hit["calculation"]) if math_hit else ""
    _seen_facts = set()
    _all_facts = []
    for _src in ((comp or {}).get("facts") or [],
                 (fact_hit or {}).get("facts") or [],
                 fact_candidates):
        for _m in _src:
            _key = (_m["subject"], _m["predicate"], _m["object"])
            if _key not in _seen_facts:
                _seen_facts.add(_key)
                _all_facts.append(_m)
    fact_text = "\n".join(
        "- %s %s %s (source: %s)" % (m["subject"], m["predicate"],
                                      m["object"], m["source"])
        for m in _all_facts)
    _seen_aiml = set()
    _all_aiml = []
    for _c in list(aiml_matches) + list(aiml_ctx):
        _key = (_c["cassette"], _c["pattern"])
        if _key not in _seen_aiml:
            _seen_aiml.add(_key)
            _all_aiml.append(_c)
    aiml_text = "\n".join(
        "- %s pattern: %s — %s" % (c["cassette"], c["pattern"],
                                   # Exact matches carry the rendered
                                   # answer (star substituted); retrieved
                                   # templates carry the raw template.
                                   c.get("answer") or c["template"])
        for c in _all_aiml)
    sess_text = "\n".join(
        "- %s: %s" % (h["source"], h["text"]) for h in sess_hits)
    context_sections = [
        ("MATH", math_text),
        ("FACTS", fact_text),
        ("LEXICON DEFINITIONS", lexicon_text),
        ("AIML CONTEXT", aiml_text),
        ("SESSION FILES", sess_text),
        ("VECTOR MEMORY", "\n".join(
            "- #%s (%s, %.2f): %s" % (h["id"], h["source"], h["score"],
                                      h["text"]) for h in memories)),
    ]
    if not any(_b.strip() for _t, _b in context_sections):
        trace.append("LOCAL MODEL — skipped: no grounded context (no "
                     "math, no fact, no AIML pattern, no lexicon "
                     "definition, no vector hit shares a content "
                     "word with the question, and no session file "
                     "shares one); calling the model on nothing "
                     "would be asking it to hallucinate")
        trace.append("CLOUD — suggested only, never called: no local "
                     "tier answered and no grounded context existed")
        return {"tier": "cloud-suggested",
                "answer": ("No local knowledge answers this: no math, no "
                           "fact, no AIML pattern, no lexicon definition, "
                           "and no relevant vector memory. Cloud "
                           "suggestion: retry in a regular chat session "
                           "with a cloud provider selected — the CETI "
                           "layer itself never calls a cloud API."),
                "evidence": ("math: miss · facts: miss · aiml: miss · "
                             "lexicon: %s · vector: %d relevant hit(s), "
                             "%d dropped as irrelevant · aiml: %d "
                             "template(s) for context"
                             % ("hit" if expansions else "miss",
                                len(memories), dropped, len(aiml_ctx))),
                "reasoning_trace": trace, "memories": memories,
                "cloud_suggested": True, "numbers_grounded": True}
    # Tier 5 — construct: the local model builds the answer from
    # every layer's output at once.
    trace.append("CONSTRUCT — %d section(s) to the local model: %s" % (
        len([1 for _t, _b in context_sections if _b.strip()]),
        ", ".join(_t for _t, _b in context_sections if _b.strip())))
    model_hit, mtrace = _model_tier(query, context_sections)
    trace.extend(mtrace)
    if model_hit and _is_refusal(model_hit.get("answer")):
        # Tier 5b (2026-11-14): the model ran, local context
        # existed, and none of it answers the question — the
        # grounded prompt gagged it into the refusal. The loaded
        # local model is asked again from its own knowledge and
        # the answer is labelled UNGROUNDED, so a reader can
        # always tell it apart from a grounded local answer.
        # (When no grounded context existed at all, the guard
        # above skipped the model entirely and this never runs.)
        trace.append("LOCAL MODEL — grounded answer refused: no "
                     "local context contains the answer; asking "
                     "the loaded model from its own knowledge, "
                     "labelled ungrounded")
        ungrounded, utrace = _model_tier_ungrounded(query,
                                                    context_sections)
        trace.extend(utrace)
        if ungrounded:
            ungrounded["reasoning_trace"] = trace
            ungrounded["memories"] = memories
            ungrounded["cloud_suggested"] = False
            return ungrounded
        # The model refused again or is unavailable: fall through
        # to the cloud suggestion below.
        model_hit = None
    if model_hit:
        model_hit["reasoning_trace"] = trace
        model_hit["memories"] = memories
        model_hit["cloud_suggested"] = False
        return model_hit

    # Model unavailable (or an empty answer): fall back to the best
    # direct hit instead of failing the question. Old ladder
    # priority: math, then two-hop composition, then a clear fact,
    # then an AIML template, then the first competing fact.
    direct = _direct_fallback(math_hit, comp, fact_hit, aiml_matches,
                              fact_candidates)
    if direct:
        trace.append("CONSTRUCT — model unavailable; falling back to "
                     "the direct %s answer" % direct["tier"])
        direct["reasoning_trace"] = trace
        direct["memories"] = memories
        direct["cloud_suggested"] = False
        direct["numbers_grounded"] = True
        return direct

    # Tier 6 — cloud suggested, never called.
    trace.append("CLOUD — suggested only, never called: no local tier "
                 "answered")
    return {"tier": "cloud-suggested",
            "answer": ("No local knowledge answers this: no math, no "
                       "fact, no AIML pattern, no lexicon definition "
                       "that answers it, and no relevant vector memory. "
                       "Cloud suggestion: retry in a regular chat "
                       "session with a cloud provider selected — the "
                       "CETI layer itself never calls a cloud API."),
            "evidence": ("math: miss · facts: miss · aiml: miss · "
                         "lexicon: %s · vector: %d relevant hit(s)"
                         % ("hit" if expansions else "miss",
                            len(memories))),
            "reasoning_trace": trace, "memories": memories,
            "cloud_suggested": True, "numbers_grounded": True}


# ------------------------------------------------------------ self-test ---

QUESTIONS = [
    ("Q1", "What is the capital of Canada?", "Ottawa"),
    ("Q2", "What is the currency of Japan?", "Japanese yen"),
    ("Q3", "What is the population of France?", "69081996"),
    ("Q4", "When did FidoNet start operating?", "1984"),
    ("Q5", "What was introduction date of Commodore 64", "January 1982"),
    ("Q6", "What is most common speed of dial-up modems", "56k"),
    ("Q7", "tell me about dial-up forums", None),
    ("Q8", "what did sysops do on a BBS?", None),
    ("Q9", "What is 17 * 23 + 41?", "432"),
    ("Q10", "What is 15% of 240?", "36"),
    ("Q11", "What is the capital of Tuvalu?", "Funafuti"),
    ("Q12", "Who won the 2026 World Cup?", None),
    ("Q13", "What is the capital of the country whose currency is the yen?",
     "Tokyo"),
    ("Q14", "Tell me about fidonet", "1984"),
]


def _auto_correct(answer, expected):
    """Auto-checkable verdict: expected substring present (case-insens),
    or None when the question has no fixed expected answer (model
    judgement questions — printed for human review instead)."""
    if expected is None:
        return None
    return expected.lower() in (answer or "").lower()


if __name__ == "__main__":
    import sys
    import time
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from hub.identity import Identity
    roots = brain.resolve_roots()
    secret = open(os.path.join(
        os.environ.get("HUB_DATA_DIR",
                       "/home/hatch/workspace/mya-node/data"),
        "identity.dat"), "rb").read()
    ident = Identity(secret)
    print("roots:", roots["ceti_dir"], "| ident:", ident.hash[:12])
    auto_total, auto_pass = 0, 0
    for qid, q, expected in QUESTIONS:
        t0 = time.time()
        r = answer_with_reasoning(q, roots=roots, ident_hash=ident.hash)
        dt = time.time() - t0
        verdict = _auto_correct(r["answer"], expected)
        if verdict is not None:
            auto_total += 1
            auto_pass += 1 if verdict else 0
        print("--- %s %.1fs tier=%s correct=%s ---" % (
            qid, dt, r["tier"], verdict))
        print("Q:", q)
        print("A:", (r["answer"] or "")[:600])
        print("EVID:", r["evidence"])
        for step in r["reasoning_trace"]:
            print("  >", step)
        print("MEMORIES:", r.get("memories"))
    print("AUTO-CHECKABLE: %d/%d correct; %d question(s) need human "
          "review (Q7, Q8, Q12 — open/model answers)"
          % (auto_pass, auto_total, len(QUESTIONS) - auto_total))
