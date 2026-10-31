# Uncensored Node 0.0.1

A local-first peer network for uncensored AI: chat, voice, and image
generation shared node-to-node, with no central server, no account,
and no permission gate. Each node runs on its owner's hardware and
decides for itself who may use it.

This is the **0.0.1 release pair**: one laptop build (Python,
stdlib only) and one Android build (Kotlin), versioned together.
It supersedes the 0.0.2–0.0.6 testing line — same code, clean
version. The 0.0.2–0.0.6 zips stay in Drive as superseded builds.

## Rebuild — 2026-10-29 (same version, same Drive file)

This rebuild keeps version 0.0.1 and replaces the laptop zip in
place. What changed since the 2026-10-28 rebuild:

- **Embedding server no longer dies on an old binary.** The
  embedding model needs llama.cpp build 11630 or newer for the
  `gemma-embedding2` architecture; the Hub used to accept any
  `llama-server` it found and only installed one when none existed,
  so a stale binary silently broke embeddings. `hub/llama.py` now
  rejects builds under 11630, prefers the Hub-managed `bin/` over
  PATH, and `python -m hub doctor --install` replaces a too-old
  binary. On a laptop that already has a stale binary, delete it
  (or run doctor with `--install`) and verify the build number.
- **Voice jobs actually land in chat.** Clicking Generate voice
  used to flash the queued notice for 2.5 seconds and leave it in
  the Node tab, where the first Fetch before the peer's poller had
  finished wiped it and lost the job id. The job now stays pinned
  to the chat message, the page auto-fetches every 15 seconds
  until its reply arrives, and the waveform renders inline in
  chat. A tts job to an allowed peer that does not offer tts now
  gets an error reply instead of a silent drop, and the reply
  bounce window is 10 cycles instead of 3.
- **Node tab redesigned.** Identity on top (name, node_id small
  underneath), with a left menu of separate pages: Lobby, Peers,
  Private chat, Files, Jobs, Poller. The Lobby is IRC-like
  (auto-refresh, Enter sends) and shows each sender's announced
  name with their node_id small underneath, so a name alone
  cannot be spoofed. With no name set, the lobby input is
  disabled and both the server and the UI say "set a name
  first" — you can read the lobby, but you can't chat.
