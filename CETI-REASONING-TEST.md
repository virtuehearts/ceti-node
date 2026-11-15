# CETI Reasoning Test — Full Ladder + Local Reasoning Prototype

Date: 2026-11-06 (UTC). Local-only test. No cloud API was called, no money
spent, nothing posted. Code: `~/workspace/uncensored-node/`, ladder
`hub/ceti_chat.py`, prototype `hub/reasoning.py` (new, alongside — `ceti_chat.py`
was NOT edited). Raw per-question JSON:
`~/workspace/uncensored-node/CETI-REASONING-TEST-results.json`.

## Test setup (verified live)

- Roots: `brain.resolve_roots()` → `/home/hatch/workspace/uncensored-node/brain`
  (cassettes in `brain/cassettes/`). Identity: mya-node,
  pubkey `0246bce0…738a1da1`, hash `cff8e94ea81768a22fd2caa43a53722d78f8c7187eabb6cbab68e01962d5ffaa`,
  fingerprint `NODE-CFF8-E94E-A817-68A2` (matches the live mya-node identity).
- Embedding server `:11435` (EmbeddingGemma-2, `~/.llama-app/llama serve … --embedding`):
  **down at start, restarted per the task, verified with a real POST to
  `/v1/embeddings` → 768 dims, HTTP 200.** Restarted again after the reboot below,
  re-verified 768 dims.
- Chat model: live LFM3 1.7B-Uncensored-AD Q4_K_M on `:11436` (llama-server,
  `-c 2048`). The ladder's `chat_complete()` defaults to `HUB_CHAT_PORT`
  (8080, nothing there), so the runner set `HUB_CHAT_PORT=11436` /
  `HUB_EMBED_PORT=11435` / `HUB_DATA_DIR=~/workspace/mya-node/data` — client
  env only; the servers themselves were not touched.
- **Sandbox reboot ~06:19 UTC** killed the first ladder run mid-Q8, wiped
  `/tmp` (runner + logs + results), and took down LFM3, Hub `:8900`, the
  poller and the bridge. The main context restarted the stack (LFM3 came
  back, pid churn 1974 → 2321 — it restarted twice). A resume run then hit
  a restart window: all five model calls failed in ~1 s with
  `Connection refused`. Those five rows were discarded and re-run once
  LFM3 health was `{"status":"ok"}`. Q1–Q7 below are the exact values from
  the pre-reboot run, preserved from the run log.
- **Contention:** a separate DERISKED test run on `:11437` was burning
  ~50–60% CPU on this 4-core box during the model calls, alongside the
  node poller hitting `:11436` every 60 s. LFM3 generated at **~1.5–4.6
  tok/s** (slot logs), not the ~11 tok/s seen on a quiet box. Timings below
  are measured under that contention and are honest, not best-case.

## Knowledge inventory (measured, not claimed)

| Store | Count | Status on the ladder |
|---|---|---|
| AIML cassettes | 31 files, 202 categories | read by AIML tier |
| `<fact>` elements in cassettes | 1,395 | read by facts tier (only source — see below) |
| `brain/facts.sqlite3` | **0 rows** | empty; the fact store contributes nothing |
| `brain/knowledge.sqlite3` | 158 QA + 13 aliases | **never read by `ceti_chat.answer()`** — the QA rows duplicate cassette templates, but a paraphrase of a QA question falls through to the model |
| `mya-node/data/memory.db` | **2 rows** (`User: capital of canada`, `Assistant: Canada — capital: Ottawa`, both source `ceti`) | the entire vector memory |

## Current ladder — question by question

Correctness was verified against the cassette facts, not against the model.
"Correct" means the answer contains the expected value.

