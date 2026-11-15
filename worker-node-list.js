/* node.darknet.ca list server (replaces/extends the CETI discovery worker).
 *
 * Gopher model: the list is open, the gates are per-node.
 * Keeps the current discovery routes working:
 *   GET  /         -> discovery banner (text, as today)
 *   GET  /nodes    -> listed nodes with their ANNOUNCED capabilities
 *                     (what each node offers)
 *   POST /gossip   -> echo (as today)
 * and adds the node-network routes:
 *   POST /announce -> signed announce -> listed immediately, with the
 *                     capabilities it claims to offer
 *   POST /upload   -> signed file offer -> listed offers
 *   GET  /uploads  -> offered files (each receiver decides its own
 *                     downloads; no central accept step)
 *   POST /mailbox/send  -> signed job to a node's mailbox. The server
 *                          checks signature + listed ONLY. Whether
 *                          the job RUNS is the receiving node's own
 *                          decision (its poller allow/deny/block).
 *   POST /mailbox/poll  -> signed poll: fetch + clear own mailbox
 *   POST /mailbox/reply -> signed reply to a requester's mailbox
 *   POST /lobby/send   -> signed public lobby post (listed nodes only)
 *   GET  /lobby         -> recent lobby messages (last 100)
 *   POST /chatbox/send  -> signed private sysop-chat message
 *                          (signature + listed check only; the
 *                          recipient decides who it answers)
 *   POST /chatbox/poll  -> fetch + clear own private chat messages
 * The mailbox is how firewalled nodes serve the network: they poll
 * outbound (no inbound ports), verify each job, run it locally, and
 * reply through the mailbox. Long results go via TC, not the mailbox
 * (KV values cap at 25MB).
 *
 * There is NO central sysop and NO grant system in this model:
 * allow/deny/mute live on each node (its poller peer policy), and a
 * whitelist is just a peer set to "allow" — persistent, no per-job
 * ask. The Worker needs NO key at deploy time.
 *
 * Storage: one KV namespace (NODE_LIST_KV). Nodes are sharded — one
 * key per node (`node:<pubkey>`, with a server `seen` timestamp) —
 * so an announce never rewrites the whole list (the old single
 * `nodes` key was a read-modify-write race: two announces at once
 * could lose one). Mailboxes and chatboxes are already per-node
 * (`mailbox:<pubkey>`, `chatbox:<pubkey>`). The lobby is one key
 * (last 100 messages) — a busy lobby can still lose a post to a
 * concurrent write; that is the known next shard if it bites.
 * Announcements are a few per day — far inside the KV free tier
 * (100k reads/day, 1k writes/day, 1GB). The Worker itself serves
 * only small JSON, inside the Workers free tier. No chunk bytes
 * ever pass through here; files move peer-to-peer (TC) afterwards.
 *
 * Presence (2026-11-07): a node that announces once used to stay
 * listed forever — with 50 people the list fills with dead nodes.
 * Each announce now stamps `seen` (server time); GET /nodes only
 * returns entries seen within NODE_TTL (15 min). Nodes heartbeat
 * by re-announcing every HEARTBEAT (5 min) — the poller does this
 * in run_forever — so a live node refreshes well before expiry
 * and a dead one drops off the list within 15 minutes.
 *
 * Rate limiting (2026-11-07): anyone listed can send unlimited
 * jobs, so one buggy client in a retry loop (or one bad actor)
 * could burn the shared KV write budget or flood mailboxes. Each
 * send now counts against its sender's quota: RATE_LIMIT writes
 * per RATE_WINDOW (600 per 10 min — a human sends a handful; a
 * tight loop hits the cap in minutes and gets 429). The counter
 * is keyed by the VERIFIED pubkey (checked after the signature,
 * so a forged request cannot burn someone else's quota), and the
 * counter write is best-effort: if KV fails, the request is
 * allowed (fail open — a KV hiccup must not take the network
 * down). The limit is approximate (concurrent requests can slip
 * a few past it); that is fine for a flood guard. Polls are NOT
 * rate-limited here: they are frequent and an empty poll writes
 * nothing, so a KV counter per poll would cost more writes than
 * it saves; a runaway poll loop burns reads, which are cheap on
 * the Paid plan, and Cloudflare's own rate limiting is the
 * backstop for that.
 *
 * Signatures: regula_core scheme (secp256k1 ECDSA over double-SHA256,
 * DER), verified here in pure BigInt JS — no WebCrypto curve dependency.
 *
 * DEPLOY (needs wrangler + the Cloudflare token):
 *   1. wrangler kv namespace create NODE_LIST_KV  (put the id in
 *      wrangler.toml below)
 *   2. Deploy under the EXISTING script name so the node.darknet.ca
 *      route keeps pointing at it, or update the zone route to the new
 *      script name. Do not deploy a second worker and leave the old
 *      discovery worker answering the domain.
 */

