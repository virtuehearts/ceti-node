"""Uncensored Hub CLI. Stdlib only. Data lives in data/ (gitignored)."""
import argparse
import os
import sys

from .config import data_dir, models_dir, load_catalog, chat_port, embed_port, effective_chat_model
from .identity import ensure_identity
from .llama import doctor
from .download import download_hf
from . import memory
from .servers import ensure_servers, stop_all, embed_text, _health
from .chat import run_chat


def _catalog_map():
    cat = load_catalog()
    return {m["id"]: m for m in cat["models"]}


def _model_path(m):
    return os.path.join(models_dir(), m["file"])


def cmd_init(_args):
    ident, existed = ensure_identity(data_dir())
    print(f"identity: {ident.fingerprint}")
    print(f"node_id: {ident.node_id}")
    print(f"identity_hash: {ident.hash}")
    print(f"identity.dat: {os.path.join(data_dir(), 'identity.dat')} ({'loaded' if existed else 'generated, mode 0600'})")
    print("Back up identity.dat like a key. Memory in data/memory.db is bound to this hash only.")


def cmd_doctor(args):
    doctor(install=args.install)


def cmd_models(_args):
    for m in load_catalog()["models"]:
        path = _model_path(m)
        have = os.path.exists(path) and os.path.getsize(path) == m["size"]
        print(f"{m['id']:20} {'[downloaded]' if have else '[missing]':14} {m['name']} ({m['hf_repo']})")


def cmd_pull(args):
    cat = _catalog_map()
    names = args.models or [m["id"] for m in cat.values() if m.get("default")]
    for name in names:
        if name not in cat:
            print(f"unknown model: {name} (see: python -m hub models)")
            continue
        m = cat[name]
        download_hf(m["hf_repo"], m["file"], _model_path(m), size=m.get("size"), sha256=m.get("sha256"))


def cmd_serve(args):
    ident, _ = ensure_identity(data_dir())
    print(f"identity: {ident.fingerprint}")
    cat = _catalog_map()
    chat_eff = effective_chat_model()
    chat_m = chat_eff if chat_eff else cat.get("ministral-uncensored")
    embed_m = cat.get("embeddinggemma")
    chat_path = _model_path(chat_m) if chat_m else None
    embed_path = _model_path(embed_m) if embed_m else None
    if chat_path and not os.path.exists(chat_path):
        print("chat model missing; downloading it first ...")
        download_hf(chat_m["hf_repo"], chat_m["file"], chat_path, size=chat_m.get("size"), sha256=chat_m.get("sha256"))
    if embed_path and not os.path.exists(embed_path):
        print("embedding model missing; downloading it first ...")
        download_hf(embed_m["hf_repo"], embed_m["file"], embed_path, size=embed_m.get("size"), sha256=embed_m.get("sha256"))
    gpu = not (args.cpu or args.ram)
    chat_ok, embed_ok = ensure_servers(chat_path, embed_path, gpu=gpu, ctx=args.ctx, embed_gpu=args.embed_gpu)
    print(f"chat server: http://127.0.0.1:{chat_port()} ({'up' if chat_ok else 'DOWN'})")
    print(f"embed server: http://127.0.0.1:{embed_port()} ({'up' if embed_ok else 'DOWN'})")
    if not chat_ok:
        print("chat server did not come up; check data/server-*.log")
        return
    print("Servers are running in the background. Stop them with: python -m hub stop")
    print("Chat in another terminal with: python -m hub chat")


def cmd_stop(_args):
    chat_stopped, embed_stopped = stop_all()
    print(f"chat stopped: {chat_stopped}, embed stopped: {embed_stopped}")


def cmd_chat(args):
    ident, _ = ensure_identity(data_dir())
    conn = memory.connect(data_dir())
    if args.auto_serve:
        cat = _catalog_map()
        chat_path = _model_path(cat["ministral-uncensored"])
        embed_path = _model_path(cat["embeddinggemma"])
        if not os.path.exists(chat_path):
            download_hf(cat["ministral-uncensored"]["hf_repo"], cat["ministral-uncensored"]["file"], chat_path, size=cat["ministral-uncensored"].get("size"), sha256=cat["ministral-uncensored"].get("sha256"))
        if not os.path.exists(embed_path):
            download_hf(cat["embeddinggemma"]["hf_repo"], cat["embeddinggemma"]["file"], embed_path, size=cat["embeddinggemma"].get("size"), sha256=cat["embeddinggemma"].get("sha256"))
        chat_ok, embed_ok = ensure_servers(chat_path, embed_path, gpu=not (args.cpu or args.ram), ctx=args.ctx, embed_gpu=args.embed_gpu)
        print(f"servers: chat {'up' if chat_ok else 'DOWN'}, embed {'up' if embed_ok else 'DOWN'}")
    run_chat(ident, conn, once=args.once)


