"""research — the node research agent, stdlib only.

Mirrors the original CETI loop (tasks.py + researcher.py) in node form:

- tasks: research jobs (topic + optional source URLs) and benchmark
  runs, kept in <data>/research.db (sqlite, same shape as CETI's
  tasks.db) plus a proposals table for the human promotion gate.
- heartbeat: tick() picks the next workable task and works it. A
  daemon thread in the web process fires it every 60s, and the
  Research tab can fire it by hand. One tick at a time (module
  lock); a slow tick never stacks. Tasks survive restarts: the
  thread resumes pending work when the server comes back.
- lynx harness: pages are read as text (hub/lynx.py), never rendered.
- compile: before any model is called, the agent compiles what CETI
  already knows locally for the topic — facts, AIML categories, and
  vector-memory nodes — so known answers are never re-queried. The
  Fidonet case is exactly this: the cassette already holds Fidonet
  history for its specific questions, so a task on Fidonet starts
  from that instead of from zero.
- distill: cloud first when a key is configured (the local model
  provably returns empty content for the JSON distill prompt — it
  burns its whole output budget thinking), local only as the no-key
  fallback. Output is QA pairs + fact triples, parsed best-effort
  (a small local model is stochastic).
- promotion is HUMAN, by design (the fact-gate lesson: unstructured
  distilled pairs need a human audit before they ship). Distilled
  pairs land as pending proposals. Only on approval do facts reach
  the operator fact store and QA reach a learned cassette (+ vector
  memory), which is what moves the ladder — and the benchmark —
  because those are ladder tiers. Approved QA deliberately does NOT
  enter knowledge.sqlite3: the benchmark set stays independent, so a
  rising score means the ladder actually learned, not that the test
  was fed its own answers.
"""
import json
import os
import re
import sqlite3
import threading
import time
import xml.sax.saxutils as _xml
from urllib.parse import quote as _url_quote
from urllib.parse import urlparse as _urlparse
from urllib.request import Request as _UrlRequest
from urllib.request import urlopen as _urlopen

from . import brain as _brain
from . import ceti_chat as _ceti
from .config import data_dir as _data_dir
from .lynx import fetch_text as _fetch_text

_TICK_LOCK = threading.Lock()
_TICK_INTERVAL_S = 60
_MAX_PAGES = 5
# The local chat server runs at 2048 tokens of context (shipped
# default, box and laptop alike). The distill prompt is the report
# plus instructions plus the JSON output — an 8000-char report
# overflows the context and llama-server rejects the call, which
# parks the task as needs-model ("local chat down"). Keep the
# report small enough to fit: ~4000 chars leaves room for the
# prompt and the answer.
# 2026-11-07 (second cut, 4000 -> 2000): the 4000-char report
# still failed — the reasoning model thinks through the report
# first and burns its 1500-token output budget on thinking,
# returning empty content. A 2000-char report leaves the model
# room to think AND answer within the budget. The Nobel winners
# sit in the lead paragraph of the first page, so the answer
# survives the cut.
# 2026-11-07 (cloud cap): cloud models take 128k+ of context, so
# when a cloud key is configured the distill feeds the whole
# report — up to 12000 chars, roughly the first three fetched
# pages including their table rows. The local cap stays 2000.
_REPORT_CHARS = 2000
_REPORT_CHARS_CLOUD = 12000

DISTILL_PROMPT = """Extract up to 8 self-contained question/answer pairs AND up to 8
fact triples from the report below.
The research topic is: %s
Prioritize pairs that answer the topic — if the report names the
people, dates, or facts the topic asks for, those come first.
Rules:
- each answer 1-3 sentences, factual and timeless (no "today",
  no "recently", no dates that will rot)
- questions a curious person would actually ask
- fact triples are (subject, predicate, object): "Fidonet",
  "founded", "1984" — short, no sentences in any field
- no markdown, no personal data, plain text only
Reply with ONLY this JSON object, no other text:
{"qa": [{"q": "...", "a": "..."}], "facts": [{"subject": "...", "predicate": "...", "object": "..."}]}

REPORT:
%s
"""

URLS_PROMPT = """List up to 5 authoritative source URLs (Wikipedia, official
docs, primary histories) for researching this topic. Reply with
ONLY a JSON list of URLs, no other text.

TOPIC:
%s
"""


def db_path(data_dir):
    return os.path.join(data_dir, "research.db")


