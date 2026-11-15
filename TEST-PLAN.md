# Two-node test plan — tasks to test between agents

How to test the node network end to end, on this box, with no cloud
account, no Cloudflare, and no money. Everything runs against the
in-memory mock list (`mock_list.py`) — the same routes as the real
`worker-node-list.js` (`/nodes`, `/announce`, `/mailbox/*`,
`/lobby`, `/chatbox/*`, `/upload`, `/uploads`) — so the suite proves
the real client, poller, and transfer code, not a simulation of it.
The only live service it touches is the local LFM3 on :11436, for
the one step that must prove a real model answers.

Run it: `python3 test_two_nodes.py` from this directory.
Work dir: `run-two-nodes/` (wiped each run; fresh identities each run).

## The agents

- **mya-node** — the serving node (this box). Caps: text, tts, image.
  Its poller allow-list holds only the laptop. Its text handler is
  the real LFM3 (`tunnel.llama_text`, :11436).
- **laptop** — the requesting node (Jero's laptop, simulated).
  Caps: text. It asks mya-node for things.
- **phone** — a third node, used only for the sysop-chat leg.
- **stranger** — an announced node NOT on mya-node's allow-list.
  Used for the negative gates.
- **ghost** — a node that never announces. Used for the listed-only gate.

## The task list (each is a check in test_two_nodes.py)

### 1. Announce + discover
- [ ] Both nodes announce; each sees the other on `/nodes` with the
  capabilities it claimed (mya-node: text/tts/image, laptop: text).
- [ ] No verified flag, no approval step — a valid announce lists
  immediately (Gopher model).

### 2. Sysop-to-sysop chat (private, back-and-forth)
- [ ] laptop → mya-node: "hello, can I use your text model?"
- [ ] mya-node polls its chatbox and sees the message with laptop's name.
- [ ] mya-node → laptop: "yes, you're allowed — sending you the allow."
- [ ] laptop polls its chatbox and sees the reply with mya-node's name.
- [ ] Ordering holds (messages arrive in send order) and the poll
  clears the queue (second poll is empty).
- [ ] phone → mya-node also works (a third node can reach the sysop),
  and the chatbox never mixes with the job mailbox.

### 3. Lobby (public room)
- [ ] mya-node posts a public note; laptop reads it on `/lobby`.
- [ ] An unlisted node (ghost) cannot post (403).

### 4. Use another node's text model from chat
- [ ] laptop sends a text job to mya-node's mailbox ("What is 2+2?").
- [ ] mya-node's poller (allow-list = {laptop}) processes it with
  the REAL LFM3 and posts a signed reply.
- [ ] laptop polls its mailbox and gets a real model answer
  (non-empty, answers the question). This is the whole point:
  the requester never runs a model, the serving node does.
- [ ] The reply is signed by mya-node (verified by the test).

### 5. File transfer (TC containers)
- [ ] mya-node offers a file on the list (`/upload`), splits it into
  signed TC containers, and serves the outbox over HTTP.
- [ ] laptop fetches it (allow-listed), every chunk verifies
  (signature + payload hash), the ledger records 3 accepted
  entries, and the reassembled file's sha256 matches the original.
- [ ] A non-allow-listed fetch is refused ("sender not allowed"),
  no file reassembled.

### 6. Share a cloud answer via a 📦
- [ ] mya-node has a cloud answer (here a canned answer standing in
  for a bolt-e4b reply — no key, no cloud call, no money in the
  test; on the real network the serving node would paste its real
  cloud answer into the same file). It writes the answer to a
  file, splits it into a signed TC container (the 📦), and offers
  it on the list.
- [ ] laptop fetches the 📦, verifies it, and reads the answer text
  — the cloud answer arrives intact, as a file, not inside a
  single mailbox JSON the proxy can truncate. This is the pattern
  for large results: mailbox for the job, 📦 for the payload.
- [ ] The 📦 manifest is signed by mya-node and the file hash matches.

### 7. Negative gates (the network stays safe)
- [ ] ghost (never announced) sends a job → rejected at the mailbox (403).
- [ ] A job with a forged signature → rejected (401).
- [ ] stranger (announced, NOT allow-listed) sends a text job → queued
  at the mailbox but the poller skips it silently; stranger gets no reply.
- [ ] A tampered 📦 chunk → refused, no reassembly.

## Manual test (against the real node.darknet.ca)

When you want to run the same sequence for real — laptop + phone
against this box's live mya-node — the steps are the same, with the
real list URL and the real identities:

1. On the laptop: `python node.py --data-dir ./data --name laptop
   --announce yes nodes` — confirm mya-node is listed with text/tts/image.
2. Sysop chat: `node.py ... chat-send <mya-node pubkey> "hello, can I
   use your text model?"` — mya-node's sysop reads it in its chatbox.
3. Text job: send a text job to mya-node's mailbox (the Jobs tab in
   the Android app does this); mya-node's poller runs it on LFM3
   and replies. Poll the mailbox for the answer.