def cmd_remember(args):
    ident, _ = ensure_identity(data_dir())
    conn = memory.connect(data_dir())
    emb = embed_text(args.text)
    if emb is None:
        print("embedding server not running; start it with: python -m hub serve")
        return
    rid = memory.add(conn, ident.hash, args.text, emb, source=args.source, tags=args.tags)
    print(f"remembered as id {rid} for identity {ident.fingerprint}")


def cmd_recall(args):
    ident, _ = ensure_identity(data_dir())
    conn = memory.connect(data_dir())
    emb = embed_text(args.query)
    if emb is None:
        print("embedding server not running; start it with: python -m hub serve")
        return
    hits = memory.search(conn, ident.hash, emb, top_k=args.top)
    if not hits:
        print("no memories for this identity yet")
        return
    for score, text, source, rid, mdl in hits:
        print(f"[{score:.3f}] id={rid} ({source}{', ' + mdl if mdl else ''}) {text}")


def cmd_status(_args):
    ident, existed = ensure_identity(data_dir())
    conn = memory.connect(data_dir())
    print(f"identity: {ident.fingerprint} ({ident.node_id}, {'existing' if existed else 'new'})")
    print(f"chat server: {'up' if _health(chat_port()) else 'down'} (port {chat_port()})")
    print(f"embed server: {'up' if _health(embed_port()) else 'down'} (port {embed_port()})")
    print(f"memories for this identity: {memory.count(conn, ident.hash)}")
    cmd_models(_args)


def _print_gate_outcome(r, unit):
    """Print gate violations/warnings from a story result. Returns
    True when the run must STOP (blocked beat or flagged output) —
    callers return without compiling or printing success totals.
    Safe on toggle-off results (no gate keys at all)."""
    if r.get("blocked"):
        print(f"GATE BLOCKED — no generation ran for {unit}:")
        for v in r.get("violations", []):
            print(f"  ! {v}")
        for w in r.get("warnings", []):
            print(f"  ~ {w}")
        print("  Rewrite the beat, or re-run with --force to override.")
        return True
    if r.get("accepted") is False:
        print(f"GATE FLAGGED — output held as draft, NOT compiled ({unit}):")
        for v in r.get("violations", []):
            print(f"  ! {v}")
        for w in r.get("warnings", []):
            print(f"  ~ {w}")
        print(f"  draft: {r.get('flagged_draft')}")
        print("  Review it, or re-run with --force to accept.")
        return True
    gate = r.get("gate") or {}
    for w in list((gate.get("beat") or {}).get("warnings", [])) + list((gate.get("output") or {}).get("warnings", [])):
        print(f"  ~ gate warning: {w}")
    if gate.get("note"):
        print(f"  ~ gate note: {gate['note']}")
    return False