class Store:
    """research.db: tasks, proposals, bench_runs, meta."""

    def __init__(self, data_dir):
        self.path = db_path(data_dir)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        db = sqlite3.connect(self.path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            " id INTEGER PRIMARY KEY, kind TEXT NOT NULL,"
            " title TEXT NOT NULL, topic TEXT NOT NULL,"
            " urls TEXT NOT NULL DEFAULT '[]',"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " result TEXT, created REAL NOT NULL, updated REAL NOT NULL)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS proposals ("
            " id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL,"
            " kind TEXT NOT NULL, subject TEXT, predicate TEXT,"
            " object TEXT, question TEXT, answer TEXT,"
            " source_url TEXT, status TEXT NOT NULL DEFAULT 'pending',"
            " result TEXT, created REAL NOT NULL)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS bench_runs ("
            " id INTEGER PRIMARY KEY, started REAL NOT NULL,"
            " finished REAL, n INTEGER, accuracy REAL, summary TEXT)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        db.commit()
        db.close()

    def _db(self):
        return sqlite3.connect(self.path)

    # ---- tasks --------------------------------------------------------
    def add_task(self, kind, title, topic, urls=None):
        title = " ".join((title or "").split())[:300]
        topic = " ".join((topic or "").split())[:300]
        if not title or kind not in ("research", "benchmark"):
            raise ValueError("kind research|benchmark and a title required")
        db = self._db()
        row = db.execute(
            "SELECT id FROM tasks WHERE title=? AND status='pending'",
            (title,)).fetchone()
        if row:
            db.close()
            return row[0]
        now = time.time()
        cur = db.execute(
            "INSERT INTO tasks (kind, title, topic, urls, created, updated)"
            " VALUES (?,?,?,?,?,?)",
            (kind, title, topic, json.dumps(urls or []), now, now))
        db.commit()
        task_id = cur.lastrowid
        db.close()
        return task_id

    def list_tasks(self, limit=50):
        db = self._db()
        rows = db.execute(
            "SELECT id, kind, title, topic, urls, status, attempts,"
            " result, created, updated FROM tasks"
            " ORDER BY id DESC LIMIT ?", (limit,))
        out = [{"id": r[0], "kind": r[1], "title": r[2], "topic": r[3],
                "urls": json.loads(r[4]), "status": r[5], "attempts": r[6],
                "result": json.loads(r[7]) if r[7] else None,
                "created": r[8], "updated": r[9]} for r in rows]
        db.close()
        return out

    def get_task(self, task_id):
        db = self._db()
        r = db.execute(
            "SELECT id, kind, title, topic, urls, status, attempts,"
            " result, created, updated FROM tasks WHERE id=?",
            (task_id,)).fetchone()
        db.close()
        if not r:
            return None
        return {"id": r[0], "kind": r[1], "title": r[2], "topic": r[3],
                "urls": json.loads(r[4]), "status": r[5], "attempts": r[6],
                "result": json.loads(r[7]) if r[7] else None,
                "created": r[8], "updated": r[9]}

    def next_workable(self):
        """Oldest pending task, else oldest needs-model retry."""
        db = self._db()
        for status in ("pending", "needs-model"):
            r = db.execute(
                "SELECT id FROM tasks WHERE status=? ORDER BY id LIMIT 1",
                (status,)).fetchone()
            if r:
                db.close()
                return r[0]
        db.close()
        return None

    def claim_task(self, task_id, force=False):
        # 2026-11-07: a task killed mid-run (Hub restart) stays
        # 'running' forever — the tick loop only claims pending /
        # needs-model, so nothing ever picks it up again and a
        # re-run silently no-ops ("task not pending"). An explicit
        # re-run (Research tab Run button) may force-claim a stuck
        # 'running' task; the tick loop never does, so a genuinely
        # live task can't be double-claimed by the worker.
        # 2026-11-07 (second fix): the tick loop must NOT claim
        # 'needs-model' — it re-ran the Nobel task forever (17
        # attempts), each distill failing the same way, because
        # needs-model is a parked state awaiting a cloud key or a
        # human decision, not a retry queue. Only 'pending' is
        # auto-claimed; force (explicit Run) may reclaim
        # needs-model and a stuck 'running'.
        states = "('pending')" if not force else \
            "('pending','needs-model','running')"
        db = self._db()
        cur = db.execute(
            "UPDATE tasks SET status='running', attempts=attempts+1,"
            " updated=? WHERE id=? AND status IN %s" % states,
            (time.time(), task_id))
        db.commit()
        ok = cur.rowcount > 0
        db.close()
        return ok

    def finish_task(self, task_id, status, result):
        db = self._db()
        db.execute(
            "UPDATE tasks SET status=?, result=?, updated=? WHERE id=?",
            (status, json.dumps(result)[:20000], time.time(), task_id))
        db.commit()
        db.close()

    def counts(self):
        db = self._db()
        rows = db.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status")
        out = {r[0]: r[1] for r in rows}
        db.close()
        return out

    # ---- proposals ------------------------------------------------------
    def add_proposal(self, task_id, kind, source_url="", **fields):
        db = self._db()
        if kind == "qa":
            dup = db.execute(
                "SELECT id FROM proposals WHERE task_id=? AND kind='qa'"
                " AND question=? AND status='pending'",
                (task_id, fields.get("question", ""))).fetchone()
        else:
            dup = db.execute(
                "SELECT id FROM proposals WHERE task_id=? AND kind='fact'"
                " AND subject=? AND predicate=? AND object=?"
                " AND status='pending'",
                (task_id, fields.get("subject", ""),
                 fields.get("predicate", ""), fields.get("object", ""))).fetchone()
        if dup:
            db.close()
            return dup[0]
        cur = db.execute(
            "INSERT INTO proposals (task_id, kind, subject, predicate,"
            " object, question, answer, source_url, created)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, kind, fields.get("subject", ""),
             fields.get("predicate", ""), fields.get("object", ""),
             fields.get("question", ""), fields.get("answer", ""),
             source_url, time.time()))
        db.commit()
        pid = cur.lastrowid
        db.close()
        return pid

    def list_proposals(self, task_id=None, status=None, limit=100):
        db = self._db()
        q = ("SELECT id, task_id, kind, subject, predicate, object,"
             " question, answer, source_url, status, result, created"
             " FROM proposals WHERE 1=1")
        args = []
        if task_id is not None:
            q += " AND task_id=?"
            args.append(task_id)
        if status is not None:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = db.execute(q, args)
        out = [{"id": r[0], "task_id": r[1], "kind": r[2],
                "subject": r[3], "predicate": r[4], "object": r[5],
                "question": r[6], "answer": r[7], "source_url": r[8],
                "status": r[9],
                "result": json.loads(r[10]) if r[10] else None,
                "created": r[11]} for r in rows]
        db.close()
        return out

    def get_proposal(self, proposal_id):
        db = self._db()
        r = db.execute(
            "SELECT id, task_id, kind, subject, predicate, object,"
            " question, answer, source_url, status FROM proposals WHERE id=?",
            (proposal_id,)).fetchone()
        db.close()
        if not r:
            return None
        return {"id": r[0], "task_id": r[1], "kind": r[2], "subject": r[3],
                "predicate": r[4], "object": r[5], "question": r[6],
                "answer": r[7], "source_url": r[8], "status": r[9]}

    def set_proposal(self, proposal_id, status, result=None):
        db = self._db()
        db.execute(
            "UPDATE proposals SET status=?, result=? WHERE id=?",
            (status, json.dumps(result) if result is not None else None,
             proposal_id))
        db.commit()
        db.close()

    def pending_proposal_count(self):
        db = self._db()
        n = db.execute(
            "SELECT COUNT(*) FROM proposals WHERE status='pending'"
            ).fetchone()[0]
        db.close()
        return n

    # ---- bench ----------------------------------------------------------
    def add_bench_run(self, started, finished, n, accuracy, summary):
        db = self._db()
        cur = db.execute(
            "INSERT INTO bench_runs (started, finished, n, accuracy, summary)"
            " VALUES (?,?,?,?,?)",
            (started, finished, n, accuracy, json.dumps(summary)[:20000]))
        db.commit()
        rid = cur.lastrowid
        db.close()
        return rid

    def last_bench(self):
        db = self._db()
        r = db.execute(
            "SELECT id, started, finished, n, accuracy, summary FROM bench_runs"
            " ORDER BY id DESC LIMIT 1").fetchone()
        db.close()
        if not r:
            return None
        return {"id": r[0], "started": r[1], "finished": r[2], "n": r[3],
                "accuracy": r[4],
                "summary": json.loads(r[5]) if r[5] else None}

    # ---- meta -------------------------------------------------------------
    def get_meta(self, k, default=None):
        db = self._db()
        r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        db.close()
        return r[0] if r else default

    def set_meta(self, k, v):
        db = self._db()
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
        db.commit()
        db.close()