| # | Question | Tier | Time | Answer (verbatim, trimmed) | Correct? |
|---|---|---|---|---|---|
| Q1 | What is the capital of Canada? | facts | 0.1 s | `Canada — capital: Ottawa` | ✅ |
| Q2 | What is the currency of Japan? | facts | 0.1 s | `Japan — capital: Tokyo` | ❌ WRONG — asked currency, answered capital |
| Q3 | What is the population of France? | facts | 0.0 s | `France — capital: Paris` | ❌ WRONG — asked population, answered capital |
| Q4 | When did FidoNet start operating? | aiml | 0.0 s | `1984` | ✅ |
| Q5 | What was introduction date of Commodore 64 | aiml | 0.1 s | `The Commodore 64 was introduced in January 1982 by Commodore International.` | ✅ |
| Q6 | What is most common speed of dial-up modems | aiml | 0.1 s | `56k was the fastest common dial-up modem speed…` | ✅ |
| Q7 | tell me about dial-up forums | local-model | **470.1 s** | Long generic essay (1,383 chars). See notes. | ⚠️ ungrounded — see notes |
| Q8 | what did sysops do on a BBS? | local-model | **683.7 s** | Generic but broadly right list (setup, accounts, monitoring, troubleshooting). One slip: expands BBS as "Basic Bulletin System" — it is Bulletin Board System. | ⚠️ partly right, ungrounded, one factual slip |
| Q9 | What is 17 * 23 + 41? | local-model | 115.8 s | Correct working: 17×23=391, +41=**432** | ✅ (took ~2 min for what Python does in 0.1 s) |
| Q10 | What is 15% of 240? | local-model | **968.9 s** | Correct working: 15×240=3600, /100=**36** | ✅ (took ~16 min; load avg 9.18 at the time) |
| Q11 | What is the capital of Tuvalu? | facts | 0.0 s | `Tuvalu — capital: Funafuti` | ✅ — Tuvalu IS in the facts; not a miss |
| Q12 | Who won the 2026 World Cup? | local-model | **1903.9 s** | `The 2026 FIFA World Cup has not been held yet — it's scheduled for June–July 2026… no winner has been crowned as of now (mid‑2025).` | ❌ WRONG — today is 2026-11-06; the June–July 2026 tournament is over. The model's knowledge is frozen at mid-2025 and it states that as current fact. 341 chars took 32 min (most of it `<think>` tokens, stripped from the answer). |
| Q13 | What is the capital of the country whose currency is the yen? | local-model | 174.7 s | `The currency known as the yen (JPY) is the official currency of Japan. The capital of Japan is Tokyo. So: Capital = Tokyo.` | ✅ — the model composed it correctly, by luck of its own weights; the ladder itself cannot compose |

**Ladder score: 8/13 fully correct.** Wrong: Q2, Q3 (facts tier returns the
wrong predicate with full confidence), Q12 (stale model knowledge
presented as current). Partial/ungrounded: Q7, Q8. Total time on the six
local-model calls: **4,317 s (~72 min)** — Q7 470 s, Q8 684 s, Q9 116 s,
Q10 969 s, Q12 1,904 s, Q13 175 s.

### Q7 notes (the only completed model answer)

- Vector tier returned **both** memory rows as context (`#1` score 0.613,
  `#2` score 0.591) — the only two rows in the DB, both about the capital
  of Canada, both above the ladder's 0.5 threshold, both irrelevant to
  dial-up forums. With a 2-row memory, *every* question retrieves the same
  two rows. The model was told to "use it if it helps, ignore it if not",
  so it ignored them — but the context slot is noise, not signal.
- The answer is plausible but ungrounded: "typically 28 kbps" (the
  cassette says 56k was the standard), "character-by-character posts"
  (BBS posts were not character-by-character), and it invents forum names
  at the end: "Dial-Up Talk", "The Slow Internet Forum". None of that
  comes from any local tier. 470 s for an answer no tier can vouch for.
- Q8 (683.7 s) is the same shape: a generic, broadly-right list of sysop
  duties, but it expands BBS as "Basic Bulletin System" and none of its
  specifics come from a cassette. The cassettes *do* contain sysop
  material (the `running-a-bbs-as-sysop.aiml` patterns), but only under
  exact-pattern questions — a paraphrase cannot reach it.
- Q9/Q10 prove the point about math: the model got both right (432, 36)
  with correct working — and took 115.8 s and 968.9 s doing it. Correct
  by luck of the draw, not by construction: nothing in the ladder checks
  the arithmetic, and a wrong digit would have shipped with the same
  confidence. Q10's 16 minutes happened while load average was 9.18 on
  4 cores (DERISKED `:11437` test run + LFM3 + poller).

## What failed in the current ladder (honest list)

1. **Facts tier ignores the predicate.** `_facts_tier` returns the first
   fact whose *subject* words appear in the question. `countries.aiml`
   lists `capital` first for every country, so *any* question naming a
   country returns its capital — Q2 and Q3 are wrong answers delivered
   with full confidence and a "FACTS — hit" trace. The correct facts were
   in `facts_search()` all along (8 matches per country); the tier just
   picks the wrong one. This is the single most damaging bug found: the
   ladder's most trusted tier is wrong 2 times out of 3 on
   non-capital fact questions.
2. **No math tier.** Arithmetic goes to the 1.7B model (Q9/Q10 pending).
   A 1.7B CPU model is the worst possible calculator: slow, and wrong
   without warning.
