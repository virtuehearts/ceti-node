"""Tests for the merged Node tab backend (hub/nodeapi.py).

One identity is the whole point of the merge: hub/nodeapi.py reads the
same data/identity.dat the Hub uses, so the HUB- fingerprint and the
NODE- fingerprint must derive from one pubkey. Everything else runs
against the in-memory mock list server — no network, no real list.
"""

import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="nodeapi-test-")
os.environ["HUB_DATA_DIR"] = TMP

import mock_list  # noqa: E402
import node as node_mod  # noqa: E402
from hub import nodeapi  # noqa: E402
from hub import servers  # noqa: E402
from hub.identity import ensure_identity  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def main():
    store = mock_list.Store()
    server, port = mock_list.serve(store)
    url = "http://127.0.0.1:%d" % port
    nodeapi.DEFAULT_LIST_URL = url
    try:
        # 1. One identity, two fingerprints, one secret file.
        st = nodeapi.status()
        check("nodeapi: status has node id + both fingerprints",
              st["node_id"].startswith("node-")
              and st["fingerprint"].startswith("NODE-")
              and st["hub_fingerprint"].startswith("HUB-"), str(st))
        ident, _ = ensure_identity(TMP)
        check("nodeapi: Hub identity reads the same secret file",
              ident.public_hex() == st["pubkey"], ident.public_hex())
        check("nodeapi: identity.dat is exactly 32 bytes",
              os.path.getsize(os.path.join(TMP, "identity.dat")) == 32)

        # 2. Announce needs a name; with a name it lists.
        check("nodeapi: announce without a name errors",
              "error" in nodeapi.announce())
        nodeapi.set_name("mergetest")
        check("nodeapi: name round-trips",
              nodeapi.get_name() == "mergetest")
        res = nodeapi.announce()
        check("nodeapi: announce lists the node",
              res.get("ok") is True, str(res))
        peers = nodeapi.peers()
        check("nodeapi: peers shows this node",
              any(n.get("node_id") == st["node_id"]
                  for n in peers.get("nodes", [])), str(peers))

        # 3. Lobby round-trip (listed nodes only).
        res = nodeapi.lobby_send("hello lobby")
        check("nodeapi: lobby send ok", res.get("ok") is True, str(res))
        lob = nodeapi.lobby()
        check("nodeapi: lobby lists the message",
              any(m.get("text") == "hello lobby"
                  for m in lob.get("messages", [])), str(lob))
        # 3b. Lobby anti-spoof + name gate (2026-10-29 Node redesign):
        # enriched messages carry the ANNOUNCED name + node_id (from
        # /nodes, not from the message), and an unnamed node cannot
        # send at all — the UI disables the input, this is the
        # server-side backstop.
        check("nodeapi: lobby message carries announced name + node_id",
              any(m.get("text") == "hello lobby"
                  and m.get("name") == "mergetest"
                  and m.get("node_id") == st["node_id"]
                  for m in lob.get("messages", [])), str(lob))
        os.remove(os.path.join(TMP, "node_name.txt"))
        res = nodeapi.lobby_send("should be gated")
        check("nodeapi: lobby send without a name errors",
              res.get("error") == "set a name first", str(res))
        nodeapi.set_name("mergetest")

        # 4. Sysop chat: a second node messages this one; poll picks it up.
        dir2 = os.path.join(TMP, "two")
        c2 = node_mod.NodeClient(dir2, "two", url)
        c2.announce()
        res = c2.chat_send(st["pubkey"], "hello node")
        check("nodeapi: peer chat send ok", res[0] == 200, str(res))
        chat = nodeapi.chat_poll()
        check("nodeapi: chat poll receives the message",
              any(m.get("text") == "hello node"
                  for m in chat.get("messages", [])), str(chat))

        # 5. Per-peer policy + favorites stay local and round-trip.
        res = nodeapi.set_policy(c2.identity.public_hex(), "allow")
        check("nodeapi: policy allow stored",
              res.get("policy", {}).get(c2.identity.public_hex()) == "allow")
        res = nodeapi.set_policy(c2.identity.public_hex(), "clear")
        check("nodeapi: policy clear removes",
              c2.identity.public_hex() not in res.get("policy", {}))
        res = nodeapi.add_favorite(c2.identity.public_hex(), "two")
        check("nodeapi: favorite added",
              c2.identity.public_hex() in res.get("favorites", {}))
        res = nodeapi.remove_favorite(c2.identity.public_hex())
        check("nodeapi: favorite removed",
              c2.identity.public_hex() not in res.get("favorites", {}))

        # 6. Text job queue + mailbox poll (no poller needed).
        # mailbox_poll returns replies only: a job met here is
        # bounced back verbatim for the serving poller, not shown.
        import tunnel as tunnel_mod
        st2, body = tunnel_mod.send_job(
            url, c2.identity, st["pubkey"], "text", {"text": "job?"})
        check("nodeapi: text job queued", st2 == 200, str(body))
        box = nodeapi.mailbox_poll()
        check("nodeapi: mailbox poll bounces the job (replies only)",
              box.get("messages", []) == [], str(box))
        st2b, box2 = tunnel_mod.poll_mailbox(url, ident)
        check("nodeapi: bounced job is back in the mailbox",
              st2b == 200 and any(m.get("capability") == "text"
                  for m in box2.get("messages", [])), str(box2))

        # 7. Poller as a managed service: start, alive, stop, gone.
        check("nodeapi: poller starts",
              servers.poller_start(list_url=url) is True)
        time.sleep(2)
        check("nodeapi: poller running after start",
              servers.poller_running() is True)
        check("nodeapi: poller stops",
              servers.poller_stop() is True)
        time.sleep(1)
        check("nodeapi: poller gone after stop",
              servers.poller_running() is False)

        # 8. TTS + image jobs (0.0.2): validation errors + queued sends.
        # Error cases return before any network, so they need no mock;
        # queued cases post to the mock list this identity announced on.
        res = nodeapi.send_job(st["pubkey"], "music", {"text": "x"})
        check("nodeapi: unknown capability errors",
              "error" in res, str(res))
        res = nodeapi.send_job(st["pubkey"], "tts", {"text": "   "})
        check("nodeapi: tts without text errors",
              "error" in res, str(res))
        res = nodeapi.send_job(st["pubkey"], "image", {"prompt": "   "})
        check("nodeapi: image without prompt errors",
              "error" in res, str(res))
        res = nodeapi.send_job(st["pubkey"], "tts",
                               {"text": "hello", "voice": "mya-03"})
        check("nodeapi: tts job queued",
              res.get("ok") is True and bool(res.get("job_id")), str(res))
        res = nodeapi.send_job(st["pubkey"], "image",
                               {"prompt": "a test render",
                                "width": 512, "height": 512})
        check("nodeapi: image job queued",
              res.get("ok") is True and bool(res.get("job_id")), str(res))

        # 9. Reply fetching must not eat jobs (mirror of the poller
        # reply bounce): the section-8 jobs sit in this mailbox;
        # mailbox_poll returns no jobs and re-queues them verbatim.
        box = nodeapi.mailbox_poll()
        check("nodeapi: mailbox poll returns no jobs (replies only)",
              box.get("messages", []) == [], str(box))
        st3, box2 = tunnel_mod.poll_mailbox(url, ident)
        caps = sorted(m.get("capability") for m in box2.get("messages", []))
        check("nodeapi: bounced jobs are back in the mailbox",
              st3 == 200 and caps == ["image", "tts"], str(box2))
    finally:
        try:
            servers.poller_stop()
        except Exception:
            pass
        server.shutdown()

    # 10. Voice UI ships in the web UI: Generate voice button,
    # waveform player, and Save voice download on audio replies.
    ui = open(os.path.join(ROOT, "hub", "web_ui.html")).read()
    check("ui: Generate voice button present",
          "Generate voice" in ui and "voiceForText" in ui)
    check("ui: waveform player present",
          "waveplayer" in ui and "initWavePlayers" in ui)
    check("ui: Save voice download present",
          "Save voice" in ui and "wavesave" in ui)
    check("ui: bare audio-controls render replaced",
          "<audio controls" not in ui)
    # 11. Voice jobs stay visible + auto-fetch (2026-10-29 fix):
    # the old flow flashed the queued notice on the button for 2.5s
    # only, so a Fetch before the peer's 60s poller finished showed
    # nothing and the job id was lost. Now the job is pinned to the
    # chat message and auto-fetched every 15s, with consumed replies
    # buffered so fetch-and-clear cannot lose them.
    check("ui: voice job pinned to chat message",
          "pendingVoices" in ui and "voice-status" in ui)
    check("ui: voice auto-fetch every 5s",
          "tickVoicePoll" in ui and "setInterval(tickVoicePoll, 5000)" in ui)
    check("ui: voice replies buffered for Node tab",
          "voiceReplyBuffer" in ui)
    check("ui: voice timeout fails loud, not silent",
          "No voice reply after 10 minutes" in ui)
    # 12. Node tab redesign (2026-10-29; the identity header moved
    # into its own page in 0.0.1f): left menu
    # with separate Lobby/Peers/Private chat/Files/Jobs/Poller pages,
    # lobby rendered as name + node_id small underneath, name gate.
    check("ui: node left menu with separate pages",
          all(x in ui for x in ("nodePage-lobby", "nodePage-peers",
                                "nodePage-chat", "nodePage-files",
                                "nodePage-jobs", "nodePage-poller",
                                "showNodePage")))
    check("ui: lobby renders name + node_id underneath",
          "lobby-nodeid" in ui and "renderLobby" in ui)
    check("ui: lobby name gate present",
          "set a name first" in ui.lower() and "nodeNameGate" in ui
          and "nodeLobbyGate" in ui)
    # 13. IRC-console Node tab (2026-10-29): Identity left-menu page,
    # Node tab on general load, lobby auto-refresh 15s (client side),
    # black console lobby + blinking cursor, click-a-node card with
    # shared endpoint + private chat, local-only lobby block.
    check("ui: Identity left-menu button + page",
          "nodePage-identity" in ui and 'data-nodepage="identity"' in ui
          and 'id="nodeName"' in ui and 'id="nodeIdent"' in ui)
    check("ui: Chat is the default on refresh",
          "showWelcome(); loadStatus(); loadSessions({openChat:true}); loadStories();\nshowTab('chat');" in ui)
    check("ui: lobby auto-refresh every 5s client side",
          "nodeTimer" in ui and "pollNodeLobby" in ui
          and "setInterval(function(){ pollNodeLobby(); }, 5000)" in ui)
    check("ui: click-a-node card with endpoint + private chat",
          "showNodeCard" in ui and "lobby-name-btn" in ui
          and "no endpoint shared" in ui and "fillChatTo" in ui)
    check("ui: local-only lobby block",
          "nodeBlockedV1" in ui and "toggleNodeBlock" in ui)
    check("ui: black console lobby + blinking cursor",
          "#nodePage-lobby,#nodeLobby{background:#000}" in ui
          and "console-cursor" in ui and "consoleBlink" in ui)
    # 14. Memory provenance + node view + TTS share (2026-10-29):
    # per-message trace, full node view, download icon, Share button,
    # local shared files + publish, ledger status.
    check("ui: per-message memory trace render",
          "memtrace" in ui and "j.memories" in ui)
    check("ui: full node view loader",
          "loadNodeView" in ui and "nodeView" in ui and "/api/memories/list" in ui)
    check("ui: download icon + share on waveform player",
          "⬇ Save voice" in ui and "waveshare" in ui and "/api/voice/share" in ui)
    check("ui: local shared files + publish",
          "nodeSharedFiles" in ui and "publishSharedFile" in ui
          and "/api/voice/publish" in ui and "/api/voice/shared" in ui)
    web = open(os.path.join(ROOT, "hub", "web.py")).read()
    check("api: memories list + voice + ledger routes",
          all(x in web for x in ("/api/memories/list", "/api/voice/share",
                                 "/api/voice/publish", "/api/voice/shared",
                                 "/api/ledger/status", "_list_memories_full",
                                 "_voice_share", "_voice_publish", "_ledger_status")))
    check("api: chat done event carries used memories",
          '"memories": used_memories' in web)
    reg = open(os.path.join(ROOT, "hub", "regula.py")).read()
    check("regula: ledger + bundle manifest present",
          all(x in reg for x in ("def append", "def verify", "def merkle_root",
                                 "def bundle_manifest", "def verify_manifest",
                                 "prev_hash", "event_hash")))
    # 15. Ledger on every write + 50MB block store (2026-10-29):
    # sessions, stories, ezines, code, samples, models chunked into
    # data/blocks/; ledger events view + chunk fetch endpoint.
    check("regula: chunk store + record present",
          all(x in reg for x in ("CHUNK_SIZE = 50 * 1024 * 1024",
                                 "def chunk_file", "def store_chunked",
                                 "def record_file", "def get_chunk",
                                 "def verify_blocks", "def list_events",
                                 "def blocks_count", "def sign_manifest")))
    check("api: ledger events + block chunk routes",
          all(x in web for x in ("/api/ledger/events", "/api/blocks/",
                                 "_ledger_events", "_block_chunk",
                                 "_ledger_record", '"blocks": regula.blocks_count')))
    check("api: session/story/ezine/code writes ledgered",
          all(x in web for x in ('_ledger_record("session", pth, stype)',
                                 '_ledger_record("story", r["md"], sid)',
                                 '_ledger_record("ezine", r["md"], r["ezine_id"])',
                                 '_ledger_record("code", path, body["file"])')))
    check("api: publish chunks into the block store",
          'regula.chunk_file(data_dir(), pth)' in web
          and '"chunks": chunks' in web)
    check("ui: ledger events view in Files tab",
          "nodeLedgerEvents" in ui and "/api/ledger/events" in ui)
    samples = open(os.path.join(ROOT, "hub", "samples.py")).read()
    check("samples: ingest records each file",
          'regula.record_file(_data_dir(), "sample", path, name)' in samples)
    hfmodels = open(os.path.join(ROOT, "hub", "hfmodels.py")).read()
    check("models: download completion chunks into blocks",
          'regula.store_chunked(data_dir(), "model", path' in hfmodels)

    # ---- 0.0.1d: live system panels, telemetry, lobby, peers, endpoints
    ui = open(os.path.join(ROOT, "hub", "web_ui.html")).read()
    check("d: no mockup panels left in the UI",
          "MOCKUP" not in ui and "mockpanel" not in ui)
    check("d: system tab renders live ledger + regula",
          "sys-ledger-table" in ui and "sys-regula-lines" in ui
          and "sys-ledger-val" in ui and "j.ledger" in ui and "j.regula" in ui)
    check("d: chat messages persist telemetry + render it on reload",
          "function addMsg(role, text, model, extra)" in ui
          and "tokens:effToks" in ui and "tok_s:" in ui
          and "m.tokens" in ui and "m.tok_s" in ui)
    check("d: lobby has Announce beside Refresh + IRC presence roster",
          'onclick="announceNode()">Announce' in ui
          and "nodePresence" in ui and "renderPresence" in ui
          and "refreshPresence" in ui)
    check("d: node card offers private chat, job, block, add peer",
          "Add peer" in ui and "toggleNodeFav" in ui
          and "fillJobTo" in ui)
    check("d: peers offer request access (LLM/image/TTS) via sysop chat",
          "requestAccess" in ui and "Request LLM" in ui
          and "Request image" in ui and "Request TTS" in ui
          and "nodePeersOut" in ui)
    check("d: chat has peer text backend + peer image + default voice peer",
          'id="peerBackend"' in ui and "peerSendChat" in ui
          and 'id="peerImageBackend"' in ui and "sendPeerImage" in ui
          and "defaultVoicePeer" in ui and "saveDefaultVoicePeer" in ui)
    check("d: jobs image form carries an engine field",
          'id="nodeJobEngine"' in ui and "body.engine" in ui)
    check("d: chat images persist server-side, not as session base64",
          "/api/chat-images/save" in ui and "/api/chat-images/" in ui)
    web = open(os.path.join(ROOT, "hub", "web.py")).read()
    check("d: _system returns live ledger + regula status",
          '"ledger":' in web and '"regula":' in web
          and "regula.signing_probe" in web)
    check("d: _chat strips messages to role+content before model calls",
          '[{"role": m.get("role", "user"), "content": m.get("content", "")}' in web)
    check("d: chat-images save/get routes exist",
          '"/api/chat-images/save"' in web
          and '"/api/chat-images/"' in web)
    check("d: nodeapi passes image engine through to the job payload",
          'clean["engine"] = eng' in open(os.path.join(ROOT, "hub", "nodeapi.py")).read()
          and "_sanitize_engine" in open(os.path.join(ROOT, "hub", "nodeapi.py")).read())
    nd = open(os.path.join(ROOT, "node.py")).read()
    check("d: announce carries engine tags",
          "_announce_caps" in nd and "image-engine:" in nd
          and "tts-engine:" in nd)
    tun = open(os.path.join(ROOT, "tunnel.py")).read()
    check("d: receiver enforces the requested image engine",
          "engine_offered" in tun and "image_engine" in tun
          and "not offered here" in tun)

    # ---- 0.0.1f: Node tab screens + real IRC lobby (2026-10-30):
    # no persistent identity header above the pages — Identity is
    # its own page, loaded on click; the lobby page has nothing
    # above it but the menu; the lobby is a real IRC layout with
    # messages on the left and the presence roster on the right,
    # username with small node-id below.
    ui = open(os.path.join(ROOT, "hub", "web_ui.html")).read()
    check("f: no identity header above the Node pages",
          "node-identity" not in ui and "nodeIdentityCard" not in ui
          and ui.index('id="nodePage-lobby"') < ui.index('id="nodeName"'))
    check("f: Identity page holds the full identity UI, loaded on click",
          'id="nodePage-identity" hidden' in ui
          and 'data-nodepage="identity"' in ui
          and 'id="nodeName"' in ui and 'id="nodeIdent"' in ui
          and 'id="nodeNameGate"' in ui)
    check("f: lobby is a real IRC layout, roster on the right",
          "irc-row" in ui and "irc-main" in ui
          and 'id="nodePresence" class="irc-roster"' in ui)
    check("f: roster entries are username + small node-id below",
          "presence-user" in ui and "presence-nodeid" in ui
          and "renderPresence" in ui and "Present now" in ui)

    # ---- 0.0.1h: full-size IRC room + fast polls + Chat default (2026-10-30):
    # the Node tab breaks out of the 860px panel, lobby/chat message
    # areas are tall scroll regions that keep their scroll position
    # across refreshes; lobby polls every 5s, presence every 60s,
    # private chat every 5s while its page is open; a browser
    # refresh always lands on Chat; the voice-peer error names Job →.
    check("h: lobby messages every 5s, presence every 60s",
          "setInterval(function(){ pollNodeLobby(); }, 5000)" in ui
          and "setInterval(function(){ refreshPresence(); }, 60000)" in ui
          and "presenceTimer" in ui)
    check("h: private chat auto-refreshes every 5s while its page is open",
          "setInterval(function(){ pollNodeChat(); }, 5000)" in ui
          and "startChatAutoRefresh" in ui and "stopChatAutoRefresh" in ui)
    check("h: Node tab breaks out of the narrow panel, tall scroll rooms",
          "#view-node .panel{max-width:none}" in ui
          and "#nodeLobby{height:60vh" in ui
          and "#nodeChat{height:55vh" in ui)
    check("h: lobby/chat refresh keeps scroll position",
          "if(pinned) lel.scrollTop = lel.scrollHeight" in ui
          and "if(pinned) cel.scrollTop = cel.scrollHeight" in ui)
    check("h: refresh lands on Chat, the default page",
          "showTab('chat'); // Refresh always lands on Chat" in ui)
    check("h: voice-peer error names the Job step",
          "then Job → on its card" in ui)

    # ---- 0.0.1i: transfers, serving, share chats, faster voice polls ----
    check("i: Transfers page tracks jobs like a torrent client",
          'id="nodePage-transfers"' in ui and 'data-nodepage="transfers"' in ui
          and "renderTransfers" in ui and "refreshTransfers" in ui
          and "resendTransfer" in ui and "nodeTransfers" in ui)
    check("i: transfers auto-refresh every 5s while its page is open",
          "setInterval(function(){ refreshTransfers(); }, 5000)" in ui
          and "startTransferAutoRefresh" in ui and "stopTransferAutoRefresh" in ui)
    check("i: Serving page sets text backend, voice, endpoint",
          'id="nodePage-serving"' in ui and 'data-nodepage="serving"' in ui
          and "nodeServeBackend" in ui and "nodeServeKey" in ui
          and "nodeServeEndpoint" in ui and "saveServing" in ui
          and "/api/node/serving" in ui)
    check("i: chat sessions can be packaged and shared",
          "shareSession" in ui and "/share" in ui)
    check("i: voice reply poll runs every 5s",
          "setInterval(tickVoicePoll, 5000)" in ui)
    check("i: failed job replies are held and retried",
          "_pending_replies" in tun and "MAX_REPLY_TRIES" in tun
          and "held for retry" in tun)

    # ---- 0.0.1k: CETI rename, UI top, logo, screenshots ----
    readme = open(os.path.join(ROOT, "README.md")).read()
    check("k: UI top says CETI: Uncensored Node 0.0.1, Built by AI for Humanity",
          "<b>CETI: Uncensored Node</b> 0.0.1 · Built by AI for Humanity" in ui
          and "<title>CETI: Uncensored Node</title>" in ui)
    check("k: README embeds logo + UI screenshots",
          "docs/images/logo.svg" in readme
          and "docs/images/chat.png" in readme
          and "docs/images/node-lobby.png" in readme)
    check("k: logo + screenshots ship in the tree",
          os.path.isfile(os.path.join(ROOT, "docs", "images", "logo.svg"))
          and os.path.isfile(os.path.join(ROOT, "docs", "images", "chat.png"))
          and os.path.isfile(os.path.join(ROOT, "docs", "images",
                                          "node-lobby.png")))
    check("k: README states the CETI zero-token goal",
          "zero tokens at serve time" in readme
          and "AIML" in readme and "ledger" in readme.lower())
    check("k: README footnotes CETI AIML/BIOS not yet in this release",
          "does not use CETI AIML technology yet" in readme
          and "CETI BIOS for general intelligence" in readme
          and "used in developing this release" in readme)

    # ---- 0.0.1l: info/ folder with baked README ----
    baked = open(os.path.join(ROOT, "info",
                              "ceti-readme-baked.html")).read()
    check("l: info/ ships the baked README with images baked in",
          "data:image/png;base64" in baked
          and "data:image/svg+xml;base64" in baked
          and 'src="docs/images/' not in baked)
    check("l: README points at info/ and drops the android/ folder claim",
          "info/ceti-readme-baked.html" in readme
          and "from the `android/` folder" not in readme)
    check("l: no stale GitHub-release claim left on the j entry",
          "this is the GitHub release build" not in readme)

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
