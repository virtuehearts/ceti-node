# Uncensored Node 0.0.1

A local-first peer network for uncensored AI: chat, voice, and image
generation shared node-to-node, with no central server, no account,
and no permission gate. Each node runs on its owner's hardware and
decides for itself who may use it.

This is the **0.0.1 release pair**: one laptop build (Python,
stdlib only) and one Android build (Kotlin), versioned together.
It supersedes the 0.0.2–0.0.6 testing line — same code, clean
version. The 0.0.2–0.0.6 zips stay in Drive as superseded builds.

## 0.0.1-beta4 — 2026-11-15

What changed since 0.0.1-beta3: the CETI ladder redesign, from
Jero's call the same morning ("i honestly want all layers to
reason, and output the facts and knowledge, so the local model
can elaberate and make a construct of the answer").

- **All-layers-reason** (`hub/reasoning.py`, `hub/ceti_chat.py`,
  `tests/test_ceti_chat.py`): every layer outputs what it found
  and the local model constructs the answer from all of it.
  Tier 0 math is still computed in Python and fed in as a
  context fact ("- 17 * 23 + 41 = 432 (computed in Python)").
  Tier 1 facts (predicate-aware hit, competing candidates,
  two-hop composition) all become a FACTS section. Tier 2 AIML
  exact matches become AIML CONTEXT — with the RENDERED answer
  (star substituted), not the raw template, so "hello there
  friend" still carries "THERE FRIEND" into the model. Tier 3
  lexicon, Tier 4 vector, session files as before. The construct
  call (Tier 5, grounded-only prompt rewritten for construction,
  numbers guard) runs for EVERY question; the trace shows a
  CONSTRUCT line naming the sections handed to the model.
  Deleted: `_arbitrate_tier` / `_ARBITRATE_SYSTEM` (Tier 1b)
  and `_elaborate_tier` / `_wants_elaboration` /
  `_ELABORATE_SYSTEM` (Tier 1c) — the construct call does both
  for every question, so the separate selection and elaboration
  steps no longer exist.
- **Fallbacks, deliberately unchanged:** model down or empty
  answer -> `_direct_fallback` returns the best direct hit in
  old ladder priority (math, composition, fact, AIML template,
  first competing fact); no context at all -> cloud-suggested,
  model never called; ungrounded answers never recorded into
  vector memory.
- **Tests:** 41 CETI tests rewritten for the new ladder (the
  default model stand-in in setUp returns the assembled context
  as the answer, so tests assert on what the ladder fed the
  model without a live server; fallback paths get their own
  tests). Full suite 619 checks green (unit 331 + script 288).
