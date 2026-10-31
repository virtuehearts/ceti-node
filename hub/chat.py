"""Chat loop with memory: retrieve relevant memories, answer, then remember."""
import sys

from . import memory
from .servers import chat_complete, embed_text


def run_chat(ident, conn, once=None):
    print(f"identity: {ident.fingerprint}  ({ident.node_id})")
    print(f"memories for this identity: {memory.count(conn, ident.hash)}")
    print("Type a message, or /quit to stop. /recall <query> searches memory without chatting.")
    if once is not None:
        _turn(ident, conn, once)
        return
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line.startswith("/recall "):
            _recall(ident, conn, line[len("/recall "):].strip())
            continue
        _turn(ident, conn, line)


def _recall(ident, conn, query: str):
    emb = embed_text(query)
    if emb is None:
        print("embedding server not running; start it with: python -m hub serve")
        return
    for score, text, source, rid, mdl in memory.search(conn, ident.hash, emb, top_k=5):
        print(f"[{score:.3f}] ({source}{', ' + mdl if mdl else ''}) {text}")


def _turn(ident, conn, user_text: str):
    emb = embed_text(user_text)
    context = ""
    if emb is not None:
        hits = memory.search(conn, ident.hash, emb, top_k=5)
        hits = [h for h in hits if h[0] > 0.35]
        if hits:
            context = "Relevant memories from this identity (use them if they help, ignore them if not):\n" + "\n".join(f"- {t}" for _, t, _, _, _ in hits) + "\n"
    messages = []
    if context:
        messages.append({"role": "system", "content": context})
    messages.append({"role": "user", "content": user_text})
    try:
        answer, _reasoning = chat_complete(messages)
    except Exception as e:
        print(f"chat server error: {e} (is it running? python -m hub serve)")
        return
    print(f"hub> {answer}")
    # Remember both sides, so future recall finds the exchange.
    if emb is not None:
        memory.add(conn, ident.hash, f"User: {user_text}", emb, source="chat")
        ans_emb = embed_text(answer)
        if ans_emb is not None:
            memory.add(conn, ident.hash, f"Assistant: {answer}", ans_emb, source="chat")
