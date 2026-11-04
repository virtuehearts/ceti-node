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
- distill: local chat server first, cloud fallback, mirroring the
  original's local-first order. Output is QA pairs + fact triples,
  parsed best-effort (a small local model is stochastic).
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

from . import brain as _brain
from . import ceti_chat as _ceti
from .config import data_dir as _data_dir
from .lynx import fetch_text as _fetch_text

_TICK_LOCK = threading.Lock()
_TICK_INTERVAL_S = 60
_MAX_PAGES = 5
_REPORT_CHARS = 8000

DISTILL_PROMPT = """Extract up to 8 self-contained question/answer pairs AND up to 8
fact triples from the report below.
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

    def claim_task(self, task_id):
        db = self._db()
        cur = db.execute(
            "UPDATE tasks SET status='running', attempts=attempts+1,"
            " updated=? WHERE id=? AND status IN ('pending','needs-model')",
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
    answer, _reasoning = chat_complete(
        [{"role": "system", "content": system},
         {"role": "user", "content": user}],
        max_tokens=1500, temperature=0.2)
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


def distill_text(report_text, topic, cloud):
    """Local chat server first, cloud fallback. Returns dict or error."""
    prompt = DISTILL_PROMPT % (report_text[:_REPORT_CHARS])
    raw = None
    model_used = None
    local_down = False
    try:
        raw = _local_complete(prompt)
        model_used = "local"
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
    if raw is None and cloud is not None:
        try:
            raw = _cloud_complete(
                cloud, "Reply with ONLY the requested JSON object.", prompt)
            model_used = cloud["provider"] + "/" + cloud["model"]
        except Exception as e:
            return {"error": "distill failed: %s" % e}
        if not (raw or "").strip():
            return {"error": "cloud model returned no output"}
    if raw is None:
        if local_down:
            return {"error": "no model available (local chat down, no cloud key)"}
        return {"error": "local model returned no output (no cloud key to fall back to)"}
    out = _extract_distill(raw)
    out["model_used"] = model_used
    return out


# ---- task work --------------------------------------------------------------

def run_task(data_dir, ident_hash, task_id, cloud=None,
             distill_fn=None, fetch_fn=None):
    """Work one research task to proposals. Fail-soft throughout."""
    store = Store(data_dir)
    task = store.get_task(task_id)
    if not task:
        return {"error": "unknown task"}
    if not store.claim_task(task_id):
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
            urls = suggest_urls(task["topic"], cloud)
            steps.append("suggested %d source URLs" % len(urls))
        else:
            steps.append("using %d supplied URLs" % len(urls))
        pages = []
        texts = []
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
                texts.append((page.get("title", "") + "\n" + text)[:4000])
        if not texts:
            store.finish_task(task_id, "failed",
                              {"steps": steps, "compiled": compiled,
                               "pages": pages})
            return {"task": task_id, "status": "failed",
                    "reason": "no pages fetched"}
        report = "\n\n".join(texts)
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