# ---- local compile ------------------------------------------------------

def compile_local(data_dir, ident_hash, topic, roots=None):
    """What CETI already knows about a topic, without any model call.

    Facts (exact subject match), AIML categories (cassette-name match
    or pattern-word overlap), and vector-memory nodes (embed server
    up only, else []). This is the compile step the ladder's local
    tiers already perform per question, lifted to topic level so the
    agent — and the operator — can see coverage before researching.
    """
    if roots is None:
        roots = _brain.resolve_roots()
    facts = []
    try:
        for m in _ceti.facts_search(topic, roots)["matches"][:10]:
            facts.append({"subject": m["subject"], "predicate": m["predicate"],
                          "object": m["object"], "source": m["source"]})
    except Exception:
        pass
    aiml = []
    try:
        topic_words = {w for w in _ceti._words(topic) if len(w) >= 3}
        for name, text in _ceti._iter_cassettes(roots):
            name_hit = any(w in name.upper() for w in topic_words)
            for pattern, template in _ceti._CAT_RE.findall(text):
                overlap = topic_words & {w for w in _ceti._words(pattern)
                                         if len(w) >= 3}
                if name_hit or overlap:
                    aiml.append({
                        "answer": _ceti._render_template(template, []),
                        "evidence": "cassette %s pattern: %s"
                                    % (name, _ceti._norm(pattern))})
                    if len(aiml) >= 10:
                        break
            if len(aiml) >= 10:
                break
    except Exception:
        pass
    memories = []
    try:
        memories = _ceti._memory_hits(topic, ident_hash)
    except Exception:
        pass
    return {"facts": facts, "aiml": aiml, "memories": memories}


# ---- distill parsing ------------------------------------------------------

def _balanced_span(raw, open_ch, close_ch):
    start = raw.find(open_ch)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]
    return None


def _extract_distill(raw):
    """Best-effort {"qa": [...], "facts": [...]} from model output."""
    candidates = [raw.strip()]
    span = _balanced_span(raw, "{", "}")
    if span is not None:
        candidates.append(span)
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        qa = []
        for p in data.get("qa", []) or []:
            if isinstance(p, dict):
                q = str(p.get("q", "")).strip()
                a = str(p.get("a", "")).strip()
                if q and a and len(q) <= 300:
                    qa.append({"q": q, "a": a})
        facts = []
        for f in data.get("facts", []) or []:
            if isinstance(f, dict):
                s = str(f.get("subject", "")).strip()
                pr = str(f.get("predicate", "")).strip()
                o = str(f.get("object", "")).strip()
                if s and pr and o and len(s) <= 120 and len(o) <= 200:
                    facts.append({"subject": s, "predicate": pr, "object": o})
        if qa or facts:
            return {"qa": qa[:8], "facts": facts[:8]}
    return {"qa": [], "facts": []}


def _extract_urls(raw):
    candidates = [raw.strip()]
    span = _balanced_span(raw, "[", "]")
    if span is not None:
        candidates.append(span)
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            out = []
            for u in data:
                u = str(u).strip()
                if u.startswith(("http://", "https://")):
                    out.append(u)
            if out:
                return out[:5]
    return []


# ---- model calls ----------------------------------------------------------

def server_cloud():
    """First cloud provider with a server-side env key, or None."""
    from .cloud import PROVIDERS
    for provider in ("orcarouter", "openrouter", "nvidia"):
        key = os.environ.get(PROVIDERS[provider]["env"], "")
        if key:
            return {"provider": provider, "api_key": key,
                    "model": PROVIDERS[provider]["suggest"][0]}
    return None


def resolve_cloud(body):
    """Browser-supplied provider/key/model, else server env, else None."""
    provider = (body or {}).get("provider", "")
    if provider:
        from .cloud import key_for, PROVIDERS
        if provider not in PROVIDERS:
            return None
        key = key_for(provider, (body or {}).get("api_key", ""))
        model = (body or {}).get("model", "") or PROVIDERS[provider]["suggest"][0]
        if not key:
            return None
        return {"provider": provider, "api_key": key, "model": model}
    return server_cloud()


def _cloud_complete(cloud, system, user, max_tokens=1500):
    from .cloud import chat_cloud
    return chat_cloud(cloud["provider"], cloud["api_key"], cloud["model"],
                      [{"role": "system", "content": system},
                       {"role": "user", "content": user}],
                      max_tokens=max_tokens, temperature=0.2)


