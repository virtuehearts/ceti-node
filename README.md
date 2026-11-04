![CETI: Uncensored Node — pixel banner by darknet.ca labs](docs/images/banner.png)

# CETI: Uncensored Node — Sovereign AI, run on your own iron

**Built by [darknet.ca labs](https://darknet.ca) — Jero (drkkrobot, aka [Virtuehearts](https://github.com/virtuehearts)), Toronto, Canada.**

CETI: Uncensored Node is a local-first AI network. Every node runs on
its operator's own hardware, holds its own keys, and keeps its own
data. No accounts, no cloud dashboards, no one else's model shaping
your answers. Chat, CETI sessions, long-form stories and ezines,
code, voice, and image — served by uncensored models you control,
shared node to node.

If you want your AI to live in your own building, this is the
project. Welcome to the network.

## Run it in 2 minutes

That's it — no installs to babysit, no accounts to create.

1. Install the newest Python from
   [python.org](https://www.python.org/downloads/).
2. Download `laptop-uncensored-node-0.0.1.zip` and unzip it.
3. Run `autorun.bat` (Windows) or `autorun.sh` (Linux). It checks
   the setup, downloads the models, starts the servers, and opens
   the web UI.
4. Open `http://localhost:8900` in your browser.

Primary testing is done on **Windows, with a 6 GB RTX card** (an
RTX 3060 laptop) — chat on VRAM, embeddings on CPU/RAM by default
so they don't steal VRAM. A model needs disk space and RAM/VRAM to
run; GPU memory figures in the Models tab are labelled estimates
(file size × 1.2), not measurements.

## What you get

- **Chat** — everyday talk with a local uncensored model, or a
  cloud model when you pick one. Your keys live in your browser
  only; the server never writes them to disk.
- **CETI sessions** — answers from the CETI ladder: facts, AIML,
  and local knowledge first, cloud only as a suggestion. CETI's
  design goal is zero tokens at serve time: the model is a
  background compiler, and serving is pure retrieval. Every
  answer shows its tier, its reasoning, and its signed ledger
  entry.
- **Node network (Gopher, not a platform)** — announce your node
  and it lists immediately with the capabilities it claims. No
  approval, no verified flag, no network sysop. Allow, deny, and
  mute live per node, and every answer is signed by the node's own
  key.
- **Lobby** — a public IRC-like room for listed nodes, plus
  private sysop chat and file transfers between nodes.
- **Research tab** — a local agent with a heartbeat. It compiles
  what CETI already knows, reads source pages as text, distills
  what it learns, and proposes it for your approval. Approvals are
  what move the ladder — and the benchmark score.
- **Models tab (the model hub)** — browse Hugging Face
  collections (default: `chwoo/uncensored-models`) and download
  GGUFs into `models/`. Drop your preferred model (`.gguf`) into
  `models/` yourself and it appears in the System tab's local
  model list — pick it there to load it.
- **SSH terminal** — a terminal in the workspace. It defaults to
  **chat.hf.co**, where you can use free models by pairing your
  Hugging Face account.
- **Memory** — a node view of vector memory, with provenance on
  every reply: which memories were used, and what drew on them.

## Models & cloud API

Local chat is the default and sends nothing anywhere. When you pick
a cloud provider in Chat, the key stays in your browser
(localStorage) and rides along with each request.

Providers, in list order:

- **Local — uncensored (private).** The GGUFs in `models/`,
  picked in System → Models & cloud API. The loaded one is marked.
- **OrcaRouter** — `https://api.orcarouter.ai/v1`
  (OpenAI-compatible). Keys look like `sk-orca-…`
  (`ORCAROUTER_API_KEY`). `orcarouter/auto` is seeded on signup
  and picks the cheapest live chat model; the model list is
  public, so the dropdown populates before you add a key.
- **OpenRouter** — `https://openrouter.ai/api/v1`. Free models
  list first.
- **Nvidia** — `https://integrate.api.nvidia.com/v1`.

Node endpoints (same OpenAI-compatible shape for all three):

- `GET /api/cloud/models?provider=orcarouter|openrouter|nvidia`
  (key in the `X-API-Key` header; without a key the provider's
  suggestion list is returned)
- `POST /api/cloud/test` — `{provider, api_key, model}` tests one
  model and reports back
- `POST /api/chat` — `{messages, provider, model, api_key?}`
  streams the reply

## Screenshots

Project logo:

![CETI: Uncensored Node logo](docs/images/logo.svg)

Chat, the default view:

![CETI: Uncensored Node — Chat](docs/images/chat.png)

Node tab, Lobby — the IRC room:

![CETI: Uncensored Node — Node lobby](docs/images/node-lobby.png)

System tab — local model picked, RAM available:

![CETI: Uncensored Node — System](docs/images/system.png)

## The ledger (regula core)

Every CETI answer, session save, and share is appended to the
node's own signed ledger (`regula_core: OK`, `block ledger: OK`
in the footer). Verification uses only the keys inside the
package — nothing is sent anywhere else to check it.

## A note on CETI

This release does not use CETI AIML technology yet.
CETI BIOS for general intelligence is still ahead.
CETI technology was used in developing this release, and the
node network is the platform the new CETI is built on. The Brain
pane shows CETI components as a read-only view; the packaged
release runs the node network, the ledger, and the model serving
described above.

## Hardware sponsors wanted

We are **actively looking for hardware sponsors**. If you can
donate GPUs, machines, or hosting to keep uncensored nodes
running, email **admin@darknet.ca** — tell us what you run and
what you want to serve, and we'll get you listed and set up.
Anyone who wants to join the project gets a **free darknet.ca
Gmail account** — same address to ask.

## Status

- **Version:** 0.0.1z. (Lettered builds
  mark exactly which 0.0.1 is in hand; z is this one.)
- **Tests:** 402 checks green at this build (unit 206: Hub 124,
  Brain 6, CETI 13, SSH terminal 29, Research 21, Sessions 5,
  Local models 8; script suites 196: nodeapi 101, community 16,
  image 13, node_client 12, text_handler 8, transfer 13,
  transfers/serving 14, tunnel 19).
- **New in 0.0.1z:** the Research tab (local agent, 60-second
  heartbeat, lynx-style text reads, local-first distill, human
  approval gate, benchmark that turns misses into research
  tasks); chat sessions you can archive or delete from the
  session list, ChatGPT-style; the System tab's Models & cloud
  API card finally shows the local model — the Model dropdown
  lists the GGUFs in `models/` and picking one loads it; a RAM
  AVAILABLE row in SYSTEM DETAIL (now readable on Windows too);
  and this README, rewritten with fresh screenshots and the
  pixel banner above.

- **Offline copy:** this README, with images baked in, ships at
  `info/ceti-readme-baked.html` — open it anywhere, no network
  needed.

Sovereign AI, from Canada, on your own iron. Welcome to the network.