// --- secp256k1 (BigInt) ---------------------------------------------------
const P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2Fn;
const N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141n;
const Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798n;
const Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8n;

function mod(a, m) { const r = a % m; return r >= 0n ? r : r + m; }
function modInv(a, m) {
  let [old, cur] = [mod(a, m), m], [x0, x1] = [1n, 0n];
  while (cur !== 0n) { const q = old / cur; [old, cur] = [cur, old - q * cur]; [x0, x1] = [x1, x0 - q * x1]; }
  return mod(x0, m);
}
function pointAdd(p, q) {
  if (!p) return q; if (!q) return p;
  const [x1, y1] = p, [x2, y2] = q;
  if (x1 === x2 && y1 !== y2) return null;
  let lam;
  if (x1 === x2) lam = mod(3n * x1 * x1 * modInv(2n * y1, P), P);
  else lam = mod((y2 - y1) * modInv(x2 - x1, P), P);
  const x3 = mod(lam * lam - x1 - x2, P);
  return [x3, mod(lam * (x1 - x3) - y1, P)];
}
function pointMul(k, point) {
  let result = null, addend = point;
  while (k > 0n) {
    if (k & 1n) result = pointAdd(result, addend);
    addend = pointAdd(addend, addend);
    k >>= 1n;
  }
  return result;
}
function decompress(comp) {
  if (comp.length !== 33 || (comp[0] !== 2 && comp[0] !== 3)) throw new Error("bad pubkey");
  const xHex = [...comp.slice(1)].map((b) => b.toString(16).padStart(2, "0")).join("");
  const x = BigInt("0x" + xHex);
  if (x >= P) throw new Error("pubkey x out of range");
  const ySq = mod(x ** 3n + 7n, P);
  let y = modPow(ySq, (P + 1n) / 4n, P);
  if (mod(y * y - ySq, P) !== 0n) throw new Error("point not on curve");
  if ((y & 1n) !== BigInt(comp[0] & 1)) y = P - y;
  return [x, y];
}
function modPow(base, exp, m) {
  let result = 1n;
  base = mod(base, m);
  while (exp > 0n) { if (exp & 1n) result = mod(result * base, m); base = mod(base * base, m); exp >>= 1n; }
  return result;
}
function derDecode(hex) {
  const b = Uint8Array.from(hex.match(/../g).map((h) => parseInt(h, 16)));
  if (b.length < 2 || b[0] !== 0x30) throw new Error("bad DER");
  const total = b[1];
  if (b.length !== 2 + total) throw new Error("bad DER length");
  let o = 2;
  if (b[o] !== 0x02) throw new Error("bad DER int");
  const l1 = b[o + 1];
  const r = BigInt("0x" + [...b.slice(o + 2, o + 2 + l1)].map((x) => x.toString(16).padStart(2, "0")).join("") || "0");
  o += 2 + l1;
  if (b[o] !== 0x02) throw new Error("bad DER int");
  const l2 = b[o + 1];
  const s = BigInt("0x" + [...b.slice(o + 2, o + 2 + l2)].map((x) => x.toString(16).padStart(2, "0")).join("") || "0");
  return [r, s];
}
function sortKeys(v) {
  if (Array.isArray(v)) return v.map(sortKeys);
  if (v && typeof v === "object") {
    const out = {};
    for (const k of Object.keys(v).sort()) out[k] = sortKeys(v[k]);
    return out;
  }
  return v;
}
async function sha256d(bytes) {
  const once = await crypto.subtle.digest("SHA-256", bytes);
  return new Uint8Array(await crypto.subtle.digest("SHA-256", once));
}
async function verifySignature(pubkeyHex, payloadBytes, signatureHex) {
  try {
    const comp = Uint8Array.from(pubkeyHex.match(/../g).map((h) => parseInt(h, 16)));
    const point = decompress(comp);
    const [r, s] = derDecode(signatureHex);
    if (!(r >= 1n && r < N && s >= 1n && s < N)) return false;
    const digest = await sha256d(payloadBytes);
    const z = BigInt("0x" + [...digest].map((x) => x.toString(16).padStart(2, "0")).join(""));
    const w = modInv(s, N);
    const sum = pointAdd(pointMul(mod(z * w, N), [Gx, Gy]), pointMul(mod(r * w, N), point));
    if (!sum) return false;
    return mod(sum[0], N) === r;
  } catch { return false; }
}
async function verifySigned(payload) {
  const { signature, pubkey, ...rest } = payload;
  if (!signature || !pubkey) return false;
  const signedForm = { ...rest, pubkey };
  const bytes = new TextEncoder().encode(JSON.stringify(sortKeys(signedForm)));
  return verifySignature(pubkey, bytes, signature);
}