4. File transfer: mya-node `upload` + `send` a file, then laptop
   `fetch --base-url <mya-node outbox> --allow-pubkey <mya-node pubkey>`.
5. Cloud answer via 📦: mya-node writes its cloud answer to a file,
   `send`s it as a TC container, laptop `fetch`es it.
6. Gates: the laptop must be on mya-node's allow-list (peers.json)
   or its jobs are skipped; an unannounced node is rejected at the
   mailbox.

## Round 2 — edge cases (test_edge_cases.py, 35 checks)

The failure modes the first suite did not cover. Same setup
(mock list, stdlib only); the real bridge is used for the TTS leg.

- **Mailbox:** an awaiter that meets a job must bounce it back
  (re-queue), not destroy it; the same job posted twice is
  answered once (dedup); a raising handler gets an error reply
  and the poller keeps going; an unknown capability is skipped
  with no crash and no reply.
- **Lobby/chat limits:** the 100-message cap, 2000-char truncation,
  empty post/message rejected (400).
- **Announce:** re-announce updates capabilities; bad signature
  rejected (401).
- **Real TTS through the mailbox:** the poller's tts handler is the
  real bridge — the reply carries a real 25,101-byte MP3.
- **Transfer edges:** a large multi-chunk 📦 (600KB cloud answer,
  3 chunks) arrives intact; fetch without a listed offer is
  refused; a missing chunk gives "incomplete set", no reassembly;
  a manifest that lies about the file hash is caught by the
  reassembled hash; a dead outbox is a clean "download failed",
  not a crash (this caught a real bug — the manifest fetch in
  `fetch_transfer` used to raise; fixed 2026-11-07).
- **Crypto:** a node.py signature verifies under the Hub's
  hub/package.py and vice versa; canonical forms agree.

## Round 3 — presence + rate limiting (test_presence.py, 21 checks)

The 50-person scale work (2026-11-07, Jero: "Yes" + 15s poll for
testing). The list host is presence, not a roster: a node is listed
only while it has been seen within the TTL (15 min), and sends are
rate-limited per verified sender (600 per 10 min, 429 over it).

- **Presence:** announce -> listed with a `seen` stamp; no
  heartbeat -> gone from /nodes after the TTL; an expired node gets
  403 on send ("not a listed node"); re-announce lists it again and
  updates its capabilities; `NodeClient.heartbeat()` is a
  re-announce that refreshes `seen`.
- **Poller heartbeat:** a poller heartbeating every 1s keeps its
  node listed past a 3s TTL; it also heartbeats on start (before
  the first poll), so a fresh poller is listed from minute one.
- **Rate limiting:** a sender that exceeds its quota gets 429 on
  the 6th send; a different sender is unaffected (per-sender); the
  limit applies to lobby, mailbox, and reply sends. Polls are NOT
  rate-limited (an empty poll writes nothing, so a KV counter per
  poll would cost more writes than it saves).

The real Worker JS was also run locally (`wrangler dev`, :8787)
and verified end to end: two signed announces -> /nodes lists
both with `seen` stamps and correct caps; lobby send 200, /lobby
shows 1 message; mailbox send 200. The TTL and 429 paths are
proven by the mock tests above (waiting 15 min / sending 600
requests against the live Worker is not a useful test).

## What this does NOT test

- The real Cloudflare Worker / KV (the mock mirrors its routes, but
  KV's 25MB cap and the 1000-writes/day free tier are live-only).
- The Android app UI (the app talks the same mailbox/chatbox/TC
  protocol; its UI is tested on the phone).
- TTS / image end to end (covered by test_tunnel.py and
  test_image_capability.py with stub handlers; the live bridge MP3
  is checked in test_tunnel.py).
- The torrent-style swarm transfer (design note only,
  TORRENT-TRANSFER-IDEA.md — not built).