3. **No composition.** Q13 ("capital of the country whose currency is
   the yen") cannot match any single subject, so it falls through to the
   model. The two facts it needs (yen → Japan, Japan → Tokyo) are both
   in the cassettes.
4. **Vector memory is effectively empty and unfiltered.** 2 rows, both
   retrieved for every question. The 0.5 threshold is meaningless at this
   scale — cosine between unrelated short texts still clears it.
5. **`knowledge.sqlite3` (158 QA) is dead weight on this path.** The
   ladder never opens it. A paraphrase of a known QA ("when did the first
   BBS launch?" vs the AIML pattern "WHAT WAS FIRST BULLETIN BOARD SYSTEM
   BBS CREATED BY WARD CHRISTENSEN AND RANDY SUESS IN CHICAGO") falls
   through to the model even though the answer is on disk.
6. **Local model is very slow under real conditions:** 470 s for Q7 at
   1.5–4.6 tok/s with a second llama-server and the poller sharing 4
   cores; Q12 took 1,904 s for a 341-character answer. Any design that
   calls the model per question must budget for minutes, not seconds — or
   keep the model off the path for questions lower tiers can answer.
7. **Stale knowledge, stated as current fact (Q12).** The local model
   believes it is mid-2025 and says the World Cup "has not been held
   yet". Nothing in the ladder knows the model's cutoff or the date, so
   the stale answer ships as confidently as a fresh one. A local brain
   needs a "knowledge current as of" stamp on the model, and current
   events must route to cloud/research, not to frozen weights.
8. **facts.sqlite3 is empty (0 rows).** The "fact store" tier is
   cassette-only. Anything learned through the research pipeline that
   lands in the store is currently nothing.

## "Just the cloud" — baseline comparison (CODE-READ, not a live call)

No cloud API was called in this test. What follows is read from
`hub/cloud.py` and `hub/web.py` (`_chat`, `_cloud_test`), so it describes
what the cloud path *would* do.

- **What the cloud gets:** in `_chat`, the messages the UI sends (the
  conversation history) go to `stream_cloud(provider, api_key, model,
  gen_messages)`. The vector-memory context block is added **only when
  `provider == "local"`** — the cloud does *not* receive local memory
  text. Telemetry/memory traces are stripped from messages before any
  model call, so they never reach a provider. `_cloud_test` sends only
  `"Say ok."`.
- **What leaves the machine:** the user's question + chat history, to
  `api.orcarouter.ai`, `openrouter.ai`, or `integrate.api.nvidia.com`,
  with the API key as a Bearer header. The key itself is supplied per
  request (browser localStorage or `*_API_KEY` env) and is **never
  written to disk by `cloud.py`**.
- **What it costs:** per-provider token pricing (OpenRouter/OrcaRouter
  metered; Nvidia via key). The CETI ladder itself costs $0 — it never
  calls cloud, it only *suggests* it.
- **What comes back:** the cloud provider's answer, which is then
  **saved into local vector memory** (`source="web-chat"`,
  `model=<cloud model>`) when `use_memory` is on — so cloud answers do
  contaminate the local memory the ladder later treats as "local
  knowledge". That is a provenance gap worth closing (tag cloud-sourced
  memories as such; the `model` column exists for exactly this).
- **What the cloud would do differently on these 13 questions:** Q2/Q3
  (currency/population) — right, from its own weights. Q9/Q10 (math) —
  probably right. Q12 (World Cup winner) — right, from training/current
  data CETI does not have. Q7/Q8 (BBS lore) — fluent, plausibly right,
  but with the same ungrounded specifics the local model produced, and
  with no citation to CETI's own cassettes. The cloud is a better
  guesser; it is not a better *knower* of what CETI knows. The honest
  split: cloud for what is not in the house, local tiers for what is —
  and the local tiers must stop answering wrongly first (finding 1).

## The reasoning prototype — `hub/reasoning.py`

New file, stdlib-only, alongside `ceti_chat.py` (which was not edited).
Single entry: `answer_with_reasoning(query, roots, ident_hash)` →
`{tier, answer, evidence, reasoning_trace, memories, cloud_suggested,
numbers_grounded}`. It reuses the ladder's own helpers
(`ceti_chat._aiml_tier`, `facts_search`, `_db_facts`, `_iter_cassettes`)
so the two cannot drift apart on parsing.

### Tiers (in order — what each contributes)

```
query
  │ Tier 0 MATH — safe AST parser (+ - * / % ( ), percent-of, word ops).
  │   Exact numbers, calculation shown. No eval() of user text: only the
  │   extracted numeric expression passes an AST whitelist (no names,
  │   no calls, no attributes, no power). "What is 2026?" and "100/0"
  │   correctly fall through; "ignore 2+2; rm -rf /" evaluates only "2+2".
  ▼
  │ Tier 1 FACTS — all subject matches, PREDICATE-aware, + COMPOSITION.
  │   "currency of Japan" now picks the currency fact, not the capital
  │   fact. Composition: "capital of the country whose currency is the
  │   yen" → hop 1 (yen is Japan's currency) → hop 2 (Japan's capital).
  │   If the subject matches but the asked predicate has no fact, it
  │   REFUSES the first unrelated fact instead of answering wrongly.
  ▼
  │ Tier 2 AIML — cassette pattern match, reused from ceti_chat.
  ▼
  │ Tier 3 LEXICON — synonym/definition expansion BEFORE vector search.
  │   "dial-up forums" → also searches "bulletin board system, modem…";
  │   "sysops" → "system operator". Built-in seed from cassette
  │   vocabulary + a loader for a real WordNet-style file (below).
  ▼
  │ Tier 4 VECTOR — embed the EXPANDED query, search vector memory
  │   (threshold 0.35, top 5, scores reported). Still near-empty today
  │   (2 rows) — reported honestly, not hidden.
  ▼
  │ Tier 5 MODEL — local model ONLY, with the assembled context
  │   (lexicon definitions + vector hits) and a strict prompt:
  │   "Answer ONLY using the LOCAL CONTEXT… If the context does not
  │   contain the answer, reply exactly: I don't know from local
  │   knowledge." Plus a NUMBERS GUARD: every number in the answer must
  │   appear in the context or the question, else it is flagged
  │   (numbers_grounded=False, offending numbers listed). If there is
  │   NO context at all, the model is NOT called — calling it on nothing
  │   is asking it to hallucinate.
  ▼
  │ Tier 6 CLOUD — suggested only, never called.
```

### Prototype results (same 13 questions)

Full self-test run live twice (`python3 -m hub.reasoning`, log
`~/workspace/reasoning_selftest2.log`): **AUTO-CHECKABLE 10/10 correct**;
the 3 open/model questions (Q7, Q8, Q12) are judged by hand below.
Deterministic tiers run in 0.0–0.1 s each; the three model calls took
64.3 + 25.4 + 19.0 = 108.7 s total (contention had eased by the second
run, and temp 0.0 + short grounded answers generate far fewer tokens
than the ladder's temp 0.7 open-ended calls — do not read this as a
clean speed comparison, but the direction is real: grounded prompts are
shorter prompts).

| # | Question | Tier | Time | Answer | Verdict |
|---|---|---|---|---|---|
| Q1 | capital of Canada | facts | 0.0 s | `Canada — capital: Ottawa` | ✅ |
| Q2 | currency of Japan | facts | 0.0 s | `Japan — currency: Japanese yen` | ✅ (ladder: ❌) |
| Q3 | population of France | facts | 0.0 s | `France — population: 69081996` | ✅ (ladder: ❌) |
| Q4 | FidoNet start | aiml | 0.0 s | `1984` | ✅ |
| Q5 | Commodore 64 intro | aiml | 0.0 s | `…introduced in January 1982…` | ✅ |
| Q6 | dial-up modem speed | aiml | 0.0 s | `56k was the fastest common…` | ✅ |
| Q7 | dial-up forums | local-model | 64.3 s | Grounded in lexicon definitions: "online message boards… commonly called BBSs… post messages… over an ordinary phone line with a modem. The fastest common dial-up speed was 56k, and the connection made a distinctive chirping sound." Numbers guard passed. | ✅ grounded (one paraphrase slip: "bulletin board services" for "system"; everything else traces to the seed/cassettes) |
| Q8 | sysops on a BBS | local-model | 25.4 s | "A sysop (system operator) is the person who ran a BBS — its owner, moderator and keeper of the rules." Numbers guard passed. | ✅ correct but narrow — answers what a sysop *is*, not the task list the question asked for; the lexicon definition has no duties in it |
| Q9 | 17 * 23 + 41 | math | 0.0 s | `432` | ✅ |
| Q10 | 15% of 240 | math | 0.0 s | `36` | ✅ |
| Q11 | capital of Tuvalu | facts | 0.1 s | `Tuvalu — capital: Funafuti` | ✅ |
| Q12 | 2026 World Cup winner | local-model | 19.0 s | `I don't know from local knowledge.` | ✅ honest — the strict prompt held; no hallucinated winner (ladder: ❌ stale-wrong after 1,904 s) |
| Q13 | capital of country whose currency is yen | facts-composition | 0.0 s | `Japan — capital: Tokyo` (hop 1: Japan currency Japanese yen; hop 2: Japan capital Tokyo) | ✅ (ladder: model, 174.7 s) |

**Prototype score: 10/10 auto-checkable + 3/3 model answers honest and
grounded (Q8 narrow, noted).** Side-by-side: the ladder is 8/13 with 2
confidently-wrong fact answers; the prototype fixes both, answers math
and composition deterministically, and turns the one true miss into an
honest "I don't know" instead of a 32-minute stale hallucination.

Two prototype flaws found by the test itself, both recorded honestly:

- **Trace duplication bug (found and fixed during this test).** The
  first self-test run printed every reasoning-trace line 3×: `_vector_tier`
  and `_model_tier` both appended to the caller's trace list *and*
  returned it, and the caller then extended the list with itself.
  Fixed by returning fresh line-lists from both functions; the second
  run's traces are clean (each step once). The answers were unaffected.
- **The "empty context" guard never fires while vector memory is tiny.**
  Q12 reached the model only because the 2 irrelevant Canada rows
  cleared the 0.35 vector threshold and counted as "context". The model
  still answered honestly, but the guard as designed only triggers when
  vector memory returns *nothing*. Fix before wiring in: require vector
  hits to share a content word with the question (or a higher threshold)
  before they count as context — otherwise, at 2 rows, the guard is
  decoration, exactly like the ladder's 0.5 threshold.

Loader stubs verified: missing path → `{}`; JSON, TSV and Moby
`mthesaur.txt` formats all parse; numbers guard passes when numbers are
in context and flags `28`, `1994` when they are not. Lexicon expansion
verified live on Q7/Q8 (plural stemming: "forums"→"forum",
"sysops"→"sysop").

## Dictionary / thesaurus recommendation

Judged on license, offline size, stdlib-friendly format, synonym +
definition + hypernym coverage, Python availability. No large files were
downloaded in this run.

| Resource | License | Size (offline) | Format | Synonyms | Definitions | Hypernyms | Python | Verdict |
|---|---|---|---|---|---|---|---|---|
| **Open English WordNet 2025** (en-word.net, `globalwordnet/english-wordnet`) | **CC-BY 4.0** | WNDB release, ~12–17 MB compressed (Princeton 3.1 `wn3.1.dict.tar.gz` is 16 MB; OEWN 2025 is the same WNDB layout) | `index.<pos>` + `data.<pos>` plain text; also LMF XML / JSON / RDF / SQLite | ✅ synsets | ✅ glosses | ✅ | `wn` (`pip install wn`, `wn.download("oewn:2025+")`) or the stdlib WNDB reader in `reasoning.load_lexicon()` | **PRIMARY** |
| Princeton WordNet 3.1 | BSD-like (custom) | ~12 MB compressed, 155,327 words / 175,979 synsets | WNDB, same as above | ✅ | ✅ | ✅ | NLTK `wordnet` corpus | Superseded by OEWN (same data, cleaner license, still maintained; Princeton has no new releases planned) |
| Moby Thesaurus II (Grady Ward, Project Gutenberg #3202) | **Public domain** | `mthesaur.txt` ~26 MB extracted (12 MB compressed); 30,260 root words, 2,520,264 synonyms (~83 per root) | One CSV line per root word: root, synonym, synonym… | ✅ (widest) | ❌ none | ❌ | trivial stdlib parse (in `load_lexicon()`) | **FALLBACK** — synonym breadth WordNet lacks |
| GCIDE 0.54 (GNU, ftp.gnu.org) | Public domain core (Webster 1913) + GPLv3 packaging | 14–19 MB compressed; ~130k headwords | Marked-up ASCII text files | ❌ (not a thesaurus) | ✅ (full definitions) | ❌ | needs a parser | Definitions fallback only; 1913 core is dated |
| OpenThesaurus | LGPL / CC-BY-SA | German-only in practice (English export is not its focus) | `.txt`, one synset per line | ✅ | ❌ | ❌ | easy parse | Not for English CETI |
| Wiktionary (en, kaikki.org / dumps.wikimedia.org) | CC-BY-SA + GFDL | **1.55 GB** compressed XML; kaikki raw JSONL ~20 GB; needs 40–50 GB RAM to load whole | XML / JSONL | ✅ | ✅ | ✅ | `wiktextract` (pip) | Too big for a 6 GB laptop node; reject for CETI |

**Recommendation — PRIMARY: Open English WordNet 2025, WNDB format.**
File: `english-wordnet-2025.zip` from
`https://en-word.net/static/english-wordnet-2025.zip` (WNDB download;
the same release also ships LMF/XML, JSON and RDF). Ship the unzipped
`dict/` directory (`index.noun`, `data.noun`, `index.verb`, `data.verb`,
…) under the CETI data dir (e.g. `brain/lexicon/wordnet/`) and point
`reasoning.load_lexicon()` at it — it already parses WNDB, lazily by
lemma when a `want` set is passed, so a query-time load stays small.
License CC-BY 4.0: attribution to Open English WordNet in the README is
the only obligation, and it is redistribution-clean (unlike Princeton's
custom BSD-like licence). ~120k synsets / ~153k entries, with
definitions, synonyms, hypernyms, meronyms and antonyms — the one
resource that covers all three things the lexicon tier needs.

**Recommendation — FALLBACK: Moby Thesaurus II.**
File: `mthesaur.txt` inside the Project Gutenberg #3202 distribution
(public domain, no attribution requirement). One line per root word,
comma-separated synonyms — the `load_lexicon()` Moby branch already
reads it. Use it when WordNet has no entry for a word (slang, BBS-era
jargon, compounds): it has 2.5M synonyms where WordNet has none for that
headword. It gives no definitions, so it feeds query expansion only,
never the definitions slot. GCIDE stays on the shelf as a
definitions-only fallback for words neither resource defines.

**How it plugs in:** `answer_with_reasoning(query, roots, ident_hash,
lexicon=load_lexicon("brain/lexicon/wordnet"))` — file entries override
the built-in seed; `expand_query()` then expands with real synonyms
before the vector search, and the synset glosses become the
`LEXICON DEFINITIONS` context for Tier 5. No new dependency: the WNDB
reader is stdlib. (Do not ship the `wn` pip package or NLTK into the
node — the point of CETI is zero-dependency local operation; the WNDB
files plus ~60 lines of stdlib are the whole integration.)

## Recommended unified-memory architecture (for CETI chat, reasoning on)

```
User question
  │
  ├─ Tier 0  PYTHON MATH (deterministic) ............ exact, shown working
  ├─ Tier 1  FACTS (predicate-aware + 2-hop composition)
  │           + knowledge.sqlite3 QA as a second fact source
  │           (currently unread — wire it in, same subject/predicate shape)
  ├─ Tier 2  AIML cassettes (exact + * patterns)
  ├─ Tier 3  LEXICON (Open English WordNet WNDB, Moby fallback)
  │           → expands the query, supplies definitions
  ├─ Tier 4  VECTOR MEMORY (expanded query, scores shown, threshold
  │           that means something once the store has >100 rows)
  ├─ Tier 5  LOCAL MODEL (only with assembled context; strict
  │           only-from-context prompt; numbers guard; never called
  │           on an empty context)
  └─ Tier 6  CLOUD — suggested only, never called by CETI.
              The UI may offer it; the answer, if the user takes it,
              should be tagged cloud-sourced in vector memory.
```

Design rules that fall out of the test:

1. **Deterministic tiers first, model last, model never on empty context.**
   The model is the slowest and least trustworthy tier; it answers only
   what the lower tiers assembled.
2. **Every tier shows its evidence.** A tier that cannot show evidence
   does not get to answer (this is what catches finding 1).
3. **Numbers are guilty until proven grounded.** Any number in a model
   answer must appear in the context or the question.
4. **Vector memory needs volume before it needs tuning.** At 2 rows the
   threshold is decoration. Seed it from CETI session answers (already
   built: `record_answer_memory`) and from the 158 QA rows — then
   re-measure the threshold.
5. **Cloud is a suggestion, and its answers are imports, not memories.**
   Tag them by source/model so the ladder never presents a cloud guess
   as local knowledge.

## Prototype self-test counts

Run live twice (`python3 -m hub.reasoning`, second run after the trace
fix; log `~/workspace/reasoning_selftest2.log`):

- **AUTO-CHECKABLE: 10/10 correct** (Q1, Q2, Q3, Q4, Q5, Q6, Q9, Q10,
  Q11, Q13). Both runs agree; Q7's model answer was byte-identical
  across runs (temperature 0.0).
- **Model/open questions, hand-judged: 3/3 honest** — Q7 grounded and
  correct (one paraphrase slip, "bulletin board services"), Q8 correct
  but narrow, Q12 an honest "I don't know from local knowledge."
- **Current ladder, same 13 questions: 8/13 fully correct**, 2
  confidently wrong (Q2, Q3), 1 stale-wrong (Q12), 2 ungrounded-partial
  (Q7, Q8). Ladder model time 4,317 s vs prototype model time 108.7 s
  (3 calls; contention had eased — see the caveat in the prototype
  section, but the deterministic tiers alone decide 10 of 13 questions
  in under a second total).

## Wired in 2026-11-06

The prototype is now the live CETI chat ladder. No cloud calls, no
money spent, nothing posted, no zip packaged. `GOAL.md` untouched.

### What changed, file by file

- `hub/reasoning.py` — the reasoning layer itself:
  - **Empty-context guard (fix-before-wiring, done).** After the
    vector tier, hits are filtered to those sharing a content word
    (lowercased, len > 2, minus a small stopword list) with the
    question or its lexicon synonyms. Lexicon definitions count as
    real context on their own. If there are no definitions AND no
    relevant hits, the model is skipped entirely and the answer is
    cloud-suggested, with trace lines saying the model was skipped
    for lack of grounded context. Verified live on Q12: the 2
    irrelevant Canada rows (scores 0.64/0.62) are dropped and the
    model is not called.
  - **Cached lazy lexicon loader** `get_lexicon(roots, want=None)`:
    looks for `<ceti_dir>/lexicon/wordnet/` (or its `dict/` subdir),
    parses it with `load_lexicon()` limited to the query's words
    (plus singular forms, so "forums" finds the "forum" lemma),
    caches per (directory, want-set), returns `{}` when no lexicon
    dir exists. `answer_with_reasoning(lexicon=None)` now loads it
    by default; pass `{}` explicitly for seed-only behaviour.
  - **Definition merge priority (found during wiring).** The
    built-in seed's definition now wins when it has one; the loaded
    file fills definitions the seed lacks and always adds its
    synonyms. Reason: a general dictionary gloss ("forum" = "a
    public meeting...") is too generic for the strict
    only-from-context prompt — with file-wins, Q7 regressed to an
    honest "I don't know". With seed-wins, Q7 answers grounded
    again. Full self-test re-run after the change: 10/10.
  - **Wording fix:** the final cloud-suggested answer now says "no
    lexicon definition that answers it" instead of "no lexicon
    definition" (definitions can exist — e.g. for "world"/"cup" —
    without answering the question).
  - Module docstring updated: no longer "prototype".
- `hub/ceti_chat.py` — `answer()` now delegates to
  `reasoning.answer_with_reasoning()` and adapts the result to the
  existing contract (`thinking` = reasoning trace joined with
  newlines, plus `tier`/`answer`/`evidence`/`cloud_suggested`/
  `memories`, and new `numbers_grounded`). The old helper functions
  (`_facts_tier`, `_aiml_tier`, `_memory_hits`, `_local_model`,
  `facts_search`, `record_miss`, `read_suggestions`) are kept for
  backwards compatibility; the old ladder body in `answer()` (whose
  facts tier ignored the predicate) is gone. Docstring rewritten
  with the new tier order (math, facts, aiml, lexicon, vector,
  local-model, cloud-suggested).
- `hub/ceti_chat.py` — `record_answer_memory()` now also records
  tiers `"math"` and `"facts-composition"`. Cloud-suggested is
  still never recorded.
- `tests/test_ceti_chat.py` — rewritten for the new architecture:
  monkeypatch hooks moved from `ceti_chat._memory_hits` /
  `ceti_chat._local_model` to `reasoning._vector_tier` /
  `reasoning._model_tier` (plus a `servers.chat_complete` recording
  mock for the guard test). New tests: math tier, predicate-aware
  facts, two-hop composition, miss trace listing every layer
  (MATH/FACTS/AIML/LEXICON/VECTOR MEMORY/LOCAL MODEL/CLOUD +
  "never called"), empty-context guard (model function must not be
  called), `record_answer_memory` accepts math/facts-composition
  and still rejects cloud-suggested, lexicon-missing-dir is
  seed-only. Fixture gained Japan/France facts for the
  predicate/composition tests. **Test bug found and fixed during
  wiring:** the originals were stored as class attributes and read
  back via `self.orig_vector` — a plain function stored on a class
  binds as a method on instance access, so the restored tier got
  the test instance as a phantom first argument ("takes 2
  positional arguments but 3 were given"). `_restore_reasoning`
  now reads them through `type(self)`.
- `brain/lexicon/wordnet/` — **NEW, real data. Open English WordNet
  2025 WNDB downloaded once** from
  `https://en-word.net/static/english-wordnet-2025.zip`
  (**9.2 MB zip**, 14 files, **31 MB unzipped**: `index.noun`,
  `data.noun`, `index.verb`, `data.verb`, `index.adj`, `data.adj`,
  `index.adv`, `data.adv`, `index.sense`, `*.exc`). Parse verified
  with `load_lexicon()`: real synonyms + glosses for "computer"
  (computing machine, data processor...), "forum" (assembly,
  meeting place), "world", "cup". `expand_query()` gains them.
  No retry loop was needed; no substitute dictionary was used.
  Note: the shipped tree is NOT packaged into any laptop zip by
  this task — that is a release decision for Jero.

### Tests (all local, no servers/model/network except the self-test)

- `python -m unittest tests.test_ceti_chat -v`: **19/19 pass**.
- `tests.test_brain` + `tests.test_research` +
  `tests.test_memory_vector`: **42/42 pass** (61 total with the
  CETI suite).
- `python -m hub.reasoning` (live, `HUB_CHAT_PORT=11436`
  `HUB_EMBED_PORT=11435`): **AUTO-CHECKABLE 10/10**; model
  answers Q7 grounded (one slip: expands BBS as "bulk-broadcast
  servers"), Q8 correct but narrow (what a sysop is, not the task
  list), Q12 honest "I don't know from local knowledge."
  First wired run was run WITHOUT `HUB_CHAT_PORT` set and its 3
  model calls all failed with connection refused (port 8080) — see
  the deployment gap below; the run was repeated with the ports
  set and the numbers above are from the clean repeat.

### Live endpoint (POST /api/ceti/chat, record=false)

Hub restarted only (PID 11821, `:8900` 200). LFM3 `:11436`,
embed `:11435`, bridge `:8000`, poller and Ollama untouched and
verified still up afterwards. Vector memory still 2 rows (the
`record=false` checks did not contaminate it).

| # | Question | Tier | Answer |
|---|---|---|---|
| a | What is 17 * 23 + 41? | math | 432 |
| b | What is the currency of Japan? | facts | Japan — currency: Japanese yen |
| c | What is the capital of the country whose currency is the yen? | facts-composition | Japan — capital: Tokyo (hop 1: Japan currency Japanese yen; hop 2: Japan capital Tokyo) |
| d | When did FidoNet start operating? | aiml | 1984 |
| e | What is the capital of Canada? | facts | Canada — capital: Ottawa |
| + | tell me about dial-up forums | local-model | grounded in lexicon definitions (one slip: "bulk-broadcast servers" for BBS) |

All six ledgered (`ledger.ok: true`). Two corrections to the
original check list, both honest: the task's question (d) "what is
fidonet" has NO exact AIML pattern in the real cassettes (patterns
are "WHEN DID FIDONET START OPERATING", "WHAT IS CURRENT STATE OF
FIDONET", ...), so it was checked as (d) above and "what is
fidonet" was checked separately — it now answers via local-model,
grounded in the lexicon fidonet definition.

### Deployment gap found and fixed (pre-existing, not introduced here)

`hub/config.py: chat_port()` defaults to **8080** and nothing
listens on 8080. No start script anywhere sets `HUB_CHAT_PORT`, so
the Hub as started had no route to LFM3 `:11436` — the local-model
tier was unreachable from the live endpoint (the old ladder had
the same dead path; the first live check failed exactly this way:
"LOCAL MODEL — unavailable: Connection refused"). The Hub is now
running with `HUB_CHAT_PORT=11436 HUB_EMBED_PORT=11435` in its
environment. **This is not persisted anywhere**: the next manual
Hub restart must include those two variables (or `config.py`
should gain a smarter default) or the model tier goes dead again.
Also noted while restarting: `nohup ... &` reports a PID one off
from the actual Hub python process — kill by `pgrep -f "python3
-c from hub.web"` and confirm the port is 000 before restarting,
or the new process dies with "Address already in use" (this
happened once during this wiring and was caught by the log check).

## Re-run 2026-11-06 ~22:43 EST — AIML context retrieval (post-0.0.1e, unreleased)

Jero asked why "Tell me about fidonet" did not answer from the AIML
stores: the exact-match tier has no "TELL ME ABOUT FIDONET" pattern,
so the question fell to the local model with no AIML content at all.
Fix: `_aiml_search` in `hub/reasoning.py` (helper `_iter_categories`
in `hub/ceti_chat.py`) scores every cassette category by content-word
overlap with the ORIGINAL question words and feeds the top 4
templates to the model as an "AIML CONTEXT" section. Pure Python set
overlap — no embedding call; measured 6–11ms live on the real
cassettes (budget 250ms). The empty-context guard counts AIML
templates as grounded context.

Two regressions on the way, both caught by the self-test's new Q14
("Tell me about fidonet", expected "1984"):
1. First cut weighted lexicon synonyms at half weight; 'tell' ->
'state' let "CURRENT STATE OF ..." patterns score on the synonym and
crowd the atomic facts out. 10/11.
2. Second cut still failed: the trace prints only 3 synonyms per word
(`v[:3]`), hiding that 'about' also expands to 'around', which
matched narrative "around 2014" inside long telnet templates.
Retrieval now scores on original words only; synonyms still feed the
vector tier. Q14 then passed.

**Full self-test re-run: AUTO-CHECKABLE 11/11 correct** (Q1–Q6, Q9–
Q11, Q13, Q14; Q7/Q8/Q12 human-review, all honest). Q14 answer:
"FidoNet ... began operating in 1984; at its peak there were over
40,000 nodes and more than 120,000 points" — all three facts from
the AIML stores. Unit tests 232/232 (5 new: retrieval hit/miss,
fidonet top-4, real-cassette <250ms, context reaches the model) +
script suites 196/196. Live Hub verified after restart (pid 21604):
same fidonet answer via `POST /api/ceti/chat`, yen, 432, voice MP3
28,845 B, embed 768 dims. Not packed — the box Hub runs 0.0.1e +
this retrieval; the next letter pack carries it.