def _local_complete(user, system="Reply with ONLY the requested JSON."):
    from .servers import chat_complete
    # 2026-11-07: the local server runs with --reasoning-budget 512
    # and a 4096-token context. The reasoning tokens count towards
    # max_tokens (they are generated tokens; --reasoning-budget is a
    # sub-cap within the total), so the output budget must be sized
    # as thinking + answer: 512 + 1500 = 2012. Sizing it as 1500
    # alone would leave only 988 tokens for the answer after the
    # thinking, and the distill would truncate. The fit cap uses the
    # 4096 context (chars/4 estimate, 128 spare), same rule as the
    # chat handler's fit budget.
    _REASONING_BUDGET = 512
    _ANSWER_BUDGET = 1500
    _LOCAL_CTX = 4096
    prompt_chars = len(system) + len(user)
    fit = max(256, _LOCAL_CTX - prompt_chars // 4 - 128)
    answer, _reasoning = chat_complete(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        max_tokens=min(_ANSWER_BUDGET + _REASONING_BUDGET, fit),
        temperature=0.2)
    return answer


def suggest_urls(topic, cloud):
    if not cloud:
        return []
    try:
        raw = _cloud_complete(
            cloud, "Reply with ONLY a JSON list of URLs.",
            URLS_PROMPT % topic)
        return _extract_urls(raw)
    except Exception:
        return []


_WIKI_API = "https://en.wikipedia.org/w/api.php"
_WIKI_UA = "UncensoredNodeResearch/0.0.1 (contact: admin@darknet.ca)"


def _wikipedia_urls(topic, limit=5):
    """Wikipedia search API -> article URLs. No key, stdlib only."""
    try:
        url = (_WIKI_API + "?action=query&list=search&srsearch=%s"
               "&format=json&srlimit=%d" % (_url_quote(topic), limit))
        req = _UrlRequest(url, headers={"User-Agent": _WIKI_UA})
        with _urlopen(req, timeout=20) as r:
            data = json.loads(r.read(65536).decode("utf-8", errors="replace"))
        out = []
        for hit in (data.get("query", {}).get("search", []) or []):
            title = (hit.get("title") or "").strip()
            if title:
                out.append("https://en.wikipedia.org/wiki/"
                           + title.replace(" ", "_"))
        return out
    except Exception:
        return []


def _google_urls(topic, limit=5):
    """Google search via the lynx text harness, best-effort: Google
    may bot-wall the sandbox, so an empty list is a normal outcome,
    not an error. Result links only (no google.com internals)."""
    try:
        page = _fetch_text("https://www.google.com/search?q=%s&num=%d"
                           % (_url_quote(topic), limit + 5))
        if page.get("error"):
            return []
        out = []
        for link in page.get("links", []):
            u = (link or "").strip()
            if not u.startswith("http"):
                continue
            host = _urlparse(u).netloc.lower()
            if "google." in host or "gstatic.com" in host:
                continue
            if u not in out:
                out.append(u)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _topic_keywords(topic):
    """Content words from a topic: 4-digit years and capitalized
    words, minus stopwords. Used to find answer-bearing lines in
    table wikitext."""
    words = re.findall(r"[A-Za-z][A-Za-z\-']+|\d{4}", topic or "")
    stop = {"who", "what", "when", "where", "which", "whom", "whose",
            "did", "does", "was", "were", "is", "are", "the", "a", "an",
            "in", "on", "of", "for", "won", "win", "prize", "nobel",
            "how", "why", "many", "much", "tell", "about", "list"}
    out = []
    for w in words:
        if w.lower() in stop or len(w) < 3:
            continue
        if w not in out:
            out.append(w)
    return out[:8]


def _clean_wikitext_line(line):
    """Strip the common wikitext markup from one table line so names
    and facts read as plain text."""
    line = re.sub(r"<ref[^>]*>.*?</ref>", "", line, flags=re.S)
    line = re.sub(r"<ref[^>]*/>", "", line)
    line = re.sub(r"\[\[File:[^\]]*\]\]", "", line)
    line = re.sub(r"\{\{sortname\|([^|}]+)\|([^|}]+)(?:\|[^}]*)?\}\}",
                  r"\1 \2", line)
    line = re.sub(r"\{\{flagdeco\|[^}]+\}\}\s*", "", line)
    line = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", line)
    line = re.sub(r"\[\[([^\]]+)\]\]", r"\1", line)
    line = re.sub(r"\{\{[^}]*\}\}", "", line)
    line = re.sub(r"<[^>]+>", "", line)
    line = line.replace("'''", "").replace("''", "")
    return " ".join(line.split())


def _wiki_year_cell(line):
    """The year of a wikitext table year cell, or None.

    Real form: ``|rowspan=3 |2024`` (the year is the last field).
    A bare ``|2024`` cell also counts. Anything else — a death year
    inside a name row, a year inside a citation — is not a cell.
    """
    m = re.match(r"^\|rowspan=\d+\s*\|(\d{4})\s*$", line.strip())
    if m:
        return m.group(1)
    m = re.match(r"^\|\s*(\d{4})\s*$", line.strip())
    if m:
        return m.group(1)
    return None


def _wiki_year_block(lines, year):
    """The table block for one year, cleaned, or "" if absent.

    A year cell starts a block; the next year cell (any year) ends
    it. The block holds exactly that year's winners — no neighbours
    from other years, no death-year matches — so the distill cannot
    attribute a 2017 laureate to 2024, which the keyword windows
    let it do (Richard Henderson, 2017, was distilled as a 2024
    winner on the live run).
    """
    start = None
    for i, line in enumerate(lines):
        if _wiki_year_cell(line) == year:
            start = i
            break
    if start is None:
        return ""
    rows = []
    for idx in range(start, len(lines)):
        line = lines[idx]
        if idx > start and _wiki_year_cell(line) is not None:
            break
        cleaned = _clean_wikitext_line(line)
        if not cleaned or cleaned.startswith(("{|", "|-", "|}")):
            continue
        if cleaned.startswith("|"):
            cleaned = cleaned[1:].strip()
            cleaned = re.sub(
                r"^(?:(?:rowspan|colspan)=\d+\s*|id=\"[^\"]*\"\s*"
                r"|style=\"[^\"]*\"\s*)*\|?\s*", "", cleaned)
        if cleaned:
            rows.append(cleaned)
    return "\n".join(rows)[:3000]


def _wiki_table_rows(title, topic, fetch_json=None):
    """Answer-bearing rows from a Wikipedia article's tables.

    The plain-text fetch drops tables — the extracts API strips
    them, and the HTML scrape loses the table top to proxy
    truncation — so a question whose answer lives in a table
    ("who won the 2024 Nobel Prize in Chemistry?") never reaches
    the distill: the fetched text names no winners at all. The
    article's wikitext (one API call, no HTML chrome) still holds
    the table. Pull it, keep the lines around the topic's keywords
    (a year row plus the ~20 lines after it covers a multi-winner
    year), clean the markup, and return up to ~3000 chars of
    readable rows. Fail-soft: "" on any error.
    """
    try:
        keywords = _topic_keywords(topic)
        if not keywords:
            return ""
        # 2026-11-07: a keyword that also appears in the ARTICLE TITLE
        # is generic for that article — "Chemistry" matches every line
        # of a chemistry article, so the 3000-char cap fills with
        # intro/template lines and the 2024 table rows never survive.
        # Drop title words; the distinctive anchor (the year) remains.
        # Fall back to the full list only if that leaves nothing.
        title_words = set(w.lower()
                          for w in re.findall(r"[A-Za-z]+", title or ""))
        specific = [k for k in keywords if k.lower() not in title_words]
        if specific:
            keywords = specific
        url = (_WIKI_API + "?action=query&prop=revisions&rvprop=content"
               "&rvslots=main&titles=%s&format=json" % _url_quote(title))
        if fetch_json is None:
            req = _UrlRequest(url, headers={"User-Agent": _WIKI_UA})
            with _urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
        else:
            data = fetch_json(url)
        pages = (data.get("query", {}).get("pages", {}) or {})
        wikitext = ""
        for page in pages.values():
            revs = page.get("revisions", [])
            if revs:
                wikitext = revs[0].get("slots", {}).get("main", {}).get("*", "")
                break
        if not wikitext:
            return ""
        lines = wikitext.split("\n")
        # 2026-11-07: when the topic names a year, the year block is
        # the precise answer — the keyword windows mix in other
        # years' laureates (death years, neighbouring blocks) and the
        # distill misattributes them. Prefer the block; fall back to
        # the windows when there is no year or no block for it.
        years = [k for k in keywords if re.fullmatch(r"\d{4}", k)]
        if years:
            block = _wiki_year_block(lines, years[0])
            if block:
                return block
        hits = [i for i, line in enumerate(lines)
                if any(k.lower() in line.lower() for k in keywords)]
        if not hits:
            return ""
        keep = set()
        for i in hits:
            for j in range(max(0, i - 1), min(len(lines), i + 21)):
                keep.add(j)
        rows = []
        for j in sorted(keep):
            cleaned = _clean_wikitext_line(lines[j])
            if not cleaned or cleaned.startswith(("{|", "|-", "|}")):
                continue
            if cleaned.startswith("|"):
                cleaned = cleaned[1:].strip()
                # Cell attributes before the content:
                # "|rowspan=3 id=\"2024\" |2024" -> "2024".
                cleaned = re.sub(
                    r"^(?:(?:rowspan|colspan)=\d+\s*|id=\"[^\"]*\"\s*"
                    r"|style=\"[^\"]*\"\s*)*\|?\s*", "", cleaned)
            if cleaned:
                rows.append(cleaned)
        return "\n".join(rows)[:3000]
    except Exception:
        return ""


def _topic_sentences(body, keywords, max_chars=500):
    """The first sentences of a body that mention a topic keyword,
    up to max_chars; falls back to the definitional first sentence.

    The local model drifts on general prose — the Nobel article's
    lead names the 2026 winners, the 1901-2026 totals, and the women
    winners, none of which answer a 2024 question. So the local
    report's lead is only sentences that mention the topic (a year
    or a distinctive word). When no sentence mentions the topic (the
    2024 winners live only in the table, not the prose), the first
    sentence is the lead — it is definitional and carries no
    distracting facts.
    """
    if not body:
        return ""
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body)
             if s.strip()]
    if not sents:
        return ""
    if keywords:
        out = []
        total = 0
        for s in sents:
            if any(k.lower() in s.lower() for k in keywords):
                if out and total + len(s) + 1 > max_chars:
                    break
                out.append(s)
                total += len(s) + 1
        if out:
            return " ".join(out)
    return sents[0][:max_chars]