def cmd_story(args):
    from .story import StoryWriter
    ident, _ = ensure_identity(data_dir())
    w = StoryWriter(ident=ident)
    if args.story_cmd == "list":
        stories = w.list_stories()
        print("stories:" if stories else "no stories yet")
        for s in stories:
            st = w.status(s)
            print(f"  {s} — {st['blocks']} blocks, {st['words']} words, ~{st['est_tokens']} tokens ({st['pct_of_256k']}% of 256k)")
        return
    if args.story_cmd == "new":
        outline = args.outline or ""
        if args.outline_file:
            with open(args.outline_file, encoding="utf-8") as f:
                outline = f.read()
        plot = args.plot or ""
        if args.plot_file:
            with open(args.plot_file, encoding="utf-8") as f:
                plot = f.read()
        sid = w.new_story(args.premise, title=args.title, genre=args.genre or "",
                          storyline=args.storyline or args.premise,
                          perspective=args.perspective or "",
                          plot=plot, outline=outline, rating=args.rating or "")
        print(f"story created: {sid}")
        print(f"dir: {os.path.join(data_dir(), 'stories', sid)}")
        print("write the first block with: python -m hub story write " + sid)
        return
    if args.story_cmd == "chapter":
        for part in range(1, args.parts + 1):
            # Resumable loop: a part already on disk (crash/restart) is
            # kept, not regenerated — the loop continues, it does not restart.
            part_path = os.path.join(data_dir(), "stories", args.story_id, "chapters", f"ch{args.chapter:02d}", f"part{part:02d}.md")
            if os.path.exists(part_path):
                with open(part_path, encoding="utf-8") as f:
                    existing = f.read()
                if len(existing.split()) > 100:
                    print(f"chapter {args.chapter} part {part}: already exists ({len(existing.split())} words) — kept, not regenerated", flush=True)
                    continue
            r = w.write_chapter_part(args.story_id, args.chapter, part, args.parts, pages=args.pages, force=args.force)
            if _print_gate_outcome(r, f"chapter {args.chapter} part {part}"):
                return
            print(f"chapter {r['chapter']} part {r['part']}: {r['words']} words -> {r['path']}", flush=True)
        c = w.compile_chapter(args.story_id, args.chapter)
        print(f"chapter {c['chapter']} compiled: {c['parts']} parts, {c['words']} words, {c['pages']} pages -> {c['path']}")
        return
    if args.story_cmd == "write":
        for _ in range(args.count):
            r = w.next_block(args.story_id, pages=args.pages, force=args.force)
            if _print_gate_outcome(r, f"block {r['block']}"):
                return
            print(f"block {r['block']}: {r['words']} words -> {r['path']}")
        st = w.status(args.story_id)
        print(f"total: {st['blocks']} blocks, {st['words']} words, ~{st['est_tokens']} tokens ({st['pct_of_256k']}% of 256k)")
        return
    if args.story_cmd == "status":
        st = w.status(args.story_id)
        print(f"{st['story_id']}: {st['blocks']} blocks, {st['words']} words, {st['pages']} pages, ~{st['est_tokens']} tokens ({st['pct_of_256k']}% of 256k)")
        print(f"dir: {st['dir']}")
        return
    if args.story_cmd == "compile":
        r = w.compile(args.story_id)
        print(f"compiled: {r['chapters']} chapters, {r['words']} words")
        print(f"  {r['md']}")
        print(f"  {r['txt']}")
        return
    if args.story_cmd == "gate":
        # Diagnostic only: checks a beat without generating. Works
        # regardless of the HUB_TEMPORAL toggle — asking explicitly
        # for a check is itself the human's decision.
        from .temporal import TemporalMemory
        beats = w._beats(args.story_id)
        n = w._block_no(args.story_id)
        beat = args.beat or (beats[n - 1] if n - 1 < len(beats) else "continue the story")
        r = TemporalMemory(args.story_id, ident=ident).check_beat(beat)
        print(f"beat (block {n}): {beat}")
        for v in r["violations"]:
            print(f"  ! {v}")
        for wmsg in r["warnings"]:
            print(f"  ~ {wmsg}")
        if r["blocked"]:
            print("BLOCKED — this beat would not generate without --force.")
        elif r["warnings"]:
            print("WARNINGS only — this beat would generate.")
        else:
            print("CLEAN — no gate violations.")
        return


def cmd_tui(args):
    ident, _ = ensure_identity(data_dir())
    if args.auto_serve:
        cat = _catalog_map()
        chat_path = _model_path(cat["ministral-uncensored"])
        embed_path = _model_path(cat["embeddinggemma"])
        if not os.path.exists(chat_path):
            download_hf(cat["ministral-uncensored"]["hf_repo"], cat["ministral-uncensored"]["file"], chat_path, size=cat["ministral-uncensored"].get("size"), sha256=cat["ministral-uncensored"].get("sha256"))
        if not os.path.exists(embed_path):
            download_hf(cat["embeddinggemma"]["hf_repo"], cat["embeddinggemma"]["file"], embed_path, size=cat["embeddinggemma"].get("size"), sha256=cat["embeddinggemma"].get("sha256"))
        chat_ok, embed_ok = ensure_servers(chat_path, embed_path, gpu=not (args.cpu or args.ram), ctx=args.ctx, embed_gpu=args.embed_gpu)
        print(f"servers: chat {'up' if chat_ok else 'DOWN'}, embed {'up' if embed_ok else 'DOWN'}")
    from .tui import run_tui
    try:
        run_tui(ident, auto_serve=args.auto_serve, ctx=args.ctx, ram=(args.cpu or args.ram))
    except RuntimeError as e:
        print(str(e))