// --- KV helpers -------------------------------------------------------------
async function load(env, key, fallback) {
  const raw = await env.NODE_LIST_KV.get(key);
  return raw ? JSON.parse(raw) : fallback;
}
async function save(env, key, value) {
  await env.NODE_LIST_KV.put(key, JSON.stringify(value));
}

// Presence: a node entry is live for NODE_TTL after its last announce
// (the poller heartbeats every HEARTBEAT, well inside the TTL).
const NODE_TTL_MS = 15 * 60 * 1000;
const HEARTBEAT_MS = 5 * 60 * 1000;
// Rate limiting: sends per sender per window (see header).
const RATE_LIMIT = 600;
const RATE_WINDOW_MS = 10 * 60 * 1000;

function nodeKey(pubkey) { return "node:" + pubkey; }

async function loadNode(env, pubkey) {
  const entry = await load(env, nodeKey(pubkey), null);
  if (!entry) return null;
  // `seen` is stored as an ISO string (for clients); parse it back
  // to ms for the TTL check. A missing/unparseable stamp is treated
  // as expired — a node that cannot prove it announced recently is
  // not listed. (2026-11-07: the first version subtracted the ISO
  // string from Date.now(), which is NaN and never expires — fixed
  // to Date.parse before the first deploy.)
  const seenMs = Date.parse(entry.seen || "") || 0;
  if (Date.now() - seenMs > NODE_TTL_MS) return null;
  return entry;
}

async function listLiveNodes(env) {
  const listed = await env.NODE_LIST_KV.list({ prefix: "node:" });
  const live = [];
  for (const k of listed.keys) {
    const entry = await load(env, k.name, null);
    if (!entry) continue;
    const seenMs = Date.parse(entry.seen || "") || 0;
    if (Date.now() - seenMs <= NODE_TTL_MS) live.push(entry);
  }
  return live;
}

async function checkRate(env, pubkey) {
  // Returns true when the sender is inside its quota. Best-effort:
  // any KV failure allows the request (fail open).
  try {
    const window = Math.floor(Date.now() / RATE_WINDOW_MS);
    const key = "rl:" + pubkey + ":" + window;
    const raw = await env.NODE_LIST_KV.get(key);
    const count = raw ? parseInt(raw, 10) || 0 : 0;
    if (count >= RATE_LIMIT) return false;
    await env.NODE_LIST_KV.put(key, String(count + 1));
    return true;
  } catch { return true; }
}

// Public list entry for an announced node. Capabilities are what the
// node CLAIMS to offer (its announce payload) — whether anyone may
// use them is each receiving node's own decision, not a server grant.
// There is no verified flag in this model: recognition is per-node
// (allow-list / favorites), not global. `seen` is the server time of
// the last announce (ISO), so clients can show freshness.
function listEntry(ann, seenMs) {
  return {
    node_id: ann.node_id, fingerprint: ann.fingerprint, name: ann.name,
    pubkey: ann.pubkey, capabilities: ann.capabilities || [],
    endpoint: ann.endpoint || "",
    seen: new Date(seenMs).toISOString().replace(/\.\d+Z$/, "Z"),
  };
}

