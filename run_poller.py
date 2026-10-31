#!/usr/bin/env python3
"""Run this node's mailbox poller — the firewall tunnel.

This node never listens. It polls its mailbox on the list server
outbound (HTTPS passes firewalls; nothing inbound is ever required),
verifies each job's signature, applies this node's OWN allow/deny/
block policy (peers.json in the data dir, plus --allow-pubkey), runs
the job against a local service, and replies through the mailbox.

Handlers wired here:
  text  -> local chat model. Default backend is Ollama (see TEXT_MODEL
           / OLLAMA_URL); --text-backend llama serves a llama-server
           instead (see LLAMA_URL) — the LFM3 path, since Ollama's
           bundled llama.cpp predates LFM3 support.
  tts   -> local PocketTTS bridge on http://127.0.0.1:8000 (MP3).
           Omit with --no-tts if no bridge runs on this machine.
  image -> Z-Image queue, only when --image-queue and --image-outdir
           are given (finished PNGs are delivered back each sweep).

Usage:
  python run_poller.py --data-dir ./data
  python run_poller.py --data-dir ./data --allow-pubkey <hex> --allow-pubkey <hex>
  TEXT_MODEL=my-model OLLAMA_URL=http://127.0.0.1:11434 python run_poller.py --data-dir ./data
  python run_poller.py --data-dir ./data --text-backend llama --llama-url http://127.0.0.1:11436

Stdlib only. No pip installs. Python 3.10+.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tunnel as tunnel_mod  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default="./data",
                    help="identity.dat + peers.json live here (default ./data)")
    ap.add_argument("--list-url", default="https://node.darknet.ca",
                    help="list server (default https://node.darknet.ca)")
    ap.add_argument("--allow-pubkey", action="append", default=[],
                    help="whitelist a peer's compressed pubkey (repeatable)")
    ap.add_argument("--interval", type=int, default=60,
                    help="poll interval in seconds (default 60: on the Paid "
                         "plan 100 clients at 60s cost ~4.3M writes/month, "
                         "~14% of the 30M budget; on a free-tier list host "
                         "pass 120, where 100 clients cost ~2.2M/month)")
    ap.add_argument("--text-backend", choices=["ollama", "llama"], default="ollama",
                    help="text backend fallback (serving.json, set on the Serving page, wins when present)")
    ap.add_argument("--llama-url", default="http://127.0.0.1:11436",
                    help="llama-server URL for --text-backend llama (default :11436)")
    ap.add_argument("--image-queue", default=None,
                    help="path to a Z-Image queue.txt (enables image jobs)")
    ap.add_argument("--image-outdir", default=None,
                    help="dir where finished renders land as <name>.png")
    ap.add_argument("--image-engine", default="z-image",
                    help="this node's image engine (default z-image); jobs naming another engine fail loud")
    ap.add_argument("--no-tts", action="store_true",
                    help="do not offer tts (no bridge on this machine)")
    args = ap.parse_args()

    # Serving.json (operator-set on the Serving page) overrides the
    # CLI fallback per job: text backend incl. cloud models, and the
    # default voice. No serving.json -> flags behave exactly as before.
    handlers = {"text": lambda p: tunnel_mod.text_dispatch(
        args.data_dir, args.text_backend, args.llama_url, p)}
    if not args.no_tts:
        handlers["tts"] = lambda p: tunnel_mod.bridge_tts(
            p.get("text", ""), p.get("voice") or tunnel_mod.serving_voice(args.data_dir))
    poller = tunnel_mod.TunnelClient(
        args.data_dir, args.list_url, set(args.allow_pubkey), handlers,
        image_queue=args.image_queue, image_outdir=args.image_outdir,
        image_engine=args.image_engine)
    print("poller: node %s polling %s every %ds"
          % (poller.identity.node_id, args.list_url, args.interval))
    print("poller: handlers: %s; allow: %s"
          % (sorted(handlers), sorted(args.allow_pubkey) or "(peers.json only)"))
    poller.run_forever(args.interval)


if __name__ == "__main__":
    main()