def _focused_local_report(rows_list, lead_body, topic):
    """Report for LOCAL distill only (cloud is None).

    Measured failure (2026-11-07): the 2000-char full report — five
    pages of prose with the answer rows buried in it — made the
    local 1.7B model (reasoning off) drift off-topic. The Nobel task
    produced 16 proposals about general Nobel facts and the 2026
    winners, none naming the 2024 winners. A short probe report
    (one lead sentence + the 2024 year-block rows only, ~1107 chars)
    distilled correctly: 6 QA + 8 facts, first QA the 2024 winners.
    A plain 1100-char truncation of the full report backfired — it
    cut the 2024 rows out (they sit after the lead), so the model
    correctly answered "the report does not specify". So the local
    report leads with the topic's table rows (the year block for a
    year topic) and adds only a short lead of sentences that mention
    the topic's key terms/year — nothing else from the other pages'
    bodies. The cloud path keeps the full 12000-char report: the
    cloud model is big enough to prioritise the topic itself.
    """
    keywords = _topic_keywords(topic)
    parts = []
    # Prefer short, complete rows sets. A year block is one year's
    # table rows (the 2024 block is 184 chars); a rows set that hits
    # the 3000-char cap is a loose keyword-window match — prose,
    # infobox junk, other years — and is the noise that makes the
    # model drift (the main article's 3000-char set is its lead
    # prose: the 1901 winner, the 2026 winners, the totals). Drop
    # the capped sets. If no short set exists, the report is just
    # the lead: a truncated noise set is still noise, and the model
    # saying "the report does not specify" beats it answering from
    # the wrong year's winners.
    short = [r for r in rows_list if r and len(r) < 3000]
    rows = "\n".join(short)
    if rows:
        parts.append("TABLE ROWS (from the articles' tables):\n" + rows)
    if lead_body:
        # A year is the distinctive anchor for a year topic; the
        # other topic words (e.g. "Chemistry") match every sentence
        # of the article and let the noise back in — the 2026
        # winners sentence mentions "Chemistry" too. With no year,
        # the full keyword list is the anchor.
        years = [k for k in keywords if re.fullmatch(r"\d{4}", k)]
        lead = _topic_sentences(lead_body, years if years else keywords)
        if lead:
            parts.append(lead)
    return "\n\n".join(parts)


