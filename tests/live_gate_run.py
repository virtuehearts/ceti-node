"""Small live run of the beat-gate end to end (real Ollama model,
real embed server where used). NOT a unit test — run by hand:

    HUB_DATA_DIR=/tmp/gatelive HUB_TEMPORAL=1 python3 tests/live_gate_run.py

Uses a temp data dir only. Never touches the real data/ tree.
Writes results JSON to ~/workspace/beat-gate-live-outputs/live_run.json.

Cases:
  A. Violating beat -> next_block returns blocked in milliseconds
     (gate runs before any GPU call; no generation happens).
  B. Clean beat -> next_block generates for real (pages=1, short) and
     is accepted, or is honestly reported as flagged if the model
     stages a violation anyway (that is the output check working).
  C. Deterministic output check on hand-written violating prose
     (no model): flagged, not accepted.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["HUB_TEMPORAL"] = "1"
TMP = tempfile.mkdtemp(prefix="gatelive-")
os.environ["HUB_DATA_DIR"] = TMP

from hub.servers import embed_text  # noqa: E402
from hub.story import StoryWriter  # noqa: E402
from hub.temporal import TemporalMemory  # noqa: E402

OUT_DIR = os.path.expanduser("~/workspace/beat-gate-live-outputs")
os.makedirs(OUT_DIR, exist_ok=True)

STORY_ID = "live-gate"
d = os.path.join(TMP, "stories", STORY_ID)
os.makedirs(os.path.join(d, "blocks"))
with open(os.path.join(d, "bible.md"), "w", encoding="utf-8") as f:
    f.write("# Test\n\n1. Lena argues with Jake about the vault code.\n2. Jake opens the vault.\n")
with open(os.path.join(d, "summary.md"), "w", encoding="utf-8") as f:
    f.write("(no chapters written yet)\n")
with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
    f.write('{"genre": "mystery", "storyline": "vault", "perspective": "third person limited", "plot": "p", "rating": "PG-13"}')

results = {"tmp": TMP, "cases": {}}

# Seed: dead Lena + Jake's key + vault, with real embeddings when
# the server answers (falls back to None — the gate is rules-only).
tm = TemporalMemory(STORY_ID, identity_hash="")
emb_ok = embed_text("probe") is not None
seed = tm.record_events("block-001",
                         ["Lena dies in the vault.",
                          "Jake takes the brass key.",
                          "Jake arrives at the vault."],
                         embed_fn=embed_text if emb_ok else None)
results["embed_server_up"] = emb_ok
results["seed"] = seed
print(f"embed server up: {emb_ok}, seeded: {seed}")

w = StoryWriter(ident=None)

# Case A: violating beat (bible beat 1 stages dead Lena).
t0 = time.time()
rA = w.next_block(STORY_ID, pages=1)
dtA = time.time() - t0
results["cases"]["A_blocked_beat"] = {
    "blocked": rA.get("blocked"), "violations": rA.get("violations"),
    "seconds": round(dtA, 2),
    "block_file_exists": os.path.exists(os.path.join(d, "blocks", "01.md")),
}
print(f"A: blocked={rA.get('blocked')} in {dtA:.2f}s, "
      f"violations={len(rA.get('violations', []))}, file_written={os.path.exists(os.path.join(d, 'blocks', '01.md'))}")

# Case B: clean beat. Rewrite the bible so block 1's beat is clean
# (block 1 was never written — the gate blocked it — so block 1 is
# still next; reuse it with a clean beat).
with open(os.path.join(d, "bible.md"), "w", encoding="utf-8") as f:
    f.write("# Test\n\n1. Jake opens the vault.\n2. Jake leaves the vault.\n")
t0 = time.time()
rB = w.next_block(STORY_ID, pages=1)
dtB = time.time() - t0
results["cases"]["B_clean_beat_live"] = {
    "blocked": rB.get("blocked"), "accepted": rB.get("accepted"),
    "words": rB.get("words"), "path": rB.get("path"),
    "flagged_draft": rB.get("flagged_draft"),
    "violations": rB.get("violations"), "seconds": round(dtB, 2),
}
print(f"B: blocked={rB.get('blocked')} accepted={rB.get('accepted')} "
      f"words={rB.get('words')} in {dtB:.1f}s draft={rB.get('flagged_draft')}")

# Case C: deterministic output check, no model.
rC = tm.check_prose("Lena walked into the vault and said hello to Jake.")
results["cases"]["C_violating_prose_check"] = {
    "blocked": rC["blocked"], "violations": rC["violations"],
}
print(f"C: violating prose blocked={rC['blocked']} violations={len(rC['violations'])}")

out_path = os.path.join(OUT_DIR, "live_run.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)
print("wrote", out_path)
