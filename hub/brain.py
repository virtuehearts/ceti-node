"""Brain readers for the node web UI. Stdlib only, read-only.

The Brain pane shows what the node already knows, from files — never
from a model call: AIML cassettes, the fact store, the research queue,
and the BIOS persona. Nothing here writes, trains, or phones home.

Roots resolve in order:
  1. BRAIN_CETI_DIR env (a CETI data dir: cassettes/, persona.md, *.sqlite3)
  2. <HUB_DATA_DIR>/brain/ (the shipped-node layout)
  3. The CETI dev tree beside this checkout (this box), if it holds
     cassettes/ or persona.md
Otherwise every reader reports found=False and the pane says plainly
where it looked, instead of inventing a brain.
"""
import os
import re
import sqlite3

MAX_MODULE_READ = 200_000
MAX_COUNT_READ = 1_000_000

_CAT_RE = re.compile(r"<category[\s>]", re.IGNORECASE)


def _default_candidates():
    from .config import data_dir
    cands = []
    env = os.environ.get("BRAIN_CETI_DIR")
    if env:
        cands.append(env)
    cands.append(os.path.join(data_dir(), "brain"))
    ws = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    cands.append(os.path.join(ws, "ceti", "data"))
    return cands


def resolve_roots(candidates=None):
    """First candidate holding cassettes/ or persona.md wins."""
    looked = []
    for cand in (candidates if candidates is not None
                 else _default_candidates()):
        looked.append(cand)
        if not cand or not os.path.isdir(cand):
            continue
        if (os.path.isdir(os.path.join(cand, "cassettes"))
                or os.path.isfile(os.path.join(cand, "persona.md"))):
            return {"found": True, "ceti_dir": cand,
                    "cassettes_dir": os.path.join(cand, "cassettes"),
                    "persona_file": os.path.join(cand, "persona.md"),
                    "facts_db": os.path.join(cand, "facts.sqlite3"),
                    "tasks_db": os.path.join(cand, "tasks.db"),
                    "research_db": os.path.join(cand, "researcher_log.sqlite3"),
                    "looked": looked}
    return {"found": False, "ceti_dir": None, "cassettes_dir": None,
            "persona_file": None, "facts_db": None, "tasks_db": None,
            "research_db": None, "looked": looked}


def _read_text(path, cap):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(cap + 1)
    except (OSError, UnicodeError):
        return None, False
    return (text[:cap], len(text) > cap)


def bios(roots=None):
    """BIOS card: the persona file, head lines + size. Identity itself
    comes from the existing /api/node/status — this is just the file."""
    roots = roots or resolve_roots()
    if not roots["found"] or not os.path.isfile(roots["persona_file"]):
        return {"found": False, "persona_path": roots["persona_file"]}
    text, _trunc = _read_text(roots["persona_file"], MAX_COUNT_READ)
    if text is None:
        return {"found": False, "persona_path": roots["persona_file"]}
    head = [ln for ln in text.split("\n") if ln.strip()][:3]
    return {"found": True, "persona_path": roots["persona_file"],
            "persona_chars": os.path.getsize(roots["persona_file"]),
            "persona_head": head}


def list_modules(roots=None):
    """Every .aiml under cassettes/, with its category count."""
    roots = roots or resolve_roots()
    out = []
    cdir = roots["cassettes_dir"]
    if not roots["found"] or not cdir or not os.path.isdir(cdir):
        return out
    for base, _dirs, files in os.walk(cdir):
        for fn in sorted(files):
            if not fn.endswith(".aiml"):
                continue
            full = os.path.join(base, fn)
            rel = os.path.relpath(full, cdir).replace(os.sep, "/")
            text, trunc = _read_text(full, MAX_COUNT_READ)
            cats = (len(_CAT_RE.findall(text)) if text is not None else 0)
            out.append({"name": rel, "categories": cats,
                        "bytes": os.path.getsize(full),
                        "truncated": trunc})
    out.sort(key=lambda m: m["name"])
    return out


def read_module(name, roots=None):
    """One cassette as text. Path traversal is refused, not resolved."""
    roots = roots or resolve_roots()
    cdir = roots["cassettes_dir"]
    if not roots["found"] or not cdir:
        raise ValueError("no brain data found")
    if not name or name.startswith("/") or ".." in name.split("/"):
        raise ValueError("invalid module name")
    full = os.path.normpath(os.path.join(cdir, name))
    if os.path.commonpath([full, cdir]) != cdir:
        raise ValueError("invalid module name")
    if not os.path.isfile(full) or not full.endswith(".aiml"):
        raise ValueError("no such module")
    text, trunc = _read_text(full, MAX_MODULE_READ)
    if text is None:
        raise ValueError("module unreadable")
    return {"name": name, "text": text, "truncated": trunc}