def search_sources(topic):
    """Real web search for a research topic: Wikipedia API first,
    Google second (topped up only when Wikipedia returns fewer than
    5). Returns up to 5 URLs, deduplicated. Fail-soft: [] on any
    error, and the caller falls back to model-suggested URLs."""
    urls = _wikipedia_urls(topic)
    if len(urls) < 5:
        for u in _google_urls(topic):
            if u not in urls:
                urls.append(u)
            if len(urls) >= 5:
                break
    return urls[:5]


def distill_text(report_text, topic, cloud):
    """Cloud first when a cloud key is configured, local only as the
    no-key fallback. Returns dict or error.

    2026-11-07: the local-first order is dead. LFM3 provably returns
    empty content for this JSON distill prompt — a direct test with a
    937-char prompt and 1270 max_tokens came back finish=length,
    content '', 5094 chars of reasoning (the whole budget burned
    thinking), and the full 1660-token budget did the same. The
    local attempt wasted minutes and always failed, so when a cloud
    key exists the distill goes straight to cloud with the big
    report. Local stays only for boxes with no key at all, where it
    is best-effort and its failure is an honest error, not a silent
    empty result.
    """
    if cloud is not None:
        prompt = DISTILL_PROMPT % (topic, report_text[:_REPORT_CHARS_CLOUD])
        try:
            raw = _cloud_complete(
                cloud, "Reply with ONLY the requested JSON object.", prompt)
            model_used = cloud["provider"] + "/" + cloud["model"]
        except Exception as e:
            return {"error": "distill failed: %s" % e}
        if not (raw or "").strip():
            return {"error": "cloud model returned no output"}
        out = _extract_distill(raw)
        out["model_used"] = model_used
        return out
    prompt = DISTILL_PROMPT % (topic, report_text[:_REPORT_CHARS])
    raw = None
    local_down = False
    try:
        raw = _local_complete(prompt)
    except Exception:
        raw = None
        local_down = True
    if not (raw or "").strip():
        # A reasoning model can spend its whole budget inside
        # <think> and return no answer at all (seen live on LFM3:
        # 1500 tokens of thinking, empty content). An empty reply
        # is a failed distill, not an empty result — falling
        # through here is what keeps a task from landing "done"
        # with zero proposals and no error.
        raw = None
    if raw is None:
        if local_down:
            return {"error": "no model available (local chat down, no cloud key)"}
        return {"error": "local model returned no output (no cloud key to fall back to)"}
    out = _extract_distill(raw)
    out["model_used"] = "local"
    return out


# ---- task work --------------------------------------------------------------