- **IRC-console Node tab (rebuild #4).** General load lands on the
  Node tab (lobby + identity), with a new Identity page in the
  left menu. The lobby is a black console with a blinking cursor
  and auto-refreshes every 15 seconds client-side. Clicking a
  lobby or peer name pulls up that node's card — announced caps,
  shared endpoint when it has one (announce carries it, no
  endpoint means "no endpoint shared"), and a Private chat
  button. Block hides a node's lobby messages for you only, on
  this browser (local); removing them for everyone is not a UI
  change — it needs a Worker moderation endpoint (see Known
  limits).
- **Memory provenance + full node view (rebuild #5).** Every chat
  answer now traces to the memories it used: the done event
  carries each used memory (id, snippet, source, the model that
  wrote it, score), rendered under the assistant message as an
  expandable trace — "used N memories" was already there, but
  which ones and which model each came from was not. Memories
  record the active chat model at write time (catalog name of
  the running model, else the file basename, else "local
  uncensored"); rows written before this column existed show as
  unknown, never invented. A usage log links each reply to the
  memories it drew on, so the trace runs both ways. The Memory
  tab gains a Full node view: every memory node with its model
  and the replies that used it, as the brain grows.
- **Voice: download icon + shared files (rebuild #5).** The
  waveform player's Save link is now a ⬇ download icon, and a
  Share button stores the voice in local shared files
  (`data/shared/`), listed in the Files tab with a Publish offer
  button that posts it to the public offers list.
- **regula_core file ledger (rebuild #5).** `hub/regula.py` is the
  local file system based on the ledger: `data/ledger.json` is an
  append-only chain of events signed with the node's identity.dat
  key (ECDSA secp256k1 over double-SHA256, DER — the network's
  own scheme), each event carrying prev_hash, with the event hash
  covering the event plus its signature. Shared files publish as
  signed Merkle bundles: a manifest with a sha256 per file, a
  Merkle root over the sorted hashes, the ledger tip as prev_hash,
  and a signature over the canonical manifest. `GET
  /api/ledger/status` re-verifies the whole chain (hashes,
  prev-hash pointers, signatures); the Files tab shows ledger OK
  and height. A failed publish records no ledger event — rejected
  items never enter signed history. The 2006 Chamber Zero header
  (RSA-1024, LCG nonce) is history, not the scheme used here.

- **Ledger on every write + 50MB block store (rebuild #6).** The
  regula ledger now runs on every write a node makes, not just
  shared files: saving a session (chat, code, research) records a
  `session` event with the session type; compiling a story or an
  ezine records its finished outputs (`story`, `ezine`); saving a
  code file records a `code` event and the code memory row now
  carries the generating model; ingesting samples records each
  source file (`sample`); finishing a model download chunks it
  into the block store (`model`). A ledger failure never loses
  the user's own save — it is reported in the response instead
  of raised. Files meant to travel are split into 50MB chunks,
  one chunk in memory at a time, stored content-addressed under
  `data/blocks/` (same bytes stored once, ever), with a signed
  manifest listing the chunks in order, the whole-file sha256,
  and the Merkle root over the chunk hashes; the ledger records
  one block event per chunk plus one manifest event closing the
  set. Published files now publish that chunked manifest, so a
  peer can fetch the file chunk by chunk by hash
  (`GET /api/blocks/<sha256>`, raw bytes, 404 when absent), and
  `GET /api/ledger/events` lists the latest events for the Files
  tab's new ledger view. The trade-off is stated plainly:
  chunking a model doubles its disk (the file in `models/` plus
  its chunks in `blocks/`); the chunk store is what makes the
  file transferable, and deleting `blocks/` for a model only
  loses the transfer copy — the ledger record stays.

Test suites at rebuild time: 228 checks green (Hub 89, node 77,
nodeapi 62).

## The model: Gopher, not a platform

There is no central sysop, no approval queue, no verified badge,
and no grant system. The rules are:

- **The list is open.** A valid announce lists the node immediately,
  with the capabilities it claims to offer. Nothing is granted.
- **The gates live on each node.** Every serving node keeps its own
  allow/deny/mute policy in a local JSON file that never leaves the
  node: allow = whitelisted, persistent, no per-job ask; deny = an
  explicit error reply; block/mute = silent drop; unlisted = the
  allow-set fallback, else silent skip.
- **Asking for access means messaging the operator** — in the public
  Lobby or by private sysop chat. There is no request/grant API.
- **Files list publicly at once.** Each receiver's fetch allow-list
  is the only download gate.

The list host is **node.darknet.ca**. Nodes behind NAT or a
firewall serve outbound-only: they poll a mailbox on the list host
(HTTPS, same pattern as a relay), so nothing inbound is ever
required anywhere.

## Identity and signing

Every node has one identity: `data/identity.dat`, a single 32-byte
secp256k1 secret, mode 0600, created on first run. It never leaves
the machine. Only the compressed public key is ever shared.

- Node fingerprint: `NODE-XXXX-XXXX-XXXX-XXXX` (SHA256 of pubkey).
- Node id: `node-<12 hex>`.
- Signing: ECDSA secp256k1 over double-SHA256, deterministic
  (RFC6979), low-S, DER, over canonical JSON (sorted keys, compact
  separators). The Hub side derives its own `HUB-` fingerprint and
  hub id from the same 32 bytes, so one secret backs both the Hub
  memory and the node identity.
- Back up `identity.dat` like a key. Losing it loses the identity
  and the memory bound to it. Never ship `data/` — release zips
  exclude it, and so should any fork.

## List routes (node.darknet.ca)

Twelve routes, no more:

```
GET  /nodes          POST /announce      POST /gossip
POST /upload         GET  /uploads
POST /mailbox/send   POST /mailbox/poll   POST /mailbox/reply
POST /lobby/send     GET  /lobby
POST /chatbox/send   POST /chatbox/poll
```

The Worker behind them is a dumb pipe: signature + listed check
only, on mailbox and chatbox sends. It holds no keys and makes no
access decisions.

## Capabilities

A node offers what it claims, and serves only what its operator
allows:

- **text** — a local chat model. The reference node serves
  LFM3-1.7B-Uncensored via a standalone llama-server (Ollama's
  bundled llama.cpp predates LFM3 support, so the Ollama backend
  cannot load it); any Ollama model works with the default backend.
- **tts** — a local PocketTTS bridge, MP3 out. Phone and laptop
  clients play it directly.
- **image** — a local Z-Image queue. Finished PNGs come back as a
  base64 mailbox reply up to 20MB, else as a signed TC manifest
  with outbox packaging (fetch needs the normal offer→fetch chain,
  receiver allow-listed).

Jobs are mailbox messages: signed, addressed by pubkey, answered
with a signed reply. Mailbox polling is fetch-and-clear, so
consumers must not eat each other's messages:

- The serving poller re-queues any reply it meets, verbatim
  (bounded at 3 bounces, about 3 minutes at the 60s poll interval,
  so abandoned replies cannot burn the KV write quota; a bounce
  that fails is held in a pending list and retried every cycle, not
  lost).
- Reply awaiters (Jobs tab, self-tests) re-queue any job they meet,
  verbatim, for the poller.
- The poller answers each job id once: a job is marked seen only
  when its reply posts 200, so a spare copy retries the whole job
  including its reply instead of double-running the work.
- Posts retry 5 times with backoff (2/4/8/16s); bounces retry 3
  times (a failed bounce is retried next cycle anyway, and every
  retry that lands after a dropped response is a duplicate KV
  write). Sends are intentionally not retried blindly: a dropped
  send is ambiguous (it may have landed), so the sender sees the
  error and retries.
- Mailbox delivery is at-most-once: a job polled but unanswered is
  lost if the poller dies mid-job. KV values cap at 25MB, so long
  audio goes via TC, not the mailbox.

## Lobby, sysop chat, favorites, files

- **Lobby** — public IRC-like room. Signed posts from listed nodes
  only, last 100, 2000-character cap.
- **Sysop chat** — private chatbox queue, listed-check only. It is
  NOT the job mailbox, so the poller can never eat chat.
- **Favorites** — per-node local list only. The app star is
  client-side; nothing about favorites leaves the node.
- **Files (TC)** — 50MB signed containers, streaming split (never
  holds more than one chunk in memory, so multi-GB models work on
  small machines), per-chunk ledger, hash-verified reassembly,
  receiver allow-list enforced on fetch. Containers interoperate
  with the `tc-prototype/tc.py` format in both directions. The
  two-pane FTP-style UI is future work; the transfer engine is
  built.

## Laptop build (this tree)

Python, stdlib only. No pip install, no Docker, no cloud at serve
time. Contents: the Uncensored Hub (`hub/`) plus the node client
(`node.py`, `tunnel.py`, `run_poller.py`, `mock_list.py`), the Node
tab (`hub/nodeapi.py`, Node view in `hub/web_ui.html`), and the
poller as a managed service.

### Run it

In WSL (recommended on Windows — full Linux, GPU passthrough via
the Windows driver):

```
python3 autorun.py
# then open http://localhost:8900 in your Windows browser
```

Autorun starts the chat server (:8080) + embedding server (:11435)
(VRAM if present, RAM fallback otherwise) and the web UI (:8900).
`hub web` runs just the web UI; `hub chat` / `hub tui` are the CLI
and fullscreen TUI front ends. Models live in `models/` (downloaded
via the Models tab or `hub pull`, size + SHA256 verified) and are
never shipped in the zip.

### Node tab (web UI)

1. Set a **node name** and press **Announce** — the node lists
   immediately with the capabilities it claims.
2. **Peers** shows listed nodes and their offered caps. Star a
   favorite; set a peer to allow, deny, block, or clear.
3. **Lobby** is the public room; **Sysop chat** is private.
4. **Jobs** sends a text, tts, or image job to a peer. Replies
   render by shape: text, playable audio, image, queued ack shown
   as pending (not an error), TC manifest note.
5. **Poller** (also on the System view's Servers card) runs the
   mailbox poller: text-only by default, serving this machine's
   local model to allowed peers, polling every 60s. It never
   listens — outbound HTTPS only.

Operators with a local PocketTTS bridge or a Z-Image queue offer
tts/image too by running the poller directly:

```
python run_poller.py --data-dir ./data                                   # text + tts (bridge on :8000)
python run_poller.py --data-dir ./data --no-tts                          # text only
python run_poller.py --data-dir ./data --image-queue <queue.txt> --image-outdir <renders/>
python run_poller.py --data-dir ./data --allow-pubkey <hex> --interval 60 # repeatable allow-list
python run_poller.py --data-dir ./data --text-backend llama --llama-url http://127.0.0.1:11436  # LFM3 via llama-server
```

## Running costs (KV budget)

The list host runs on Cloudflare Workers. Every list write is a
KV put: announces, job sends, replies, bounces, lobby posts,
chatbox sends — and, until the Worker fix below is deployed,
**every poll, even an empty one** (the poll handler saves the
mailbox on every poll). On 2026-10-28 a 5s poller plus a 12-bounce
bound blew the free tier's 1000 writes/day by itself, and all list
writes went 429 until the operator upgraded to the Workers Paid
plan the same evening (1M writes/day = 30M/month).

Steady state on Paid, with the client fixes in this release: the
default poll interval is 60s. 100 clients polling at 60s cost
100 × 1440 × 30 = 4.32M writes/month, ~14% of the budget, leaving
room for jobs, lobby, and announces (a job round-trip costs 2–5
writes: send + reply, plus bounces if the awaiter is slow). At 5s
the same 100 clients would burn 51.8M/month — over budget even on
Paid, which is why 5s is not the default. On a free-tier list host
pass `--interval 120`: 100 clients at 120s cost ~2.2M/month, but
that already exceeds the free 1000/day, so a free-tier host really
only fits a few nodes. The real fix is Worker-side: the poll
handlers now only save when the mailbox is non-empty (edit made,
redeploy pending — it needs a Cloudflare token). With that deployed,
idle polling costs nothing and the interval can drop back to
seconds.

### Testing against a listed node

The Node tab's Jobs panel sends a job to any node on the list:
pick the peer, pick the capability (text, tts, image), fill the
fields. For tts there is a voice box — it defaults to mya-03;
type another voice name (for example mya-02) to use it. A job only
runs if the serving node has your pubkey on its allow-list, so the
first step is always: announce your node, then send your pubkey to
that node's operator, who runs `peer allow <pubkey>` on their side.
Replies come back through the mailbox (base64 audio/image up to
20MB, larger images via TC manifest). Poll your own mailbox from
the Jobs tab — never from a second client at the same time, or the
two will eat each other's replies.

### Generate voice + waveform + Save

Every text result carries a **Generate voice** button — on Jobs
text replies and on chat assistant messages. It sends that text as
a tts job to the peer in the Jobs peer box (click a node in the
peer list to fill it; the voice box picks the voice, default
mya-03), through the same mailbox pipeline — no new protocol. The
MP3 reply renders as a waveform player: bars drawn from the audio,
click the waveform to seek, Play/Pause to listen, and **Save
voice** downloads the MP3 to this machine (voice-<reply>-<time>.mp3).
If waveform decoding fails in the browser, the player falls back to
flat bars and playback still works.

### Hub features in this tree

- **Chat** with a local uncensored model, streaming, ChatGPT-style
  Copy / Delete / Regenerate, hidden-until-clicked thinking, and
  output multipliers 1x–16x (8x truly 16,384 tokens; 16x capped at
  28,000 for the configured context).
- **Vector memory, unified.** One sqlite DB (`data/memory.db`):
  chat turns, explicit remembers, notes, story blocks, ezine
  pieces, code, samples, PDFs — all embedded with EmbeddingGemma-2
  (768 dims, CPU/RAM by default so it does not steal VRAM) and
  filtered by identity hash in every query.
- **Rolling memory for long-form work.** A canon file (~500 tokens,
  always in context) built by a MEMORY footer the model writes in
  the same GPU call as the prose, plus a vector block index (one
  chunk per past chapter, score > 0.5, last chapter excluded).
  Prompt holds ~4,925 tokens at any book length.
- **Stories and ezines.** Chapters target 35–50 pages; ezines
  compile to a single darknet.ca-style HTML file (100–300 pages)
  for subscribers. The durable lesson, measured: the model follows
  the bible's voice, not the perspective setting — write the bible
  in the voice you want the prose in.
- **Code.** Saved to `data/code/` and embedded for later retrieval.
- **Samples and PDFs.** Ingested as style inspiration (stories) or
  facts (chat), never copied verbatim.
- **Models tab.** Fetch uncensored models from a Hugging Face
  collection (prefilled `chwoo/uncensored-models`; token per-request
  only, never stored or logged), with per-row size, quant, and a
  GPU MEMORY REQUIRED estimate (file size x 1.2, weights + ~20%
  KV/context overhead, labeled not measured).
- **Signed chat packages.** Deterministic ECDSA secp256k1 over
  double-SHA256 of the canonical payload, DER; the private key
  never leaves the server.
- **Telemetry.** Floating chip overlay with live status.
- **Beat-gate and temporal lobe.** Tree-only, default OFF, pending
  laptop multi-chapter tests.

## Android build (separate zip, `android/` folder)

Kotlin, `ca.darknet.node`, minSdk 26, targetSdk 34. Build it in
Android Studio — it is NOT compiled in this repo's sandbox
(Gradle is blocked there; Android Studio + the source zip is the
working path). First-build checklist: JDK 17, Android SDK 34,
NDK (for the native side), git, and internet at first build
(llama.cpp sources are fetched then).

- **Identity.** Generated on first run (`identity.dat`, same
  32-byte secp256k1 format as the laptop node). Announce from the
  Identity screen; the app serves nothing, so it announces no
  capabilities — listing is what enables sending jobs.
- **Nodes.** Listed nodes with offered caps + favorite star.
  Asking for access = a Lobby or sysop-chat message.
- **Jobs.** Text, tts (MP3 playback), and image jobs through the
  tunnel. One shared mailbox poll loop (independent loops eat each
  other's replies under fetch-and-clear); image waits skip the
  serving node's immediate `{"status": "queued"}` ack and complete
  on the real result; chat waits allow 300s (cold model load).
  TC manifests surface as text for now — the TC fetch client is
  phase 3.
- **Lobby + sysop chat.** Same rooms as the laptop tab.
- **Local (on-device chat).** LFM3-1.7B-Uncensored, Q4_K_M
  (1,132,208,000 bytes, size-verified on download), via a llama.cpp
  JNI bridge built from llama.cpp master at Studio build time
  (LFM3 support postdates release b11744; a pinned older release
  would miss it). Plain User/Assistant prompt format, 2048
  context, fresh context per turn, top-k 40 / top-p 0.9 / temp 0.7.
  Download once in the Local tab, then chat works fully offline.
- **Signing.** BouncyCastle RFC6979 ECDSA, low-S, DER, canonical
  JSON — audited line-by-line against the Python node and the
  Worker. The live proof is the first phone build: announce, get
  whitelisted, send one text job. A 401 "bad signature" means the
  audit missed something.

## Three-node setup

The reference setup is three nodes: this box, a laptop, and a
phone — all different nodes, one network.

1. Phone: build the Android app in Studio, open Identity, Announce,
   send the operator the phone's pubkey.
2. Laptop: run this tree, set a node name in the Node tab, Announce,
   send the operator the laptop's pubkey.
3. Operator: `python node.py --data-dir ./data peer allow <pubkey>`
   for each. Both devices can then use the operator node's Ollama
   chat, Z-Image queue, and PocketTTS bridge via node.darknet.ca.
4. Reciprocal: whitelist the operator's pubkey on your nodes, and
   the operator can use yours the same way.

No step requires anyone's approval but the serving node's own
allow-list. That is the whole access system.

## Models

- `LFM3-1.7B-Uncensored` — the local node's chat model and the
  phone's on-device model. HF:
  https://huggingface.co/AtomicChat/LFM3-1.7B-Uncensored-GGUF
  (Q4_K_M, 1,132,208,000 bytes; a Q6_K at 1.59GB exists but is not
  the default).
- `Ministral-3-3B-Reasoning-UNCENSORED-heretic` — the Hub's laptop
  chat default. HF:
  https://huggingface.co/virtuehearts/ministral-uncensored-gguf
- `EmbeddingGemma-2` — the Hub's embedding model (768 dims). HF:
  https://huggingface.co/unsloth/embeddinggemma-2-GGUF

## Tests

183 checks, all green at release (Hub 77, node 77, nodeapi 29).
From the tree root (`PYTHONPATH=.`, pytest not required):

```
python tests/test_gate.py tests/test_hfmodels.py tests/test_memory_vector.py
python tests/test_package.py tests/test_session_type.py tests/test_story_temporal.py
python tests/test_temporal.py tests/test_tui_chrome.py
python tests/test_node_client.py tests/test_tunnel.py tests/test_text_handler.py
python tests/test_transfer.py tests/test_image_capability.py tests/test_community.py
python tests/test_nodeapi.py
```

Node and tunnel suites run against an in-memory mock list
(`mock_list.py`); the tunnel suite's live bridge check needs the
PocketTTS bridge up on :8000. Test work dirs (`tests/run-*/`) are
scratch — delete before zipping. Never ship `data/`, `models/`,
or `__pycache__`.

## Known limits (honest)

- The Android app is source-only here; the APK comes from Android
  Studio, and the live Kotlin→Worker signature cross-check happens
  on the first phone build.
- Android TC fetch is phase 3: manifests render as text, not files.
- The phone app serves nothing (no serving poller); it announces
  no capabilities.
- Mailbox delivery is at-most-once and KV values cap at 25MB.
  Very large mailbox replies can be lost on flaky transports;
  large images go via TC instead.
- Nothing listens inbound anywhere. If a node cannot reach
  node.darknet.ca over HTTPS, it cannot participate.
- Mailbox polling defaults to 60s (120s on a free-tier list host);
  job latency follows the poll interval.
- Hub roadmap phases 4–5 (login-gated disk decryption, Solana
  credits) are not in this build.
- Lobby moderation is local-only in this build: Block hides a
  node's messages for you, on your browser. Deleting or blocking
  a node for everyone needs a Worker-side moderation endpoint and
  a decision on who the admins are — that is a protocol change,
  not shipped here.

## Security notes

- `identity.dat` is a private key. Never commit it, never paste it,
  never upload it. `data/` is excluded from release zips for exactly
  that reason.
- Memory is filtered by identity hash in every query. There is no
  command that lists another identity's memories.
- The Models tab token is per-request only: never stored, never
  logged.
- Signed packages never expose the private key; verification needs
  only the public key.