def _ro(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def facts(limit=20, roots=None):
    """Latest rows of the CETI fact store (subject, predicate, object)."""
    roots = roots or resolve_roots()
    db = roots["facts_db"]
    if not roots["found"] or not db or not os.path.isfile(db):
        return {"found": False, "count": 0, "rows": []}
    try:
        conn = _ro(db)
        try:
            has = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='facts'").fetchone()
            if not has:
                return {"found": False, "count": 0, "rows": []}
            count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            rows = [{"subject": s, "predicate": p, "object": o,
                     "confidence": c, "source": src}
                    for s, p, o, c, src in conn.execute(
                        "SELECT subject, predicate, object, confidence, "
                        "source FROM facts ORDER BY id DESC LIMIT ?",
                        (max(1, min(100, limit)),))]
            return {"found": True, "count": count, "rows": rows}
        finally:
            conn.close()
    except sqlite3.Error:
        return {"found": False, "count": 0, "rows": []}


def research(roots=None):
    """Research queue status: tasks by state, open gaps, latest log rows."""
    roots = roots or resolve_roots()
    out = {"found": False, "tasks": [], "task_states": {},
           "gaps": 0, "gaps_list": [], "log": []}
    db = roots["tasks_db"]
    if roots["found"] and db and os.path.isfile(db):
        try:
            conn = _ro(db)
            try:
                if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='tasks'").fetchone():
                    out["found"] = True
                    for st, n in conn.execute(
                            "SELECT status, COUNT(*) FROM tasks "
                            "GROUP BY status"):
                        out["task_states"][st] = n
                    out["tasks"] = [
                        {"kind": k, "title": t, "status": s}
                        for k, t, s in conn.execute(
                            "SELECT kind, title, status FROM tasks "
                            "ORDER BY updated DESC LIMIT 5")]
                if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='gaps'").fetchone():
                    out["gaps"] = conn.execute(
                        "SELECT COUNT(*) FROM gaps").fetchone()[0]
                    out["gaps_list"] = [
                        {"question": q, "count": n}
                        for q, n in conn.execute(
                            "SELECT question, count FROM gaps "
                            "ORDER BY count DESC, last_seen DESC LIMIT 10")]
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    rdb = roots["research_db"]
    if roots["found"] and rdb and os.path.isfile(rdb):
        try:
            conn = _ro(rdb)
            try:
                if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='log'").fetchone():
                    out["found"] = True
                    out["log"] = [
                        {"topic": t, "finished": f, "qa_added": q}
                        for t, f, q in conn.execute(
                            "SELECT topic, finished, qa_added FROM log "
                            "ORDER BY rowid DESC LIMIT 5")]
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return out


def summary(roots=None):
    """Cheap counts for the Brain console header. Lists have their own
    endpoints; this one never dumps file contents."""
    roots = roots or resolve_roots()
    mods = list_modules(roots)
    f = facts(1, roots)
    r = research(roots)
    b = bios(roots)
    lib_files = 0
    lib_dir = os.path.join(roots["ceti_dir"], "library") if roots["ceti_dir"] else None
    if lib_dir and os.path.isdir(lib_dir):
        for _dirpath, _dirnames, filenames in os.walk(lib_dir):
            lib_files += len(filenames)
    qa_count = 0
    kdb = os.path.join(roots["ceti_dir"], "knowledge.sqlite3") if roots["ceti_dir"] else None
    if kdb and os.path.isfile(kdb):
        try:
            conn = _ro(kdb)
            try:
                if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='qa'").fetchone():
                    qa_count = conn.execute("SELECT COUNT(*) FROM qa").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return {
        "found": roots["found"],
        "ceti_dir": roots["ceti_dir"],
        "looked": roots["looked"],
        "bios": {"found": b["found"],
                 "persona_chars": b.get("persona_chars", 0),
                 "persona_head": b.get("persona_head", [])},
        "aiml": {"files": len(mods),
                 "categories": sum(m["categories"] for m in mods)},
        "memory": {"facts": f["count"]},
        "library": {"files": lib_files},
        "knowledge": {"qa": qa_count},
        "research": {"task_states": r["task_states"],
                     "gaps": r["gaps"],
                     "last_topic": r["log"][0]["topic"] if r["log"] else None},
    }