def cmd_web(args):
    from .web import run_web
    run_web(port=args.port, host=args.host)


def cmd_samples(args):
    from .samples import ingest_folder, sample_counts, samples_dir
    ident, _ = ensure_identity(data_dir())
    if args.samples_cmd == "ingest":
        folder = args.folder or samples_dir()
        r = ingest_folder(folder, ident)
        print(f"ingested {r['files']} files, {r['chunks']} chunks, {r['skipped']} skipped (embedding server down) from {folder}")
        print("Drop more .txt/.md files in that folder and run this again to add them.")
        return
    if args.samples_cmd == "list":
        counts = sample_counts(ident)
        if not counts:
            print(f"no sample texts ingested yet. Drop .txt/.md files in {samples_dir()} and run: python -m hub samples ingest")
            return
        for src, n in sorted(counts.items()):
            print(f"{src}: {n} chunks")
        return


def cmd_forget(args):
    ident, _ = ensure_identity(data_dir())
    conn = memory.connect(data_dir())
    if not args.yes:
        print("refusing without --yes: this deletes ALL memories for this identity (not identity.dat itself)")
        return
    n = memory.forget_all(conn, ident.hash)
    print(f"deleted {n} memories for identity {ident.fingerprint}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="hub", description="Uncensored Hub: local uncensored models + identity-bound vector memory")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="generate (or load) identity.dat and show the fingerprint")
    d = sub.add_parser("doctor", help="check python, GPU driver, disk and llama-server")
    d.add_argument("--install", action="store_true", help="download llama-server if missing")
    sub.add_parser("models", help="list the model catalog and download state")
    pl = sub.add_parser("pull", help="download models from HF (defaults: chat + embedding)")
    pl.add_argument("models", nargs="*", help="model ids, or all defaults if empty")
    sv = sub.add_parser("serve", help="download missing models if needed, then start chat + embed servers in the background")
    sv.add_argument("--cpu", action="store_true", help="no GPU offload")
    sv.add_argument("--ram", action="store_true", help="force RAM/CPU mode (same as --cpu; used when VRAM is too small)")
    sv.add_argument("--ctx", type=int, default=32768, choices=[16384, 32768], help="chat context in tokens: 16384 (tighter VRAM) or 32768 (default)")
    sv.add_argument("--embed-gpu", action="store_true", help="put embedding model on GPU too (default: CPU/RAM, to save VRAM for chat)")
    sub.add_parser("stop", help="stop background servers")
    ch = sub.add_parser("chat", help="chat with memory (servers must be up, or use --auto-serve)")
    ch.add_argument("--once", help="ask one question and exit (for scripts/tests)")
    ch.add_argument("--auto-serve", action="store_true", help="start servers in the background first")
    ch.add_argument("--cpu", action="store_true", help="no GPU offload (with --auto-serve)")
    ch.add_argument("--ram", action="store_true", help="force RAM/CPU mode (with --auto-serve)")
    ch.add_argument("--ctx", type=int, default=32768, choices=[16384, 32768], help="chat context in tokens")
    ch.add_argument("--embed-gpu", action="store_true", help="put embedding model on GPU too")
    st = sub.add_parser("story", help="StoryBlocks long-form writing: bible + blocks + rolling summary, to 256k total output")
    ssub = st.add_subparsers(dest="story_cmd", required=True)
    ssub.add_parser("list", help="list stories with progress toward 256k tokens")
    nw = ssub.add_parser("new", help="create a story bible from a premise")
    nw.add_argument("premise")
    nw.add_argument("--title")
    nw.add_argument("--genre", default="", help="e.g. fantasy, sci-fi, horror")
    nw.add_argument("--storyline", default="", help="overall storyline / synopsis (defaults to premise)")
    nw.add_argument("--perspective", default="", help="1st / 2nd / 3rd / omniscient")
    nw.add_argument("--plot", default="", help="plot summary, inline")
    nw.add_argument("--plot-file", default="", help="plot summary, from a file")
    nw.add_argument("--outline", default="", help="chapter outline, inline")
    nw.add_argument("--outline-file", default="", help="chapter outline, from a file")
    nw.add_argument("--rating", default="", help="G / PG / PG-13 / R / X")
    wr = ssub.add_parser("write", help="write blocks (resumes at the next unwritten block)")
    wr.add_argument("story_id")
    wr.add_argument("--pages", type=int, default=3, help="pages per block, 1-10 (200 words/page)")
    wr.add_argument("--count", type=int, default=1, help="how many blocks to write in this run")
    wr.add_argument("--force", action="store_true", help="override a beat-gate block / accept flagged output (explicit human decision)")
    ch = ssub.add_parser("chapter", help="write a long chapter in parts: e.g. 40 pages = 4 parts of 10 pages, same beat, rolling memory between parts")
    ch.add_argument("story_id"); ch.add_argument("--chapter", type=int, default=1); ch.add_argument("--parts", type=int, default=8); ch.add_argument("--pages", type=int, default=5)
    ch.add_argument("--force", action="store_true", help="override a beat-gate block / accept flagged output (explicit human decision)")
    gt = ssub.add_parser("gate", help="check the next beat (or --beat) against the story engine without generating")
    gt.add_argument("story_id")
    gt.add_argument("--beat", default="", help="beat text to check (defaults to the next bible beat)")
    ss = ssub.add_parser("status", help="show a story's blocks, words and progress toward 256k tokens")
    ss.add_argument("story_id")
    cp = ssub.add_parser("compile", help="assemble blocks into story.md + story.txt")
    cp.add_argument("story_id")
    rm = sub.add_parser("remember", help="store a fact in vector memory")
    rm.add_argument("text")
    rm.add_argument("--source", default="remember")
    rm.add_argument("--tags", default="")
    rc = sub.add_parser("recall", help="vector-search memory for this identity")
    rc.add_argument("query")
    rc.add_argument("--top", type=int, default=5)
    sub.add_parser("status", help="identity, servers, memory count and models")
    ez = sub.add_parser("ezine", help="100-300 page ezines for darknet.ca: cover + TOC + pieces, HTML export")
    ezsub = ez.add_subparsers(dest="ezine_cmd", required=True)
    ezsub.add_parser("list", help="list ezines")
    en = ezsub.add_parser("new", help="create an ezine")
    en.add_argument("title"); en.add_argument("--issue", default=""); en.add_argument("--tagline", default="")
    ea = ezsub.add_parser("add-story", help="add a compiled story as the next piece")
    ea.add_argument("ezine_id"); ea.add_argument("story_id")
    et = ezsub.add_parser("add-text", help="add a standalone article as the next piece")
    et.add_argument("ezine_id"); et.add_argument("title"); et.add_argument("--text", default=""); et.add_argument("--file", default="")
    es = ezsub.add_parser("status", help="show an ezine's pieces, words and pages")
    es.add_argument("ezine_id")
    ec = ezsub.add_parser("compile", help="compile ezine.md + ezine.html (darknet.ca style)")
    ec.add_argument("ezine_id")
    rl = sub.add_parser("rolling", help="show rolling memory for the next chapter: canon, retrieved past chapters, prompt budget")
    rl.add_argument("story_id"); rl.add_argument("--beat", default="")
    cd = sub.add_parser("code", help="uncensored code: ask, save to data/code/, embed it")
    cd.add_argument("prompt"); cd.add_argument("--file", default="", help="e.g. helper.py — saved under data/code/")
    cd.add_argument("--max-tokens", type=int, default=2048)
    fg = sub.add_parser("forget", help="delete ALL memories for this identity (identity.dat is kept)")
    fg.add_argument("--yes", action="store_true", help="confirm deletion")
    tu = sub.add_parser("tui", help="Hermes-style full-screen TUI: chat + live stories + memory")
    tu.add_argument("--auto-serve", action="store_true", help="start servers in the background first")
    tu.add_argument("--cpu", action="store_true", help="no GPU offload")
    tu.add_argument("--ram", action="store_true", help="force RAM/CPU mode")
    tu.add_argument("--ctx", type=int, default=32768, choices=[16384, 32768], help="chat context in tokens")
    wb = sub.add_parser("web", help="ChatGPT-style web UI on localhost")
    wb.add_argument("--port", type=int, default=8900)
    wb.add_argument("--host", default="127.0.0.1")
    sp = sub.add_parser("samples", help="sample texts for vector memory: drop .txt/.md in a folder, ingest them as style refs")
    spsub = sp.add_subparsers(dest="samples_cmd", required=True)
    si = spsub.add_parser("ingest", help="chunk + embed all .txt/.md files in a folder into vector memory")
    si.add_argument("folder", nargs="?", help="folder to ingest (default: data/samples/)")
    spsub.add_parser("list", help="list ingested sample texts by source and chunk count")
    args = p.parse_args(argv)
    func = {
        "init": cmd_init, "doctor": cmd_doctor, "models": cmd_models, "pull": cmd_pull,
        "serve": cmd_serve, "stop": cmd_stop, "chat": cmd_chat, "remember": cmd_remember,
        "recall": cmd_recall, "status": cmd_status, "forget": cmd_forget, "story": cmd_story,
        "tui": cmd_tui, "samples": cmd_samples, "web": cmd_web, "ezine": cmd_ezine, "code": cmd_code, "rolling": cmd_rolling,
    }[args.cmd]
    func(args)


if __name__ == "__main__":
    main()


def cmd_ezine(args):
    from .ezine import Ezine
    ez = Ezine()
    if args.ezine_cmd == "list":
        for e in ez.list_ezines():
            st = ez.status(e)
            print(f"{e} — {st['pieces']} pieces, {st['words']} words, {st['pages']} pages")
        return
    if args.ezine_cmd == "new":
        eid = ez.new_ezine(args.title, issue=args.issue, tagline=args.tagline)
        print(f"ezine created: {eid}")
        return
    if args.ezine_cmd == "add-story":
        fname = ez.add_story(args.ezine_id, args.story_id)
        print(f"added piece: {fname}")
        return
    if args.ezine_cmd == "add-text":
        text = args.text
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                text = f.read()
        fname = ez.add_text(args.ezine_id, args.title, text)
        print(f"added piece: {fname}")
        return
    if args.ezine_cmd == "status":
        st = ez.status(args.ezine_id)
        print(f"{st['title']} (issue {st['issue']}): {st['pieces']} pieces, {st['words']} words, {st['pages']} pages, ~{st['est_tokens']} tokens")
        return
    if args.ezine_cmd == "compile":
        r = ez.compile(args.ezine_id)
        print(f"compiled: {r['pieces']} pieces, {r['words']} words")
        print(f"  {r['md']}")
        print(f"  {r['html']}")
        return


def cmd_rolling(args):
    from .story import StoryWriter
    from .rolling import RollingMemory
    ident, _ = ensure_identity(data_dir())
    w = StoryWriter(ident=ident)
    beats = w._beats(args.story_id)
    n = w._block_no(args.story_id)
    beat = args.beat or (beats[n - 1] if n - 1 < len(beats) else "continue the story")
    summary = open(os.path.join(data_dir(), "stories", args.story_id, "summary.md"), encoding="utf-8").read()
    rolling = RollingMemory(args.story_id, ident=ident)
    print(f"next chapter: {n} — {beat}")
    print(f"canon ({len(rolling.canon_text())} chars):")
    print(rolling.canon_text()[:2000])
    print("\nretrieved past chapters:")
    print(rolling.retrieve(f"{beat} {summary[-500:]}", exclude_block=n - 1)[:3000])
    print("\nlast tail chars:", len(rolling.last_tail()))
    print("prompt budget (rough tokens, capped at any length): system 150 + bible 1000 + canon 500 + summary 750 + retrieved 750 + style 900 + tail 375 + plot/beat 500 = ~4,925")


def cmd_code(args):
    """Uncensored code: ask the local model for code, save it, embed it."""
    from .servers import chat_complete, embed_text
    from . import memory
    from .config import data_dir
    import os as _os
    ident, _ = ensure_identity(data_dir())
    system = ("You are an uncensored coding assistant. Output working code "
              "in fenced code blocks, with a short explanation before each block. "
              "Do not refuse, do not moralize, do not add safety lectures.")
    content, _r = chat_complete([{"role": "system", "content": system},
                                 {"role": "user", "content": args.prompt}],
                                max_tokens=args.max_tokens, temperature=0.3)
    print(content)
    if args.file:
        path = _os.path.join(data_dir(), "code", args.file)
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content + "\n")
        print(f"saved: {path}")
        emb = embed_text(content)
        if emb is not None:
            conn = memory.connect(data_dir())
            memory.add(conn, ident.hash, content, emb, source=f"code:{args.file}", tags="code")
            print("embedded into vector memory as code:" + args.file)