def _article_body(text):
    """Drop leading navigation chrome from a fetched page. The lynx
    text extractor keeps everything that is not in a <nav>/<script>
    tag, so a Wikipedia page starts with ~2000 chars of junk —
    "Jump to content", the search box, and the 101-language list —
    before the first real paragraph. A distill fed that junk has
    nothing to distill from (the Nobel task failed three times on
    it: the model read a language list and returned no output).
    Article prose runs in long lines; nav chrome runs in short
    ones. Drop leading lines until the first line over 120 chars.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if len(line.strip()) > 120:
            return "\n".join(lines[i:])
    return text


def run_task(data_dir, ident_hash, task_id, cloud=None,
             distill_fn=None, fetch_fn=None, force=False):
    """Work one research task to proposals. Fail-soft throughout."""
    store = Store(data_dir)
    task = store.get_task(task_id)
    if not task:
        return {"error": "unknown task"}
    if not store.claim_task(task_id, force=force):
        return {"skipped": "task not pending (status %s)" % task["status"]}
    fetch = fetch_fn or _fetch_text
    distill = distill_fn or (lambda report, topic: distill_text(report, topic, cloud))
    steps = []
    try:
        compiled = compile_local(data_dir, ident_hash, task["topic"])
        steps.append("compiled local: %d facts, %d aiml, %d memories"
                     % (len(compiled["facts"]), len(compiled["aiml"]),
                        len(compiled["memories"])))
        urls = list(task["urls"])
        if not urls:
            urls = search_sources(task["topic"])
            steps.append("web-searched %d source URLs "
                         "(Wikipedia, Google fallback)" % len(urls))
            if not urls:
                urls = suggest_urls(task["topic"], cloud)
                steps.append("suggested %d source URLs" % len(urls))
        else:
            steps.append("using %d supplied URLs" % len(urls))
        pages = []
        texts = []
        local_rows = []
        local_lead_body = ""
        for url in urls[:_MAX_PAGES]:
            page = fetch(url)
            text = page.get("text", "")
            pages.append({"url": url, "title": page.get("title", ""),
                          "chars": len(text),
                          "error": page.get("error", "")})
            steps.append("fetched %s: %s" % (
                url, ("error " + page["error"]) if page.get("error")
                else ("%d chars" % len(text))))
            if not page.get("error") and text:
                body = _article_body(text)
                if not local_lead_body:
                    local_lead_body = body
                contribution = page.get("title", "") + "\n" + body
                # 2026-11-07: the answer to a table question lives in
                # the article's tables, which the plain-text fetch
                # drops (the Nobel winners are named nowhere in the
                # fetched prose). Pull the matching wikitext rows and
                # put them FIRST — they are the most answer-dense
                # content, so even the 2000-char local distill slice
                # sees them. Fail-soft: a dead API call changes
                # nothing about the prose contribution.
                if "wikipedia.org" in url:
                    try:
                        title = _urlparse(url).path.rsplit(
                            "/wiki/", 1)[-1].replace("_", " ")
                        rows = _wiki_table_rows(title, task["topic"])
                        if rows:
                            local_rows.append(rows)
                            contribution = (
                                page.get("title", "")
                                + "\nTABLE ROWS (from the article's tables):\n"
                                + rows + "\n\n" + body)
                            steps.append("table rows from %s: %d chars"
                                         % (url, len(rows)))
                    except Exception:
                        pass
                texts.append(contribution[:7000])
        if not texts:
            store.finish_task(task_id, "failed",
                              {"steps": steps, "compiled": compiled,
                               "pages": pages})
            return {"task": task_id, "status": "failed",
                    "reason": "no pages fetched"}
        report = "\n\n".join(texts)
        if cloud is None:
            # Local distill only: the full report drifts the small
            # model off-topic (see _focused_local_report). Distill
            # the focused report instead — rows first, short lead,
            # no other bodies. The cloud path keeps the full report.
            report = _focused_local_report(local_rows, local_lead_body,
                                            task["topic"])
            steps.append("local distill: focused report %d chars "
                         "(rows first, short lead)" % len(report))
        result = distill(report, task["topic"])
        if "error" in result:
            status = ("needs-model" if ("no model available" in result["error"]
                                        or "returned no output" in result["error"])
                      else "failed")
            store.finish_task(task_id, status,
                              {"steps": steps, "compiled": compiled,
                               "pages": pages, "distill_error": result["error"]})
            return {"task": task_id, "status": status,
                    "reason": result["error"]}
        created = 0
        for pair in result.get("qa", []):
            store.add_proposal(task_id, "qa", question=pair["q"],
                               answer=pair["a"])
            created += 1
        for f in result.get("facts", []):
            store.add_proposal(task_id, "fact", subject=f["subject"],
                               predicate=f["predicate"], object=f["object"])
            created += 1
        store.finish_task(task_id, "done",
                          {"steps": steps, "compiled": compiled,
                           "pages": pages, "proposals": created,
                           "model_used": result.get("model_used")})
        return {"task": task_id, "status": "done", "proposals": created,
                "model_used": result.get("model_used")}
    except Exception as e:
        store.finish_task(task_id, "failed", {"steps": steps, "error": str(e)})
        return {"task": task_id, "status": "failed", "reason": str(e)}


# ---- heartbeat ---------------------------------------------------------------

def tick(data_dir, ident_hash, cloud=None, **run_kwargs):
    """One heartbeat step: work the next workable task, if any."""
    if not _TICK_LOCK.acquire(blocking=False):
        return {"skipped": "tick already running"}
    try:
        store = Store(data_dir)
        if store.get_meta("worker_enabled", "1") != "1":
            store.set_meta("heartbeat_last", str(time.time()))
            store.set_meta("heartbeat_note", "paused (worker off)")
            return {"skipped": "worker off"}
        task_id = store.next_workable()
        if task_id is None:
            store.set_meta("heartbeat_last", str(time.time()))
            store.set_meta("heartbeat_note", "idle (no workable tasks)")
            return {"idle": True}
        out = run_task(data_dir, ident_hash, task_id, cloud=cloud, **run_kwargs)
        store.set_meta("heartbeat_last", str(time.time()))
        store.set_meta("heartbeat_note",
                       "task %s -> %s" % (task_id, out.get("status", "?")))
        return out
    finally:
        _TICK_LOCK.release()


def worker_loop(data_dir, interval=_TICK_INTERVAL_S):
    """Daemon heartbeat thread: tick, sleep, repeat, forever."""
    from .identity import ensure_identity
    while True:
        try:
            ident, _ = ensure_identity(data_dir)
            tick(data_dir, ident.hash, cloud=server_cloud())
        except Exception:
            pass
        time.sleep(interval)


def status(data_dir):
    store = Store(data_dir)
    bench = store.last_bench()
    return {
        "worker_enabled": store.get_meta("worker_enabled", "1") == "1",
        "heartbeat_last": store.get_meta("heartbeat_last"),
        "heartbeat_note": store.get_meta("heartbeat_note", "never ticked"),
        "task_counts": store.counts(),
        "proposals_pending": store.pending_proposal_count(),
        "last_bench": bench,
    }


# ---- promotion -----------------------------------------------------------------

def _operator_brain_dir(data_dir):
    path = os.path.join(data_dir, "brain")
    os.makedirs(path, exist_ok=True)
    return path


def _ensure_facts_db(brain_dir):
    path = os.path.join(brain_dir, "facts.sqlite3")
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS facts ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " subject TEXT NOT NULL, predicate TEXT NOT NULL,"
        " object TEXT NOT NULL, confidence REAL NOT NULL,"
        " source TEXT NOT NULL, created REAL NOT NULL,"
        " superseded INTEGER NOT NULL DEFAULT 0,"
        " superseded_by INTEGER)")
    db.commit()
    return db


def _append_cassette(brain_dir, question, answer):
    """Append one category to the operator's research cassette.

    Patterns use the ladder's own normalization (uppercased,
    punctuation stripped), so the new category matches the way users
    actually type the question.
    """
    cdir = os.path.join(brain_dir, "cassettes")
    os.makedirs(cdir, exist_ok=True)
    path = os.path.join(cdir, "research-learned.aiml")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!-- research-learned.aiml — operator-approved research.'
                    ' Safe to delete. -->\n'
                    '<aiml version="1.0">\n</aiml>\n')
    pattern = _ceti._norm(question)
    category = ("<category><pattern>%s</pattern><template>%s</template>"
                "</category>\n"
                % (pattern, _xml.escape(answer)))
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if "</aiml>" in content:
        content = content.replace("</aiml>", category + "</aiml>", 1)
    else:
        content = content + category
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _add_memory_node(data_dir, ident_hash, text):
    try:
        from .servers import embed_text
        from . import memory
        emb = embed_text(text)
        if emb is None:
            return False
        conn = memory.connect(data_dir)
        try:
            memory.add(conn, ident_hash, text, emb,
                       source="research", tags="approved", model="RESEARCH")
        finally:
            conn.close()
        return True
    except Exception:
        return False


def approve_proposal(data_dir, ident_hash, proposal_id):
    """Promote one pending proposal into the operator brain."""
    store = Store(data_dir)
    proposal = store.get_proposal(proposal_id)
    if not proposal:
        return {"error": "unknown proposal"}
    if proposal["status"] != "pending":
        return {"error": "proposal already %s" % proposal["status"]}
    brain_dir = _operator_brain_dir(data_dir)
    landed = []
    if proposal["kind"] == "fact":
        db = _ensure_facts_db(brain_dir)
        db.execute(
            "INSERT INTO facts (subject, predicate, object, confidence,"
            " source, created, superseded) VALUES (?,?,?,?,?, ?,0)",
            (proposal["subject"], proposal["predicate"], proposal["object"],
             0.9, "research", time.time()))
        db.commit()
        db.close()
        landed.append("fact store")
        if _add_memory_node(
                data_dir, ident_hash,
                "%s %s %s" % (proposal["subject"], proposal["predicate"],
                              proposal["object"])):
            landed.append("vector memory")
    else:
        path = _append_cassette(brain_dir, proposal["question"],
                                proposal["answer"])
        landed.append("cassette research-learned.aiml")
        if _add_memory_node(
                data_dir, ident_hash,
                "Q: %s A: %s" % (proposal["question"], proposal["answer"])):
            landed.append("vector memory")
    store.set_proposal(proposal_id, "approved", {"landed": landed})
    return {"proposal": proposal_id, "status": "approved", "landed": landed}


def reject_proposal(data_dir, proposal_id):
    store = Store(data_dir)
    proposal = store.get_proposal(proposal_id)
    if not proposal:
        return {"error": "unknown proposal"}
    if proposal["status"] != "pending":
        return {"error": "proposal already %s" % proposal["status"]}
    store.set_proposal(proposal_id, "rejected")
    return {"proposal": proposal_id, "status": "rejected"}


# ---- benchmark -------------------------------------------------------------------

_WORD = re.compile(r"[a-z]{5,}")


def key_terms(answer, k=6):
    words = _WORD.findall((answer or "").lower())
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= k:
            break
    return set(out)


def run_benchmark(data_dir, ident_hash, max_questions=25):
    """Quiz the CETI ladder on its own knowledge QA, like CETI's
    benchmark.py: a hit needs a local tier (facts/aiml/local-model)
    plus at least half the answer's key terms in the reply. A miss
    is a gap, not a failure — the Research tab turns misses into
    research tasks. Runs in a background thread; see web.py."""
    roots = _brain.resolve_roots()
    kpath = os.path.join(roots["ceti_dir"], "knowledge.sqlite3")
    if not os.path.exists(kpath):
        return {"error": "no knowledge.sqlite3 under %s" % roots["ceti_dir"]}
    db = sqlite3.connect(kpath)
    rows = db.execute(
        "SELECT question, answer FROM qa ORDER BY id LIMIT ?",
        (max_questions,)).fetchall()
    db.close()
    t0 = time.time()
    results = []
    tier_dist = {}
    for q, a in rows:
        try:
            r = _ceti.answer(q, ident_hash=ident_hash)
            tier = r.get("tier", "?")
            text = (r.get("answer") or "").lower()
        except Exception as e:
            tier, text = "error", ""
            results.append({"question": q, "tier": tier, "hit": False,
                            "error": str(e)})
            tier_dist[tier] = tier_dist.get(tier, 0) + 1
            continue
        tier_dist[tier] = tier_dist.get(tier, 0) + 1
        terms = key_terms(a)
        hit = (tier in ("facts", "aiml", "local-model")
               and (not terms or len(terms & set(text.split()))
                    >= max(1, len(terms) // 2)))
        results.append({"question": q, "tier": tier, "hit": hit})
    n = len(results)
    hits = sum(1 for r in results if r["hit"])
    summary = {
        "questions": n, "accuracy": round(hits / n, 3) if n else 0.0,
        "tier_dist": tier_dist,
        "misses": [{"question": r["question"], "tier": r["tier"]}
                   for r in results if not r["hit"]],
        "elapsed_s": round(time.time() - t0, 1),
    }
    store = Store(data_dir)
    rid = store.add_bench_run(t0, time.time(), n, summary["accuracy"], summary)
    summary["run_id"] = rid
    return summary


def research_misses(data_dir):
    """Create research tasks from the last benchmark run's misses."""
    store = Store(data_dir)
    bench = store.last_bench()
    if not bench or not bench["summary"]:
        return {"error": "no benchmark run yet"}
    created = []
    for miss in bench["summary"].get("misses", []):
        q = miss.get("question", "")
        if not q:
            continue
        tid = store.add_task("research", q, q)
        if tid not in created:
            created.append(tid)
    return {"created": len(created), "task_ids": created}
