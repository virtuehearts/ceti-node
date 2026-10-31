"""Unified vector memory: one sqlite DB, filtered by identity hash.

Every memory is a row: identity_hash, text, embedding (float32 blob),
created_at, source (chat / remember / note / ...), tags, model (the
local model that wrote it — the active chat model name, e.g. the
catalog name of the running model; '' when unknown, which is the case
for every row written before the model column existed). Retrieval is
brute-force cosine over that identity's rows only, which is fast enough
for a personal memory (thousands of rows) and needs no extension, no
server and no extra dependency. A different identity.dat means a
different identity_hash, so that memory is simply not returned.

Provenance: memory_usage links a memory to the assistant reply that
used it (reply_memory_id is the id of the "Assistant: ..." row saved
for that exchange), so any output traces to its memories and any
memory traces to the outputs that used it.
"""
import array
import math
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identity_hash TEXT NOT NULL,
  text TEXT NOT NULL,
  embedding BLOB NOT NULL,
  created_at REAL NOT NULL,
  source TEXT NOT NULL DEFAULT 'chat',
  tags TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mem_identity ON memories(identity_hash);
CREATE TABLE IF NOT EXISTS memory_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id INTEGER NOT NULL,
  reply_memory_id INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_memory ON memory_usage(memory_id);
"""


def db_path(data_dir: str) -> str:
    return os.path.join(data_dir, "memory.db")


def connect(data_dir: str):
    os.makedirs(data_dir, exist_ok=True)
    conn = sqlite3.connect(db_path(data_dir))
    conn.executescript(SCHEMA)
    # Migration for databases created before the model column: the
    # CREATE TABLE above is IF NOT EXISTS, so old tables keep their
    # old shape unless we add the column explicitly.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    if "model" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN model TEXT NOT NULL DEFAULT ''")
        conn.commit()
    return conn


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def add(conn, identity_hash: str, text: str, embedding, source="chat", tags="", model=""):
    blob = array.array("f", embedding).tobytes()
    cur = conn.execute(
        "INSERT INTO memories (identity_hash, text, embedding, created_at, source, tags, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (identity_hash, text, blob, time.time(), source, tags, model),
    )
    conn.commit()
    return cur.lastrowid


def count(conn, identity_hash: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM memories WHERE identity_hash = ?", (identity_hash,)).fetchone()
    return row[0] if row else 0


def search(conn, identity_hash: str, query_embedding, top_k=5, sources=None):
    """Return [(score, text, source, id, model), ...] for this identity only, best first.
    sources, if given, restricts to those exact source strings (used for
    sample selection: only the samples the user ticked are used as refs)."""
    if sources:
        placeholders = ",".join("?" for _ in sources)
        rows = conn.execute(
            f"SELECT id, text, embedding, source, model FROM memories WHERE identity_hash = ? AND source IN ({placeholders})",
            (identity_hash, *sources)).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, text, embedding, source, model FROM memories WHERE identity_hash = ?", (identity_hash,)
        ).fetchall()
    scored = []
    for rid, text, blob, source, model in rows:
        vec = array.array("f")
        vec.frombytes(blob)
        scored.append((_cosine(query_embedding, vec), text, source, rid, model))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_k]


def forget_all(conn, identity_hash: str) -> int:
    cur = conn.execute("DELETE FROM memories WHERE identity_hash = ?", (identity_hash,))
    # Usage rows point at reply memories; drop the ones left orphaned.
    conn.execute("DELETE FROM memory_usage WHERE reply_memory_id NOT IN (SELECT id FROM memories)")
    conn.commit()
    return cur.rowcount


def get(conn, identity_hash: str, mid: int):
    """Return one memory row as a dict (with the decoded vector) or None.

    Only rows belonging to this identity are ever returned. The dict
    carries exactly the record's own fields — no identity hash, no keys.
    """
    row = conn.execute(
        "SELECT id, text, source, created_at, embedding, model FROM memories WHERE id = ? AND identity_hash = ?",
        (mid, identity_hash),
    ).fetchone()
    if not row:
        return None
    vec = array.array("f")
    vec.frombytes(row[4])
    return {"id": row[0], "text": row[1], "source": row[2], "created_at": row[3], "vector": list(vec), "model": row[5]}


def recent(conn, identity_hash: str, limit=24):
    """Most recent memories for this identity, newest first.

    Returns dicts with decoded vectors (needed server-side to compute
    constellation edges); callers that only need previews should drop
    the vector before serializing.
    """
    rows = conn.execute(
        "SELECT id, text, source, created_at, embedding, model FROM memories WHERE identity_hash = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (identity_hash, limit),
    ).fetchall()
    out = []
    for rid, text, source, created_at, blob, model in rows:
        vec = array.array("f")
        vec.frombytes(blob)
        out.append({"id": rid, "text": text, "source": source, "created_at": created_at, "vector": list(vec), "model": model})
    return out


def log_usage(conn, memory_id: int, reply_memory_id: int):
    """Record that one assistant reply used one memory. Returns the row id."""
    cur = conn.execute(
        "INSERT INTO memory_usage (memory_id, reply_memory_id, created_at) VALUES (?, ?, ?)",
        (memory_id, reply_memory_id, time.time()),
    )
    conn.commit()
    return cur.lastrowid


def used_by(conn, memory_id: int, limit=20):
    """Replies that used this memory, newest first.

    Returns [{id, text_preview, created_at}] for the assistant reply
    rows — the reverse half of the trace: a memory lists the outputs
    that drew on it.
    """
    rows = conn.execute(
        "SELECT m.id, m.text, u.created_at FROM memory_usage u "
        "JOIN memories m ON m.id = u.reply_memory_id "
        "WHERE u.memory_id = ? ORDER BY u.created_at DESC, u.id DESC LIMIT ?",
        (memory_id, limit),
    ).fetchall()
    return [{"id": rid, "text_preview": txt[:200], "created_at": ts} for rid, txt, ts in rows]
