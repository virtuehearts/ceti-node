"""Live A/B: does the temporal timeline section reduce continuity
violations in short continuations? No generation in unit tests —
this script uses the real local Ollama model (qwen2.5-coder:1.5b on
:11434) and the real embed server (:11435) for seeding only.

Planted facts (seeded as temporal events + shared canon):
  1. DEATH: Lena dies in the vault.
  2. KNOWLEDGE: Jake learns the vault code.
  3. POSSESSION: Jake takes the brass key from the blue box.
  4. OPEN THREAD: The relay mast signal stays unanswered.

Variants (same bible/canon/retrieved/beat, only the timeline differs):
  V0  baseline: no timeline section.
  V1a sequence-order only (no story-day, no arcs, no flag).
  V1b sequence + story-day labels (no arcs, no flag).
  V1c full timeline section (flags + arcs + sequence + story-day).

Usage:  python3 tests/ab_experiment.py --beat 1 --out ~/workspace/temporal-ab-outputs/beat1.jsonl
Scoring is done by a human against the fixed checklist in the report
(violated / upheld / not mentioned per planted fact); this script
only generates and saves raw outputs + metrics.
"""
import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BIBLE = ("The Vault Beneath. Jake, a repairman, explores a strange vault "
         "beneath the city with Lena, his partner. Lena dies in the vault. "
         "Jake learns the vault code and takes the brass key from the blue box. "
         "A relay mast signal from the vault stays unanswered.")
CANON = ("- Lena is dead (died in the vault).\n"
         "- Jake knows the vault code.\n"
         "- Jake carries the brass key (taken from the blue box).\n"
         "- The relay mast signal is unanswered (open thread).")
RETRIEVED = ("[past chapter 001] Lena and Jake enter the vault together.\n\n"
             "---\n\n[past chapter 002] Jake learns the vault code and takes "
             "the brass key from the blue box.\n\n---\n\n[past chapter 003] "
             "The relay mast signal stays unanswered.")
BEATS = {
    1: "Lena argues with Jake about the vault code, and Jake tries the brass key on the vault door.",
    2: "Jake tries the brass key on the vault door and listens for the relay mast signal.",
}
EVENTS = [
    ("block-001", ["Lena dies in the vault."], "day-1"),
    ("block-002", ["Jake learns the vault code.", "Jake takes the brass key from the blue box."], "day-2"),
    ("block-003", ["The relay mast signal stays unanswered."], "day-3"),
]


def ollama_chat(system, user, max_tokens=500, temperature=0.7, seed=42):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    payload = json.dumps({
        "model": "qwen2.5-coder:1.5b",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "options": {"temperature": temperature, "seed": seed, "num_predict": max_tokens},
    }).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=payload,
                                 headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with opener.open(req, timeout=600) as r:
        data = json.load(r)
    dt = time.perf_counter() - t0
    return data["message"]["content"], data.get("eval_count", 0), dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--beat", type=int, choices=[1, 2], required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    tmp = tempfile.TemporaryDirectory()
    os.environ["HUB_DATA_DIR"] = tmp.name
    from hub.servers import embed_text
    from hub.temporal import TemporalMemory

    stores = {}
    for sid, use_day in (("ab-noday", False), ("ab-day", True)):
        tm = TemporalMemory(sid, identity_hash="ab-ident")
        for day_key, lines, sday in EVENTS:
            tm.record_events(day_key, lines, embed_fn=embed_text,
                             story_day=sday if use_day else None)
        stores[sid] = tm
    # Sanity: real embeddings linked the vault events.
    from hub import temporal as tmod
    conn = tmod.connect(tmp.name)
    n_links = conn.execute("SELECT COUNT(*) FROM temporal_links").fetchone()[0]
    print(f"seeded links (both stores): {n_links}")

    beat = BEATS[args.beat]
    variants = [
        ("V0-baseline", None),
        ("V1a-sequence-only", stores["ab-noday"].timeline_section(
            beat, max_chars=1200, embed_fn=embed_text, include_arcs=False, include_flags=False)),
        ("V1b-sequence-storyday", stores["ab-day"].timeline_section(
            beat, max_chars=1200, embed_fn=embed_text, include_arcs=False, include_flags=False)),
        ("V1c-full", stores["ab-day"].timeline_section(
            beat, max_chars=1200, embed_fn=embed_text)),
    ]
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for name, section in variants:
            user = (f"STORY BIBLE:\n{BIBLE}\n\nCANON — do not contradict this:\n{CANON}\n\n"
                    f"RETRIEVED PAST CHAPTERS:\n{RETRIEVED}\n\n")
            if section:
                user += f"TIMELINE — events in order (do not contradict this sequence):\n{section}\n\n"
            user += (f"Write the next chapter: {beat}\nAbout 300-400 words. "
                     f"Continue seamlessly. No preamble, no meta-commentary — just the chapter.")
            text, tokens, dt = ollama_chat("You are a novelist writing an ORIGINAL novel. Write vivid, concrete prose.", user)
            rec = {"variant": name, "beat": args.beat, "prompt_chars": len(user),
                   "output_tokens": tokens, "seconds": round(dt, 1),
                   "tok_per_s": round(tokens / dt, 1) if dt else 0,
                   "timeline_chars": len(section) if section else 0, "output": text}
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print(f"{name}: prompt={len(user)} chars timeline={rec['timeline_chars']} "
                  f"tokens={tokens} time={dt:.1f}s ({rec['tok_per_s']} tok/s)")
            print(f"  preview: {text[:200]!r}")
    tmp.cleanup()


if __name__ == "__main__":
    main()