- **Live verified 2026-11-15 on the Hub** (restarted, :8900):
  "What is the currency of Japan?" -> tier local-model, "Japan's
  currency is the Japanese Yen (symbol: ¥, code: JPY)" in 26s,
  trace shows FACTS (8 Japan facts), LEXICON, VECTOR MEMORY
  (5 nodes), AIML CONTEXT (1 template), CONSTRUCT (4 sections),
  numbers guard passed. "What is 17 * 23 + 41?" -> local-model,
  "According to the local math computation provided, 17 × 23 +
  41 = 432" in 28s. "Who won the 2026 World Cup?" -> Tier 5b
  local-model-ungrounded (live stores hold World Cup context,
  so the grounded call ran and refused, then the ungrounded
  call answered from the model's own knowledge — stale, it
  says the tournament "has not taken place yet", which is the
  small model's knowledge, honestly labelled UNGROUNDED).

## 0.0.1-beta3 — 2026-11-15

What changed since 0.0.1-beta2: one CETI ladder fix, from Jero's
laptop testing on beta2 (screenshot, 2026-11-14 19:23 EST).

- **CETI Tier 5b — the loaded model answers when local knowledge
  has no hit** (`hub/reasoning.py`, `hub/ceti_chat.py`):
  "please tell me the best way to setup a bbs" returned "I don't
  know from local knowledge." The trace proved the local model WAS
  called — Tier 5 ran, grounded in LEXICON DEFINITIONS, AIML
  CONTEXT (4 BBS templates: decline, peak usage, first BBS, early
  software), and VECTOR MEMORY (5 nodes) — but the grounded system
  prompt forbids the model from answering from its own weights
  when the assembled context does not contain the answer, and
  none of that context contains setup steps. So the model
  followed its instructions and refused. Tier 5b now catches
  that exact refusal: the loaded model is asked again with a
  prompt that permits answering from its own knowledge, and the
  result is labelled `local-model-ungrounded` in tier and
  evidence ("local model, UNGROUNDED — from the model's own
  knowledge, not local stores"), with numbers_grounded False.
  Verified live on this box via `POST /api/ceti/chat`: the BBS
  question answers, tier local-model-ungrounded, 187.8s for the
  two LFM3 calls on a loaded CPU box (the first attempt timed
  out at a 180s client limit and was retried at 600s). Honest
  caveat, kept in the README too: the 1.7B's answer was usable
  but drifted into node setup ("download the model files",
  "announce your node") — the mechanism is proven, the answer
  quality is the small model's; a GPU model (the laptop's
  Ministral 3B) should do better through the same path.
- **Deliberately unchanged:** (1) the empty-context guard — when
  no grounded context exists at all, the model is still never
  called and the cloud suggestion stands (two existing tests
  guard this rule and still pass); (2) ungrounded answers are
  never recorded into vector memory — the record gate only
  accepts facts/aiml/local-model/math/facts-composition, so
  model output from its own weights can never become local
  knowledge; (3) grounded answers, precise questions, and the
  zero-token direct tiers are untouched — a grounded answer
  still ends the ladder after one model call. (4) An
  arbitration refusal ("none of the candidates answers") now
  falls through to the lower tiers instead of being returned as
  the final answer.
- **Tests:** 619 checks green (unit 331: CETI 33, up from 29 —
  4 new tests: grounded refusal falls back to ungrounded, a
  grounded answer makes exactly one model call, model-down on
  the fallback goes to the cloud suggestion, arbitration refusal
  falls through; plus the record gate now also rejects
  local-model-ungrounded; script suites 288, unchanged).

## 0.0.1-beta2 — 2026-11-14

What changed since 0.0.1-beta, same day: the SSH terminal and the
README. Both were Jero's call on 2026-11-14, after beta shipped.

- **SSH on Windows: default back to the pipe session**
  (`hub/sshterm.py`, `hub/web.py`, `hub/web_ui.html`,
  `tools/conpty_selftest.py`): ConPTY was the Windows default
  from 0.0.1u through beta and never delivered a byte on the
  laptop — every run was a live pid with empty pipes ("no output
  yet"), including 0.0.1u's "conpty pid 13452 · no output yet",
  even after 0.0.1v aligned the call with Microsoft's ConPTY
  sample (SW_HIDE only, no CREATE_NO_WINDOW, bInheritHandles
  True, mutable command line, console-owned pipe ends closed in
  the parent). The pipe session, by contrast, rendered the real
  chat.hf.co TUI in the 0.0.1r–t era. So the Windows default is
  the pipe session again (`_make_session(..., use_conpty=False)`;
  ConPTY-first lives on in `_make_windows_session`, reached only
  when the operator opts in). The opt-in is a checkbox in the
  SSH pane, "ConPTY (experimental)", sent as `use_conpty` to
  `/api/ssh/connect` and passed through `SshManager.connect`.
  ConPTY remains the only path with real resize
  (ResizePseudoConsole); the pipe session's resize still only
  records, and its connect-time size comes from the pane
  measurement plus COLUMNS/LINES (0.0.1t console-inherit when
  the Hub was launched from a console).
- **Exit-code diagnostics** (same files): the old status line
  could not tell an ssh that exited from a live process whose
  PTY never delivered — both read "no output yet". Every
  session type now exposes `exit_code` (None while running;
  the real wait status once it ends), `/api/ssh/status`
  returns it, and the UI polls every 2s instead of one check
  at 3s. A session that ends with zero bytes now names its
  exit code in the status line (255 = ssh failing at the
  network/auth layer; a live pid with zero bytes = the PTY
  never delivering) and points at `tools\conpty_selftest.py`.
  Verified live on this box: a connect to 127.0.0.1:22 (no
  sshd) reports `alive: false, exit_code: 255, buffered: 61`
  — ssh's "Connection refused" text in the buffer, code
  surfaced. The selftest gained `ssh` and `ssh-pipe` modes
  that run the real ssh argv to chat.hf.co and report
  bytes/alive/exit code, so the next laptop run separates
  "ConPTY is broken" from "ssh itself failed".
- **Build display fix** (`hub/web.py` `_read_build`): the
  status line showed a bare "0.0.1" for the whole beta line
  because the version regex (`0\.0\.1\w+`) stopped at the
  hyphen in "0.0.1-beta". It now matches hyphenated suffixes
  (`0\.0\.1[\w-]+`), so beta2 displays as "0.0.1-beta2".
- **README rewritten to the CETI public story** (Jero's call):
  the top narrative now follows ceti.darknet.ca — local-first,
  peer-to-peer, local-blockchain-ledger AI; data sovereignty
  and intelligence sovereignty as the two parts; darknet.ca
  since 2002; the original X handle @darknet_ca (since 2008)
  and the new @virtue_hearts; retro but polished, with a
  small ASCII banner. All CETI wording is quoted from the live
  page; the X post itself would not load, so no wording from
  it is quoted. Install/quickstart, download link, version
  line, and Tests section were kept intact through the rewrite
  and updated here for beta2.
- **Tests:** 615 checks green (unit 327: SSH terminal 37, up
  from 29 — 6 routing/exit-code tests + 2 endpoint tests for
  the build display and the status exit code; script suites
  288, unchanged). JS syntax checked with node; the Hub was
  restarted to the beta2 code and verified on :8900.

## 0.0.1-beta — 2026-11-14

What changed since 0.0.1n — the two things Jero's laptop testing
caught on 2026-11-14, both fixed and verified live before packing:

- **CETI elaboration (Tier 1c)** (`hub/reasoning.py`):
  a clear fact hit returned the bare fact verbatim even when the
  sysop asked for more. "tell me more about fidonet" matched one
  fact and came back as one line; the local model never got the
  hit knowledge to build on. That is CETI's main angle — previous
  hits plus reasoning — so Tier 1c now feeds every matched fact
  plus the related AIML templates to local LFM3 (thinking OFF,
  max_tokens 768) whenever the question explicitly asks for more
  ("tell me more", "more about", "tell me about", "explain",
  "elaborate", "describe", "what else", "history of", ...), and
  the model composes the fuller answer from local knowledge only.
  A precise question with no elaboration phrase still returns
  the bare fact directly, zero model tokens — including a
  subject-only hit ("when did FidoNet start operating?"). An
  open-ended "Tell me about Japan" now elaborates over all three
  competing facts instead of arbitrating down to one; a plain
  ambiguous question ("What is Japan?") still arbitrates. Model
  down / empty / "nothing more" falls back to the direct fact;
  the numbers guard still applies to elaborated answers. Live
  on the tunnel: "tell me more about fidonet" → tier
  `elaborated`, 26.0s, answer built from 1 fact + 4 AIML
  templates (Christmas 1983, Tom Jennings, night-time calls,
  20,000 boards, 40,000-node peak, Telnet today), numbers guard
  passed, ledgered. Precise questions still answer in 0.16–0.30s.
  New tests: 4 (elaborates on a fact hit, falls back when the
  model fails, precise question stays direct, "tell me about"
  elaborates over competing facts); the arbitration test now
  uses "What is Japan?" so the two paths stay distinct.
- **Packaged TTS fixed** (`hub/servers.py`, `hub/web.py`,
  `hub/web_ui.html`, `tts/requirements.txt`, `tts/start_tts.sh`,
  `tts/start_tts.bat`, `tts/README.md`): what happened in the
  newest release is that nothing in `tts/` changed — the bundle
  is byte-identical from 0.0.1k to 0.0.1n, and the breakage was
  in the paths around it. (a) The Hub's START button called
  `Popen([start_tts.bat])`; Windows refuses a bare .bat
  (WinError 193, "not a valid Win32 application"), the exception
  was caught, and the UI printed "Starting TTS server…" anyway —
  on the laptop START never launched anything. `tts_start()`
  now launches through `cmd /c`, the same as autorun.py (which
  was already correct — only the Hub path was broken). (b) The
  first-run install floated `torch` / `pocket-tts` / `scipy`
  unpinned, so a fresh extract installed whatever was newest
  that day; all three are now pinned to the versions verified
  with this bridge (torch 2.14.0+cpu, pocket-tts 3.2.0, scipy
  1.18.1, numpy 2.5.3, python-multipart 0.0.32). (c) Both start
  scripts used to run on past a failed pip install (the .bat had
  no error checks at all) and die in uvicorn; they now fail
  loudly, never write the installed marker on failure, and run
  an import smoke check before serving. (d) The System view now
  distinguishes STARTING (a start is really running, PID alive)
  from DOWN, and shows the last bridge-log lines while TTS is
  down — a failed first-run install no longer looks like "still
  building". The new `/api/tts/status` fields are `starting` and
  `log_tail`. Verified: the packaged `tts/` tree boots standalone
  on the pinned set (`/health` ok, model ready) and synthesizes
  mya-03 (3.12s MP3 for one sentence). Honest limit: a prebuilt
  venv cannot ship in the zip (torch CPU alone is ~1 GB and
  platform-specific), so a fresh extract still builds the venv
  and downloads the ~215 MB model on first run — now pinned,
  checked, and visible instead of silent.

## 0.0.1n — 2026-11-09

What changed since 0.0.1l:

- **Night-shift research bundle** (`brain/cassettes/night-shift-2026-11-08.aiml`):
  67 facts + 38 QA approved across 13 topics (Ethereum, Bitcoin,
  secp256k1, Gopher, Tor, IPFS, llama.cpp, Rust, WireGuard, Cloudflare
  Workers, darknet.ca dates, Darknet Diaries, Jack Rhysider), plus 183
  Darknet Diaries episode facts extracted deterministically from the RSS
  feed. Every approved claim was verified against a live source (RDAP,
  Wikipedia, project docs, the feed itself). 107 proposals were left
  pending, unverified, and are NOT in the bundle. The local LFM3
  distiller drifted on several topics (Gemini for Gopher, Tor
  disambiguation page, Tailscale for WireGuard, Deno for Workers);
  repairs used hand-built lead-prose reports + verified extraction.
- **Modem research bundle** (`brain/cassettes/modem-research-2026-11-09.aiml`):
  49 facts + 52 QA approved across 7 topics (2400 baud BBS era, V.32bis
  14.4k, V.34 28.8k/33.6k, V.90/K56flex/X2 56k war, Hayes AT, USR
  Sportster/Courier, dial-up timeline). Riskiest dates spot-checked
  against live sources: V.32bis approved 22 February 1991 (the ITU text
  itself), SupraFAXModem January 1992 at $399 (the original press
  release), Bell 212A November 1976, Courier sold to corporate customers
  from 1979, X2 as twice 28.8k, dial-up 40% to 3%. One proposal (a
  FidoNet fact that drifted into the Hayes AT distill) is quarantined
  pending and NOT in the bundle. Distill notes: thinking OFF,
  max_tokens 3000 (2012 truncated one topic); LFM3 JSON needs 3-stage
  parsing (direct, sanitize `\'` escapes/trailing commas, per-item
  salvage); distills run ~2–4.5 min each on CPU, sequential only.
- **jero_worker_mips descriptor** (`brain/nodes/jero_worker_mips.json`):
  public descriptor (pubkey only, no secret) for the simulated Cobalt
  Qube node used in the node-to-node transfer test. The node itself is
  deliberately NOT announced to the live list.
- **Test expectations updated for the new knowledge** (`tests/test_brain.py`,
  `tests/test_ceti_chat.py`): the packaged-seed module count goes 31 to
  33 (the two new cassettes), and the FidoNet AIML top-four now leads
  with the modem bundle's verified FidoNet category (it outranks the
  bare-number atomic fact 'Over 120,000'). Both are consequences of the
  new bundles, not regressions. The script suites need `PYTHONPATH` set
  to the tree root (`import node` lives there); run from `tests/` with
  `PYTHONPATH=..` or the import fails.
- **Letter note:** this pack skips 0.0.1m. The Oct 31 0.0.1m zip was a
  broken build (unguarded top-level `import fcntl` in `hub/sshterm.py`,
  crashed on the laptop 2026-11-01); it is archived at
  `~/workspace/archive/laptop-uncensored-node-0.0.1m-BROKEN-oct31.zip`
  and its Drive copy stays untouched. A first-cycle 0.0.1n zip from
  Oct 31 (681KB) was also archived out of the way; its Drive copy
  remains in the old-zips folder, a different folder from this release.
- **Tests:** 595 checks green (unit 307 + script 288), same as 0.0.1l:
  no new tests, the two expectation updates above.

## 0.0.1l — 2026-11-08

What changed since 0.0.1k:

- **CETI arbitration (Tier 1b)** (`hub/reasoning.py`,
  `hub/ceti_chat.py`): when several facts or AIML patterns match
  a question, the local model now arbitrates over all of them
  instead of the ladder taking the first. `_facts_tier` now
  returns its candidates alongside the clear-hit answer, and
  `ceti_chat._aiml_all_matches` returns every matching pattern
  (`_aiml_tier` is now a thin wrapper over it). A single clear
  fact or pattern still returns directly — fast, zero model
  tokens. Only genuine ambiguity (several facts sharing a
  predicate, no predicate word with several subject matches, or
  several AIML patterns) feeds the candidates to the local model
  via `_arbitrate_tier`: thinking off, max_tokens 512, system
  prompt restricted to the provided knowledge only. If the model
  is down or answers empty, the ladder falls through to the
  lower tiers (lexicon, vector, session files) instead of failing
  the question. `_model_tier` gained optional `system` /
  `max_tokens` / `reasoning` parameters for the arbitration call;
  the regular Tier 5 path is unchanged. Verified live on this
  box: "Tell me about Japan" arbitrated over 8 facts into one
  composed answer (capital, currency, region, population,
  language) in 14–20s across two runs; "What is the currency of
  Japan?" still answers in 0.1s with no model call. Caveat: the
  numbers guard flagged the model's "about 123 million" rounding
  of 122,950,000 — a defensible rounding, but the guard is
  strict/literal and will not vouch for it.
- **Test-isolation fix** (`tests/test_ceti_chat.py`):
  `test_nodeapi.py` sets HUB_DATA_DIR at import time, which
  clobbered this module's TMP whenever it was imported after it
  under `discover`, so the two attachment tests failed with 0
  hits (a pre-existing failure, unrelated to the arbitration
  work). `AttachmentContextTest` now pins HUB_DATA_DIR back to
  its own TMP in setUpClass and restores it in tearDownClass, so
  the suite is green in any import order.
- **Tests:** 595 checks green (unit 307 + script 288), up from
  589: 6 new arbitration tests (clear fact/AIML hits never call
  the model; ambiguous facts and ambiguous AIML patterns
  arbitrate; arbitration failure falls through; `_aiml_all_matches`
  returns every match).

## 0.0.1k — 2026-11-07

What changed since 0.0.1j:

- **TTS server in System** (`hub/servers.py`, `hub/web.py`,
  `hub/web_ui.html`, `autorun.py`, `hub/nodeapi.py`): the Servers
  card gains a TTS SERVER row (Start/Stop, status from the bridge's
  `/health`) under the node poller, and a voice panel below Refresh
  servers — the container's old voice panel, in the Hub. The panel
  lists the voices the bridge knows (read from its `voices.db`,
  built-ins mya-02/mya-03 when the DB is missing), speaks a test
  line in the selected voice (real MP3 via `/api/voice/local`,
  waveform player), and sets the default voice. `set_default_voice`
  writes ONLY `tts_voice` — `set_serving` would pop text_backend
  and blank text_model if posted a voice alone. The bridge start
  records its PID (`data/tts.pid`; autorun writes it at boot too),
  so Stop works on the boot-started bridge. Verified live: status
  lists mya-02/mya-03, voice test returns a real 28,845-byte MP3,
  default voice flips mya-03 → mya-02 → mya-03, Stop kills the
  bridge (health down), Start relaunches it. Caveat, this box
  only: the bridge here predates the tts/ bundling and runs from
  `~/workspace/venvs/bridge`, so a UI Start runs `start_tts.sh`
  and creates `tts/.venv` (first-run setup); on a fresh laptop
  extract that is the intended path.
- **Tests:** 589 checks green (unit 301 + script 288), up from
  577: 12 new TTS control tests (voices.db read + fallbacks,
  start/stop PID handling, default-voice preserves serving).

## 0.0.1j — 2026-11-07

What changed since 0.0.1i:

- **llama-server auto-install fixed for Linux/WSL** (`hub/llama.py`):
  the 0.0.1xa laptop run died with "could not discover a llama.cpp
  release asset" and no chat server, because llama.cpp switched its
  Linux packages from `.zip` to `.tar.gz` and `_pick_asset` only
  accepted `.zip` — every Linux asset was silently rejected (Windows
  `.zip` still worked, so it only hit Linux/WSL). The picker now
  accepts `.tar.gz` / `.tgz` / `.zip` for non-Windows, the installer
  extracts tarballs with `tarfile`, and it flattens the `llama-bXXXX/`
  subfolder the tarball extracts to (binary + its `.so` libs land in
  `bin/` together, since the binary's RUNPATH is `$ORIGIN`). The
  plain CPU build (`llama-*-bin-ubuntu-x64.tar.gz`, no 'cpu' in the
  name) now matches the CPU fallback instead of being skipped.
- **Version read from stderr** (`hub/llama.py`): `llama-server
  --version` prints to stderr, not stdout. `server_build()` only
  read stdout, so it reported 'unknown build' and the fail-closed
  gate skipped a perfectly good binary even after a successful
  download. Both streams are now read. Verified end to end: real
  download of the current Vulkan build (b12192), installs, reports
  build 12192, and `find_server()` finds it.
- **Tests:** 577 checks green (unit 289 + script 288), up from
  570: 7 new tests (4 install: tarball accept, plain-Ubuntu CPU,
  CPU fallback skips accelerated builds, tarball flatten; 3 gate:
  version on stderr, version on stdout, no version output).

## 0.0.1i — 2026-11-07

What changed since 0.0.1h:

- **Presence: the list is live, not a roster** (`worker-node-list.js`,
  `node.py`, `tunnel.py`, `run_poller.py`, `mock_list.py`): a node
  that announces once used to stay listed forever — with 50 people
  the list fills with dead nodes. Each announce now stamps a server
  `seen` time; GET /nodes only returns nodes seen within 15 min.
  Nodes heartbeat by re-announcing every 5 min (the poller does it
  in run_forever, starting on launch), so a live node stays listed
  and a dead one drops off within 15 minutes. Nodes are sharded
  (one `node:<pubkey>` KV key each) instead of one `nodes` key, so
  an announce never rewrites the whole list (the old single key
  was a read-modify-write race). `run_poller.py` gains `--name` and
  announces the capabilities actually wired (text/tts/image), so
  the list says what the node really offers. The Worker is NOT
  deployed in this zip — it ships as `worker-node-list.js` +
  `wrangler.toml` for a separate deploy; until then the live list
  host still runs the old Worker (no TTL, no rate limit).
- **Rate limiting** (`worker-node-list.js`, `mock_list.py`): anyone
  listed could send unlimited jobs, so one buggy client in a retry
  loop could burn the shared KV write budget or flood mailboxes.
  Each send now counts against its sender's quota: 600 per 10 min,
  429 over it, keyed by the verified pubkey (checked after the
  signature, so a forged request cannot burn someone else's
  quota), failing open if KV hiccups. Polls are deliberately not
  rate-limited — an empty poll writes nothing, so a KV counter per
  poll would cost more writes than it saves.
- **15s poll for testing** (`run_poller.py`, `start_mya_node.sh`):
  the poll interval is now 15s on this box during testing (Jero
  2026-11-07) for snappier jobs (pickup ~15s instead of ~60s).
  Idle polls cost 0 KV writes since the empty-poll fix, so the
  interval drives reads/requests/latency, not the write budget.
  The shipped default stays 60s.
- **Two bugs caught by the new tests:** (1) the Worker's first
  `seen` was stored as an ISO string and subtracted from
  `Date.now()` — `NaN` in JS, so entries would never expire;
  fixed to `Date.parse` before deploy. (2) `fetch_transfer`'s
  manifest fetch sat outside the download try-block, so a dead
  outbox raised instead of returning a clean refusal; fixed in
  both trees. (3) Unknown capability is now fail-loud, not a
  silent skip: the poller replies `{"error": "capability 'X' not
  offered by this node"}` so the sender's UI can show why there
  is no answer (the silent skip made voice jobs "never show").
- **Tests:** 570 checks green (unit 282 + script 288), up from
  478: 3 new node suites (two-nodes 36, edge cases 35, presence
  21) now ship in `tests/` with the plan in `TEST-PLAN.md`. The
  real Worker JS was verified locally via `wrangler dev`.

## 0.0.1h — 2026-11-07

What changed since 0.0.1g:

- **Reasoning budget 512** (`start_mya_node.sh`, `hub/servers.py`,
  `hub/research.py`, `hub/web.py`): the local LFM3 server now
  runs `-c 4096 --reasoning-budget 512` instead of `--reasoning
  off`. Unrestricted thinking traps the model (0 content, 13,190
  chars reasoning at 3000 tokens); reasoning off fixes the empty
  output but the distill drifts off-topic; a 512-token budget is
  the sweet spot — the Nobel distill probe gave 2,811 chars of
  content, 8 QA + 8 facts, the first QA naming all three 2024
  winners (better than off's 6 QA; budget 256 gave 0 proposals,
  the forced cutoff broke the output). The reasoning budget
  counts towards max_tokens (a sub-cap within the total), so
  callers size max_tokens as budget + answer: the research
  distill uses 512 + 1500 = 2012, and the chat fit-budget adds
  the 512 thinking room on top of the user's answer budget
  ("don't count reasoning towards the limit").
- **Focused local report** (`hub/research.py`): the local distill
  now leads with the topic's table rows plus a short lead of
  topic sentences only (339 chars for Nobel), dropping the
  general lead prose that made the model drift. The cloud path
  is untouched (12000 chars, full report). Combined proof
  (task 6, live :11436): 11 proposals, all about the 2024
  winners, zero 2026, all three winners in the QA. Honest gap:
  the headline QA still leads with Hassabis alone (he appears
  in two row sets); the other two are in the remaining QA/facts.
- **The Thinking toggle works** (`hub/web_ui.html`, `hub/web.py`,
  `hub/servers.py`): the switch used to only hide the thinking
  panel — the model still thought. It now sends `thinking` to
  `/api/chat` and the local model honours it per request via
  `chat_template_kwargs enable_thinking=false` (proven on a
  test server: reasoning ON 0 content/349 reasoning chars,
  OFF 288 content/0 reasoning). For cloud reasoning models
  (Nvidia lightning-30b-a3b, nano-30b-a3b, "reasoning" substring)
  the toggle is greyed out with "Reasoning cannot be turned off
  for this model"; bolt-e4b and local are not greyed out.
- **Tests:** 478 checks green (unit 282 + script suites 196),
  up from 469: 3 new focused-report tests, 6 new toggle tests.

## 0.0.1g — 2026-11-07

What changed since 0.0.1f:

- **Bundled Mya TTS** (`tts/`, `autorun.py`): the PocketTTS bridge
  now ships inside the release as a self-contained unit — the
  bridge code (`app/`, `src/`), the mya-02 and mya-03 voices
  (samples + embeddings, 38M), a fresh `voices.db` with only those
  two voices (the live bridge's admin password hash was not
  copied), `start_tts.sh` / `start_tts.bat` (create a venv once,
  pip-install once, `ENABLE_AUTH=false`, `HF_HOME` under
  `tts/data/hf-cache`), and the `Dockerfile` as an alternative for
  Docker users. The autorun starts it alongside the Hub
  (fail-soft: a missing tts/ folder or a failed start never blocks
  chat). The kyutai/pocket-tts model (~215M) is NOT shipped — it
  downloads from Hugging Face on first run, same rule as the Hub's
  models. Proven live: the bundled copy on port 8001 answered
  /health with model ready and synthesized a 25,101-byte MP3 in the
  mya-03 voice; the voices list shows mya-02 and mya-03 (mya-01
  and mya-04-test absent by design). `tests/test_tts_bundle.py`
  (11 tests) guards the bundle. The pack script now includes
  `tts/` (220 entries in the throwaway z pack, no bad entries).
- **Local distill — context limits tested and refuted** (Jero's
  hypothesis): raising the local context to 8192 and the output
  budget to 3000 tokens does NOT fix the local distill. A test
  llama-server on port 11438 ran the same Nobel distill prompt and
  still returned empty content with 13,190 chars of reasoning —
  the model just thinks longer with a bigger budget. A plain-text
  (non-JSON) prompt did the same (0 content, 2,200 chars
  reasoning). The limits were NOT raised: they would only waste
  more time and compute. Cloud remains the working distill path
  when a key is configured. The known-limit note in the README now
  carries both numbers.

## 0.0.1f — 2026-11-07

What changed since 0.0.1e:

- **Attachments** (`hub/attachments.py`, `hub/web.py`, `hub/web_ui.html`):
  a paperclip in the chat composer attaches files into the session's
  context. Text and PDF files are chunked (1500 chars), embedded via
  the local embedding server, and retrieved by the same vector bar as
  memories (top-5 session-file hits >0.5, local chat only); images are
  stored and shown. Proven live: the local model answered a question
  from an attached manual with the exact detail (zorp test).
- **Expandable code box** (`hub/web_ui.html`): long ``` code blocks
  render collapsed (~170px) with language, copy, and expand, in chat
  and in attachment rendering. Verified at 412×915: collapsed 170px,
  expanded 432.5px, toggle flips to "collapse".
- **Research mode** (`hub/research.py`, `hub/web.py`): when CETI can't
  answer, a "Suggest research job" button queues a web job (Wikipedia
  API first, Google second); the agent fetches the pages, distills QA
  pairs + fact triples, and lands them as pending proposals for a human
  to approve — nothing auto-promotes. Fixes since the first cut: the
  distill report cap (8000 → 4000 → 2000 chars), the zombie-claim
  (`force=True` may claim pending/needs-model/running; the tick loop
  claims only 'pending'), Wikipedia nav stripping (`_article_body`),
  wikitext table-row extraction (answers that live in tables — the
  2024 Nobel winners), and cloud-first distill with a 12000-char
  report. Two extraction fixes landed after the first cloud run:
  title words are dropped from the table keywords (a generic word
  like "Chemistry" flooded the cap with intro lines), and when the
  topic names a year the table rows come from that year's block
  alone, so the distill cannot attribute a 2017 laureate to 2024;
  the distill prompt now carries the topic so it prioritizes the
  pairs that answer it. Research tests 39/39.
- **Cloud context lifted** (`hub/web.py`): cloud chat now injects
  session files with a 30000-char budget, top-10 chunks (local keeps
  3000 / top-5 for its 2048-token context). The long-term vector
  memory stays local-only and never goes to a cloud provider.
- **Known limit:** the local model returns empty content for the JSON
  distill prompt — a direct test with a 937-char prompt and 1270
  max_tokens returned finish=length, content '', reasoning 5094 chars
  (the whole budget burned thinking). Cloud is the working distill
  path when a key is configured. The Nobel task (task 2) is proven
  end to end: fetch (5 pages) + year-block table rows + cloud distill
  (bolt-e4b, topic-prioritized) produced 16 pending proposals, the
  first naming the correct 2024 winners (David Baker, Demis Hassabis,
  John M. Jumper) — no misattribution (the earlier Henderson-2017
  error is gone). The human gate (approve/reject in the Research tab)
  is live with real proposals to act on.

## 0.0.1e — 2026-11-06

What changed since 0.0.1d:

- **Session chips are back on phones** (`hub/web_ui.html`,
  ≤720px block): the 0.0.1c chat-first pass hid every `.chan`
  behind the More-sessions drawer (`.chan-list .chan{display:none}`),
  so the workspaces vanished from the top of the phone screen.
  The chips show again as a sideways-scrolling strip of the 3
  most recent sessions; More sessions and New workspace stay in
  the strip, and the drawer still holds the rest. Verified at
  412×915: 3 chips visible, strip scrolls (scrollWidth 1214 vs
  412), chat 733px/915 = 80%.
- **Generated voices persist** (`hub/web.py` + `hub/web_ui.html`):
  the waveform player used to live only in the DOM — its base64
  was deleted after wiring and the blob URL died on reload — so
  leaving the session or the page wiped it. New endpoints
  `POST /api/chat-voice/save` and `GET /api/chat-voice/<name>`
  mirror the chat-images pattern (MP3 only, ID3 or frame sync,
  20MB cap, `data/chat-voice/`); the filename is stored on the
  message (`voice` field, saved verbatim by `/api/sessions`) and
  `addMsg` re-renders the player from the stored file on session
  open (`initWavePlayers` gained a URL mode that fetches the
  file, decodes it, and draws the waveform). Covered paths: the
  local `/api/voice/local` path and both mailbox-delivery points
  (auto-poll and manual Fetch). Verified at 412×915: voice
  generated on the S·02 LFM reply (322,605 B MP3), player still
  there after a session switch AND after a full reload, waveform
  drawn (10,608 cyan px), Play flips to Pause, Save links to the
  stored file.
- **Mobile-size audit** (`hub/web_ui.html`): the viewport meta
  is correct and one 720px breakpoint decides the layout —
  phones get the mobile UI, desktop resolutions get the sidebar
  UI. Two bugs found and fixed: at 360px wide the SEND button
  was pushed off-screen (the composer flex items couldn't shrink
  — `.prompt-wrap` had no `min-width:0`; fixed with `min-width:0`
  on the input wrap and `flex:0 0 auto` on SEND and the image
  toggle), and the Node tab used a 700px breakpoint while chat
  used 720px (aligned both to 720px). Verified at 360/412/710/
  721/768/1024/1280/1920: the right UI at every width, no page
  overflow, no JS errors, SEND reachable everywhere.
- **423 checks green** (227 unit + 196 script), re-run with all
  three fixes in the tree.

## 0.0.1d — 2026-11-06

What changed since 0.0.1c:

- **Generate voice works on a fresh browser** (`voiceForText` in
  `hub/web_ui.html`): the peer resolution used to stop dead with
  "Pick a peer first" whenever the Jobs peer box was empty and no
  default voice peer was saved — which is every fresh browser and
  every new phone. The peer is now resolved inside the
  `ownPubkey()` step, and when nothing is set it defaults to this
  node, so Generate voice goes straight to the local
  `POST /api/voice/local` path; a peer typed in Jobs or saved as
  the default still wins, and the "Pick a peer first" message only
  remains for the case where the node's own pubkey can't be read
  at all. Found by using the Hub at hub.darknet.ca end to end:
  the cloud browser's Jobs peer box was empty, no default was
  saved, and the voice peer had to be set by hand before Generate
  voice could run. Verified with a fresh browser and no peer set:
  waveform player with a real 25,101-byte MP3, 2.0 seconds; the
  direct endpoint still answers in ~2s (mya-03 25,101 B,
  mya-02 32,589 B).
- **423 checks green** (227 unit + 196 script), re-run with the
  fix in the tree.
- Note: the same evening's browser test also saw the France chat
  reply stall for 17+ minutes and both Generate-voice clicks fail
  with "Failed to fetch" — that was a transport stall on the
  tunnel path, not Hub code: the tunnel held its connections, and
  the same requests answered locally in 15.1s (chat) and ~2s
  (voice). A later VM replacement took the tunnel down entirely
  (hub.darknet.ca Error 1033); the watchdog restart brought the
  stack back and the public page was verified loading again.
  Operational history, not a code change — nothing in this
  letter touches the tunnel.

## 0.0.1c — 2026-11-06

What changed since 0.0.1b:

- **TTS voice works on the web UI** (`POST /api/voice/local`,
  `hub/nodeapi.py: voice_local` + `hub/web.py` route): the Hub
  synthesises directly through the local PocketTTS bridge and
  returns the MP3 base64 in the same result shape the poller's
  bridge_tts returns — no mailbox round-trip, so the reply can't
  be lost to a truncated proxy fetch. `voiceForText` in
  `hub/web_ui.html` uses the local endpoint whenever the voice
  peer is this node (own pubkey from `/api/node/status`), and
  the Generate-voice button now reads the article's live `<p>`
  text at click time — the old button closed over the `addMsg`
  text parameter, which is empty for streamed replies, so every
  streamed reply voiced "" ("voice needs text"). Verified on
  the box: direct endpoint 3.7s, 37,485 B, 3.04s MP3; mobile
  Playwright at 412×915: chat → Generate voice → waveform
  player with a real decoded envelope.
- **Mobile UI is chat-first** (`hub/web_ui.html` ≤720px block):
  chat is 70% of the screen at 412×915 (was 62%). Session chips
  hidden (the drawer is the switcher), session header one line,
  provider selects share a row, `#topStatus`, `.controls.live`,
  `.outNote` and `.statusbar` hidden on phones, and the
  image-peer row tucked behind a ▸ toggle beside the input.
- **Nvidia cloud chat:** a custom model input in the provider
  bar (`cloudModelCustom`, `cloudModelActive`,
  save/restore in `hub/web_ui.html`) — type any model id, it
  overrides the list and is saved per-provider in localStorage;
  and the suggest fallback in `hub/cloud.py` is now three models
  verified working on a free key: nemotron-3.5-super-vl-120b-a12b,
  nemotron-3.5-lightning-30b-a3b, nemotron-3.5-bolt-e4b (the old
  fallback models were retired by Nvidia and returned 410).
- **Math tier fix found during release verification:** the letter
  `x` as a multiplication sign ("17x23+41") used to answer 64 —
  the `x` was never converted, so the expression regex dropped
  "17x" and ran 23+41. The `if False` disablement is replaced by
  a digit-guarded substitution (only between digits, so "box"
  and "six" are untouched). `17x23+41`, `17 x 23 + 41` and
  `17X23+41` all answer 432 now; covered in `test_math_tier`.
- **423 checks green** (227 unit + 196 script).
- Note: the box's `start_mya_node.sh` section 5 (Hub UI with
  `HUB_CHAT_PORT=11436`) is box-only and not in this zip; the
  shipped laptop flow (autorun → `ensure_servers`, chat on
  8080) is unchanged and self-consistent.

## 0.0.1b — 2026-11-06

What changed since 0.0.1a2:

- **Reasoning ladder wired into CETI chat** (`hub/ceti_chat.py`
  → `hub/reasoning.py: answer_with_reasoning`, CETI tests 13 →
  19): Python math first, predicate-aware facts + 2-hop
  composition, AIML, lexicon expansion (Open English WordNet
  2025 primary, Moby Thesaurus II fallback), vector memory, a
  guarded local model, and cloud-suggested-only-never-called.
  Fixes the facts tier ignoring the predicate — currency of Japan
  used to return "Tokyo", population of France "Paris". The
  ladder was measured on a 13-question local-only test:
  prototype 10/10 on auto-checkable questions (old ladder 8/13).
- **Empty-context guard fixed:** a vector hit only counts as
  context when it shares a real content word with the question;
  otherwise the ladder says it doesn't know from local knowledge
  instead of answering from nothing. Same return shape the UI
  expects, plus `numbers_grounded`.
- **423 checks green** (227 unit + 196 script).
- Note: the box's `start_mya_node.sh` section 5 (Hub UI with
  `HUB_CHAT_PORT=11436`) is box-only and not in this zip; the
  shipped laptop flow (autorun → `ensure_servers`, chat on
  8080) is unchanged and self-consistent.

## 0.0.1a2 — 2026-11-05

The letter scheme ran out at z, so builds now take a suffix
(a2 follows z). What changed since 0.0.1z:

- **Code agent** (`hub/codeagent.py`, 9 tests): a cloud model
  drives allowlisted file tools in a sandboxed work folder
  (`data/code-work/<session>/`) — search, read, edit, syntax
  check — seeded from the tree, never data/, identity, or
  models/. OpenRouter, OrcaRouter, and Nvidia providers.
- **Code workspace** (built by the agent on camera, 6 tests):
  file picker, monospace editor, Load / Save / Run check, via
  `GET /api/code/files`, `GET /api/code/file`,
  `POST /api/code/file`, `POST /api/code/check` (traversal
  blocked, 500KB cap so the ~230KB `web_ui.html` loads).
- **Copy code and Clear buttons** on the code output.
- **Version reader** (`_read_build`) now takes a suffix
  (`0.0.1\w+`), so 0.0.1a2 shows in the UI.
- **417 checks green** (221 unit + 196 script).

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
