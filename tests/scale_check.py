"""Scale sanity: retrieval + timeline-walk latency vs store size.

Seeds a synthetic temporal store (fake 8-dim embeddings, no servers)
and times, per store size:
  - full-scan nearest (brute cosine over ALL events, today's approach)
  - day-slice lookup (one day_key via sqlite index)
  - link walk from an entry event (indexed edge lookups)
  - timeline_section (no-embed path: recent events + arcs + flags)

Run from the repo root:  python3 tests/scale_check.py
Prints a small table; no assertions on absolute ms (box CPU varies),
the point is the scaling shape: full scan grows linearly, day slice
+ walk stays flat.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.temporal import TemporalMemory


def fake_emb(i):
    # 8-dim deterministic vector, topic varies slowly so links exist.
    base = [(i % 7) + 1.0, ((i // 7) % 5) + 1.0, 1.0, 0.5, 0.25, 0.125, 0.0625, 0.03125]
    return base


def bench(n_events, events_per_day=10):
    tmp = tempfile.TemporaryDirectory()
    os.environ["HUB_DATA_DIR"] = tmp.name
    tm = TemporalMemory("scale-story", identity_hash="scale-ident")
    # Seed without per-event linking cost first (links_pending), then
    # link a thin chain manually so the walk has edges to follow.
    t0 = time.perf_counter()
    for start in range(0, n_events, events_per_day):
        day = f"block-{start // events_per_day:03d}"
        lines = [f"Event {start + j} happens near the vault." for j in range(events_per_day)]
        tm.record_events(day, lines, embed_fn=None)
    seed_ms = (time.perf_counter() - t0) * 1000

    # Full-scan nearest: cosine over every stored embedding.
    # (Events were stored without embeddings, so embed them in a
    # scratch pass here to simulate a populated store.)
    from hub import temporal as tmod
    from hub.config import data_dir as _dd
    conn = tmod.connect(_dd())
    rows = conn.execute("SELECT id FROM temporal_events WHERE story_id = 'scale-story'").fetchall()
    for (eid,) in rows:
        conn.execute("UPDATE temporal_events SET embedding = ? WHERE id = ?",
                     (tmod._blob(fake_emb(eid)), eid))
    conn.commit()
    # Link each event to the previous one (a chain) to simulate links.
    ids = [r[0] for r in rows]
    for a, b in zip(ids, ids[1:]):
        conn.execute("INSERT OR IGNORE INTO temporal_links (identity_hash, story_id, event_id, linked_event_id, score, created_at) VALUES ('scale-ident', 'scale-story', ?, ?, 0.9, 0)",
                     (a, b))
    conn.commit()

    q = fake_emb(ids[len(ids) // 2])
    t0 = time.perf_counter()
    tm.nearest_events(q, top_k=3)
    full_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    tm.day_events(f"block-{(n_events // events_per_day) // 2:03d}")
    day_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    tm.walk([ids[len(ids) // 2]], max_hops=2)
    walk_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    section = tm.timeline_section("Jake opens the vault.")
    section_ms = (time.perf_counter() - t0) * 1000
    tmp.cleanup()
    return {"n": n_events, "seed_ms": seed_ms, "full_scan_ms": full_ms,
            "day_slice_ms": day_ms, "walk_ms": walk_ms,
            "timeline_ms": section_ms, "section_chars": len(section)}


if __name__ == "__main__":
    print(f"{'events':>7} {'seed_ms':>9} {'fullscan':>9} {'day_slice':>9} {'walk':>8} {'timeline':>9} {'section_chars':>13}")
    for n in (100, 1000, 3000):
        r = bench(n)
        print(f"{r['n']:>7} {r['seed_ms']:>9.1f} {r['full_scan_ms']:>9.2f} {r['day_slice_ms']:>9.2f} {r['walk_ms']:>8.2f} {r['timeline_ms']:>9.2f} {r['section_chars']:>13}")