// --- HTTP -------------------------------------------------------------------
function json(data, status) {
  return new Response(JSON.stringify(data, null, 2), {
    status: status || 200,
    headers: { "content-type": "application/json" },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    if (request.method === "GET" && path === "/") {
      return new Response(
        "node.darknet.ca - node discovery. GET /nodes, POST /gossip, " +
        "POST /announce, POST /upload, GET /uploads, " +
        "POST /mailbox/send, POST /mailbox/poll, POST /mailbox/reply, " +
        "POST /lobby/send, GET /lobby, " +
        "POST /chatbox/send, POST /chatbox/poll",
        { headers: { "content-type": "text/plain" } });
    }
    if (request.method === "GET" && path === "/nodes") {
      return json({ format: "tc-node-list", version: 1, nodes: await listLiveNodes(env) });
    }
    if (request.method === "GET" && path === "/uploads") {
      return json({ uploads: await load(env, "uploads", []) });
    }
    if (request.method === "GET" && path === "/lobby") {
      return json({ messages: await load(env, "lobby", []) });
    }
    if (request.method !== "POST") return json({ error: "not found" }, 404);
    let payload;
    try { payload = await request.json(); } catch { return json({ error: "bad json" }, 400); }
    if (path === "/gossip") return json({ echo: payload });
    if (path === "/announce") {
      if (!(await verifySigned(payload))) return json({ error: "bad signature" }, 401);
      if (!(await checkRate(env, payload.pubkey))) return json({ error: "rate limited" }, 429);
      const pending = await load(env, "pending_announces", {});
      pending[payload.pubkey] = payload;
      await save(env, "pending_announces", pending);
      // Gopher model: a valid announce lists the node immediately,
      // with the capabilities it claims to offer. No approval, no
      // verified flag — recognition is per-node, not global.
      // Sharded (one key per node) + server `seen` stamp: the list
      // is presence, not a roster — entries older than NODE_TTL
      // drop off GET /nodes until the node heartbeats again.
      const seenMs = Date.now();
      await save(env, nodeKey(payload.pubkey), listEntry(payload, seenMs));
      return json({ status: "listed",
                    note: "listed; each node decides its own allow/deny/mute" });
    }
    if (path === "/upload") {
      if (!(await verifySigned(payload))) return json({ error: "bad signature" }, 401);
      if (!(await checkRate(env, payload.pubkey))) return json({ error: "rate limited" }, 429);
      const uploads = await load(env, "uploads", []);
      uploads.push(payload);
      await save(env, "uploads", uploads);
      return json({ status: "offered",
                    note: "offer listed; each receiver decides its own downloads" });
    }
    if (path === "/mailbox/send") {
      if (!(await verifySigned(payload))) return json({ error: "bad signature" }, 401);
      // Gopher model: signature + listed ONLY. Whether the job runs
      // is the receiving node's own decision (its poller policy).
      // "Listed" means a live node entry (seen within NODE_TTL).
      if (!(await loadNode(env, payload.pubkey))) return json({ error: "not a listed node" }, 403);
      if (!(await checkRate(env, payload.pubkey))) return json({ error: "rate limited" }, 429);
      const key = "mailbox:" + payload.to_pubkey;
      const queue = await load(env, key, []);
      queue.push(payload);
      await save(env, key, queue);
      return json({ status: "queued", id: payload.id });
    }
    if (path === "/mailbox/poll") {
      if (!(await verifySigned(payload))) return json({ error: "bad signature" }, 401);
      const key = "mailbox:" + payload.pubkey;
      const queue = await load(env, key, []);
      // KV writes are the scarce resource (free tier: 1000/day shared
      // across the whole network). An empty poll must NOT write: the
      // old code saved [] on every poll, so an idle 5s poller alone
      // burned 17,280 writes/day and tripped the limit on 2026-10-28.
      if (queue.length) await save(env, key, []);
      return json({ messages: queue });
    }
    if (path === "/mailbox/reply") {
      if (!(await verifySigned(payload))) return json({ error: "bad signature" }, 401);
      if (!(await checkRate(env, payload.pubkey))) return json({ error: "rate limited" }, 429);
      const key = "mailbox:" + payload.to_pubkey;
      const queue = await load(env, key, []);
      queue.push(payload);
      await save(env, key, queue);
      return json({ status: "queued" });
    }
    if (path === "/lobby/send") {
      if (!(await verifySigned(payload))) return json({ error: "bad signature" }, 401);
      const sender = await loadNode(env, payload.pubkey);
      if (!sender) return json({ error: "not a listed node" }, 403);
      if (!(await checkRate(env, payload.pubkey))) return json({ error: "rate limited" }, 429);
      const text = String(payload.text || "").trim();
      if (!text) return json({ error: "empty message" }, 400);
      const lobby = await load(env, "lobby", []);
      lobby.push({ pubkey: payload.pubkey, name: sender.name || "",
                   text: text.slice(0, 2000),
                   ts: new Date().toISOString().replace(/\.\d+Z$/, "Z") });
      await save(env, "lobby", lobby.slice(-100));
      return json({ status: "posted" });
    }
    if (path === "/chatbox/send") {
      if (!(await verifySigned(payload))) return json({ error: "bad signature" }, 401);
      const senders = await loadNode(env, payload.pubkey);
      if (!senders) return json({ error: "not a listed node" }, 403);
      if (!(await checkRate(env, payload.pubkey))) return json({ error: "rate limited" }, 429);
      const text = String(payload.text || "").trim();
      if (!text) return json({ error: "empty message" }, 400);
      const key = "chatbox:" + payload.to_pubkey;
      const queue = await load(env, key, []);
      queue.push({ from_pubkey: payload.pubkey,
                   from_name: senders.name || "",
                   text: text.slice(0, 2000),
                   ts: new Date().toISOString().replace(/\.\d+Z$/, "Z") });
      await save(env, key, queue);
      return json({ status: "queued" });
    }
    if (path === "/chatbox/poll") {
      if (!(await verifySigned(payload))) return json({ error: "bad signature" }, 401);
      const key = "chatbox:" + payload.pubkey;
      const queue = await load(env, key, []);
      // Same as /mailbox/poll: no write on an empty poll.
      if (queue.length) await save(env, key, []);
      return json({ messages: queue });
    }
    return json({ error: "not found" }, 404);
  },
};
