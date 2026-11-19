"""Start, stop and health-check the chat and embedding servers."""
import json
import ntpath
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

from .config import chat_port, embed_port, data_dir, REPO_ROOT
from .llama import find_server, server_args


def _health(port: int) -> bool:
    try:
        with urllib.request.build_opener().open(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _pid_path(name: str) -> str:
    return os.path.join(data_dir(), f"{name}.pid")


def _log_tail(port: int, n: int = 20) -> str:
    path = os.path.join(data_dir(), f"server-{port}.log")
    try:
        with open(path, "rb") as f:
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return "(no server log yet)"


def _start(model_path: str, port: int, ctx: int, ngl: str, extra):
    server = find_server()
    if not server:
        raise RuntimeError("no llama-server found; run: python -m hub doctor --install")
    cmd = server_args(server) + ["-m", model_path, "-c", str(ctx), "--host", "127.0.0.1", "--port", str(port), "--n-gpu-layers", ngl] + extra
    print(f"starting server on port {port}: {' '.join(cmd)}")
    log = open(os.path.join(data_dir(), f"server-{port}.log"), "ab")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, **kwargs)
    with open(_pid_path(f"server-{port}"), "w") as f:
        f.write(str(proc.pid))
    return proc


def _stop(port: int):
    path = _pid_path(f"server-{port}")
    if not os.path.exists(path):
        return False
    try:
        pid = int(open(path).read().strip())
    except Exception:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, 15)
    except Exception:
        pass
    try:
        os.remove(path)
    except OSError:
        pass
    return True


def _wait_health(port: int, tries: int, proc=None) -> bool:
    """Wait for health, printing progress every ~10s. If proc is given
    and it exits early, fail fast instead of waiting the full timeout,
    and show the server log tail so the user can see why it died."""
    for i in range(tries):
        if _health(port):
            return True
        if proc is not None and proc.poll() is not None:
            print(f"server on port {port} exited early (code {proc.returncode}); last log lines:")
            print(_log_tail(port))
            return False
        if i % 5 == 0 and i > 0:
            print(f"  ... still waiting for port {port} ({i * 2}s so far)")
        time.sleep(2)
    print(f"server on port {port} did not come up in {tries * 2}s; last log lines:")
    print(_log_tail(port))
    return False


def ensure_servers(chat_model: str, embed_model: str, gpu: bool, ctx: int = 32768, embed_gpu: bool = False):
    """Architecture:
    chat on VRAM (hot path), embed on CPU/RAM by default (infrequent,
    tiny model, saves VRAM for chat). Set embed_gpu=True only if you
    want both on GPU."""
    """Start chat + embed servers in the background if their health checks fail.
    Returns (chat_ok, embed_ok). GPU offload is all layers when gpu=True, else 0.

    VRAM -> RAM fallback: if the GPU attempt does not come up (VRAM too
    small, driver issue), stop it and retry the same model on CPU in
    system RAM. The chat model still runs, just slower. The caller
    should tell the user the fallback happened — it is not silent.
    """
    if ctx not in (16384, 32768):
        raise ValueError("ctx must be 16384 or 32768 (16k or 32k)")
    chat_ok = _health(chat_port())
    if not chat_ok and chat_model and os.path.exists(chat_model):
        for attempt_ngl, label in ([("-1", "VRAM"), ("0", "RAM (CPU)")] if gpu else [("0", "RAM (CPU)")]):
            print(f"chat server attempt: {label} (ctx {ctx})")
            try:
                # 2026-11-07: --reasoning-budget 512. LFM3's chat template
                # enables thinking by default, and unrestricted thinking
                # traps the model (it burns its whole output budget
                # thinking and emits no content for distill/generation
                # prompts: 0 content at 3000 tokens, 13,190 chars
                # reasoning). --reasoning off fixes the empty output
                # but the distill drifts off-topic (the 1.7B model
                # cannot pick the topic rows out of a noisy report
                # without thinking). A 512-token budget is the sweet
                # spot (tested on the Nobel distill): 2,811 chars of
                # content, 8 QA + 8 facts, first QA naming all three
                # 2024 winners — better than reasoning-off (6 QA) and
                # better than budget 256 (0 proposals, the forced cutoff
                # broke the output). The budget counts towards
                # max_tokens, so callers must size max_tokens as
                # budget + answer (see research._local_complete and
                # the web.py fit budget). The CETI reasoning ladder is
                # Python retrieval, not model thinking, so it is
                # unaffected.
                proc = _start(chat_model, chat_port(), ctx, attempt_ngl, ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0", "--reasoning-budget", "512"])
            except RuntimeError as e:
                # No usable server binary: fail this server, not the
                # whole run — the caller still starts the web UI.
                print(f"chat server cannot start: {e}")
                break
            chat_ok = _wait_health(chat_port(), 90, proc=proc)
            if chat_ok:
                if label != "VRAM" and gpu:
                    print(f"chat server fell back to {label}: VRAM attempt did not come up")
                break
            _stop(chat_port())
            time.sleep(2)
    embed_ok = _health(embed_port())
    if not embed_ok and embed_model and os.path.exists(embed_model):
        for attempt_ngl, label in ([("-1", "VRAM"), ("0", "RAM (CPU)")] if embed_gpu else [("0", "RAM (CPU)")]):
            print(f"embedding server attempt: {label} (embed on {'VRAM' if embed_gpu else 'CPU/RAM'} by default)")
            try:
                proc = _start(embed_model, embed_port(), 8192, attempt_ngl, ["--embedding"])
            except RuntimeError as e:
                print(f"embedding server cannot start: {e}")
                break
            embed_ok = _wait_health(embed_port(), 60, proc=proc)
            if embed_ok:
                if label != "VRAM" and embed_gpu:
                    print(f"embedding server fell back to {label}")
                break
            _stop(embed_port())
            time.sleep(2)
    return chat_ok, embed_ok


def stop_all():
    return (_stop(chat_port()), _stop(embed_port()))


def embed_text(text: str, port=None):
    """Return a 768d embedding from the embedding server, or None if unavailable."""
    port = port or embed_port()
    payload = json.dumps({"input": text}).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/embeddings", data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.build_opener().open(req, timeout=60) as r:
            data = json.load(r)
        return data["data"][0]["embedding"]
    except Exception:
        return None



import re as _re

def strip_think_blocks(text: str):
    """Split text into (answer, thinking) by <think>...</think> blocks.
    Reasoning models sometimes emit their draft in the content stream
    instead of reasoning_content — those drafts must never be shown as
    the answer. Unclosed <think> at the end counts as thinking."""
    thinking_parts = []
    def _collect(m):
        thinking_parts.append(m.group(1))
        return ""
    answer = _re.sub(r"<think>(.*?)</think>", _collect, text, flags=_re.DOTALL | _re.IGNORECASE)
    m = _re.search(r"<think>(.*)$", answer, flags=_re.DOTALL | _re.IGNORECASE)
    if m:
        thinking_parts.append(m.group(1))
        answer = answer[:m.start()]
    answer = _re.sub(r"^\s*(thinking|draft|attempt)\s*:\s*", "", answer, flags=_re.IGNORECASE)
    return answer.strip(), "\n".join(thinking_parts).strip()


def split_think_chunk(chunk: str, in_think: bool):
    """Stateful split of one streamed content chunk into (answer_part, thinking_part, new_in_think).
    Handles <think> / </think> split across chunks via the in_think flag."""
    out_answer, out_think = [], []
    rest = chunk
    while rest:
        if in_think:
            idx = rest.lower().find("</think>")
            if idx == -1:
                out_think.append(rest); rest = ""; break
            out_think.append(rest[:idx]); rest = rest[idx + len("</think>"):]; in_think = False
        else:
            idx = rest.lower().find("<think>")
            if idx == -1:
                out_answer.append(rest); rest = ""; break
            out_answer.append(rest[:idx]); rest = rest[idx + len("<think>"):]; in_think = True
    return "".join(out_answer), "".join(out_think), in_think

def chat_complete(messages, port=None, max_tokens=2048, temperature=0.7, repeat_penalty=1.15, reasoning=True):
    """POST to the chat server's OpenAI-compatible API. Returns (content, reasoning).

    reasoning=False asks the server to disable thinking for this call
    (chat_template_kwargs enable_thinking=false). The LFM3 chat
    template enables thinking by default, and the model burns its whole
    output budget thinking and emits no content for distill/generation
    prompts; with reasoning off it answers directly. Default True
    preserves current behaviour.
    """
    port = port or chat_port()
    body = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature, "repeat_penalty": repeat_penalty}
    if not reasoning:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.build_opener().open(req, timeout=3600) as r:
        data = json.load(r)
    msg = data["choices"][0]["message"]
    answer, think = strip_think_blocks(msg.get("content") or "")
    reasoning = (msg.get("reasoning_content") or "")
    if think:
        reasoning = (reasoning + "\n" + think).strip() if reasoning else think
    return answer, reasoning


def stream_chat(messages, port=None, max_tokens=2048, temperature=0.7, cancel=None, repeat_penalty=1.15, reasoning=True):
    """Stream a chat completion, yielding content chunks as they arrive.
    Uses the server's OpenAI-compatible SSE stream. cancel is an optional
    callable returning True when the caller wants to stop early.
    Falls back to a single non-streaming call if streaming fails.
    reasoning=False disables thinking for this call (see chat_complete)."""
    import urllib.request
    port = port or chat_port()
    body = {"messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "stream": True, "repeat_penalty": repeat_penalty}
    if not reasoning:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                 data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.build_opener().open(req, timeout=3600)
    except Exception:
        content, _r = chat_complete(messages, port=port, max_tokens=max_tokens, temperature=temperature, repeat_penalty=repeat_penalty, reasoning=reasoning)
        yield content
        return
    buf = b""
    try:
        while True:
            if cancel is not None and cancel():
                break
            chunk = resp.read(1024)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    return
                try:
                    obj = json.loads(data.decode("utf-8", errors="ignore"))
                except Exception:
                    continue
                try:
                    delta = obj["choices"][0]["delta"]
                except Exception:
                    continue
                text = delta.get("content") or ""
                if text:
                    # Fallback path: content may carry <think> drafts
                    ans, _th = strip_think_blocks(text)
                    if ans:
                        yield ans
    finally:
        try:
            resp.close()
        except Exception:
            pass


def stream_chat_events(messages, port=None, max_tokens=2048, temperature=0.7, cancel=None, repeat_penalty=1.15, reasoning=True):
    """Stream a chat completion, yielding (kind, text) events.
    kind is 'thinking' for reasoning_content, 'text' for content.
    This is what makes a reasoning model feel alive in the UI:
    without it the user stares at a blank box while the model thinks,
    then gets the answer all at once.
    reasoning=False disables thinking for this call (see chat_complete);
    with reasoning off the stream carries only 'text' events.
    """
    import urllib.request
    port = port or chat_port()
    body = {"messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "stream": True, "repeat_penalty": repeat_penalty,
            "stream_options": {"include_usage": True}}
    if not reasoning:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    payload = json.dumps(body).encode("utf-8")
    in_think = False
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                 data=payload, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.build_opener().open(req, timeout=3600)
    except Exception:
        content, _r = chat_complete(messages, port=port, max_tokens=max_tokens, temperature=temperature, repeat_penalty=repeat_penalty, reasoning=reasoning)
        yield ("text", content)
        return
    buf = b""
    try:
        while True:
            if cancel is not None and cancel():
                break
            chunk = resp.read(1024)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    return
                try:
                    obj = json.loads(data.decode("utf-8", errors="ignore"))
                except Exception:
                    continue
                usage = obj.get("usage") or {}
                if usage.get("completion_tokens"):
                    yield ("usage", {"tokens": usage["completion_tokens"]})
                try:
                    delta = obj["choices"][0]["delta"]
                except Exception:
                    continue
                if delta.get("reasoning_content"):
                    yield ("thinking", delta["reasoning_content"])
                if delta.get("content"):
                    ans, th, in_think = split_think_chunk(delta["content"], in_think)
                    if th:
                        yield ("thinking", th)
                    if ans:
                        yield ("text", ans)
    finally:
        try:
            resp.close()
        except Exception:
            pass


# ---- node poller (managed service) -------------------------------------
# The poller is the firewall tunnel: it polls this node's mailbox on
# node.darknet.ca outbound and serves text jobs from the local Ollama
# model to peers this node allows (peers.json). It never listens.
# Default is text-only: tts needs a local PocketTTS bridge and image
# needs a Z-Image queue, so operators who have those run run_poller.py
# directly with --image-queue/--image-outdir (tts is on unless
# --no-tts). The Servers card starts the text-only form.

def poller_pid_path():
    return os.path.join(data_dir(), "node-poller.pid")


def poller_log_path():
    return os.path.join(data_dir(), "node-poller.log")


def poller_running():
    path = poller_pid_path()
    if not os.path.exists(path):
        return False
    try:
        pid = int(open(path).read().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
    except Exception:
        return False
    return True


def poller_log_tail(n=30):
    try:
        with open(poller_log_path(), "rb") as f:
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-n:])
    except OSError:
        return "(no poller log yet)"


def poller_start(list_url=None):
    if poller_running():
        return True
    cmd = [sys.executable, os.path.join(REPO_ROOT, "run_poller.py"),
           "--data-dir", data_dir(),
           "--list-url", list_url or "https://node.darknet.ca",
           "--no-tts", "--interval", "60"]
    log = open(poller_log_path(), "ab")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, **kwargs)
    with open(poller_pid_path(), "w") as f:
        f.write(str(proc.pid))
    return True


def poller_stop():
    path = poller_pid_path()
    if not os.path.exists(path):
        return False
    try:
        pid = int(open(path).read().strip())
    except Exception:
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, 15)
    except Exception:
        pass
    try:
        os.remove(path)
    except OSError:
        pass
    return True


# ---- TTS server (PocketTTS bridge, :8000) ---------------------------
# The bridge is the container's old voice panel, brought into the Hub
# UI: the System view's Servers card gets a TTS SERVER row (start/stop)
# plus a voice panel (pick a voice, test it, set the default). The
# bridge is started by autorun.py at boot; these functions let the UI
# restart it without restarting the Hub. Same PID-file pattern as the
# poller. The start script execs uvicorn, so on Linux the recorded
# PID IS the server; on Windows the .bat spawns python under cmd, so
# stop uses taskkill /T to take the whole tree. autorun.py writes the
# same PID file at boot, so Stop works on the boot-started bridge too.

def tts_port():
    try:
        return int(os.environ.get("TTS_PORT", "8000"))
    except ValueError:
        return 8000


def tts_pid_path():
    return os.path.join(data_dir(), "tts.pid")


def tts_log_path():
    return os.path.join(data_dir(), "tts.log")


def tts_health():
    """True when the bridge answers /health (auth is OFF, localhost)."""
    try:
        with urllib.request.build_opener().open(
                f"http://127.0.0.1:{tts_port()}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def tts_running():
    return tts_health()


def _tts_cmd(script, is_windows=None):
    """Use the script's basename on Windows. The launcher sets its cwd."""
    if is_windows is None:
        is_windows = os.name == "nt"
    if is_windows:
        return ["cmd.exe", "/d", "/c", "call", ntpath.basename(script)]
    return ["bash", script]


def _tts_windows_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def launch_tts(script, log):
    if os.name == "nt":
        # Children of a console-less cmd.exe may allocate a visible console.
        # Give cmd.exe a hidden console for the whole batch process tree.
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        options = {"creationflags": subprocess.CREATE_NEW_CONSOLE,
                   "startupinfo": startupinfo}
    else:
        options = {"start_new_session": True}
    return subprocess.Popen(
        _tts_cmd(script), cwd=os.path.dirname(script), stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT, **options)



def tts_log_tail(n=25):
    """Last n lines of the bridge log (data/tts.log), for the System
    view: a failed first-run install used to leave only DOWN and a
    static "starting…" line, with the real error buried in this file
    where the UI never looked."""
    try:
        with open(tts_log_path(), "r", encoding="utf-8",
                  errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def tts_starting():
    """True when a start was recorded (PID file) but /health is still
    down — the first-run venv + model install takes minutes, and the
    UI should say "starting" only in this state, not on a dead start."""
    if tts_health():
        return False
    try:
        with open(tts_pid_path()) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    if os.name == "nt":
        # No kill(pid, 0) on Windows: ask tasklist whether the PID
        # is alive. Fail open to "starting" when the check itself
        # errors — health is the arbiter, this only labels the row.
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid],
                capture_output=True, text=True, timeout=10, **_tts_windows_options())
            return str(pid) in (out.stdout or "")
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def tts_start():
    """Start the bridge in the background. Returns True when the
    process was launched (not when it is healthy — first run builds
    a venv and downloads the model, minutes; the UI polls status).
    True immediately when it is already up, False when tts/ is
    missing or the launch itself fails."""
    if tts_health():
        return True
    tts_dir = os.path.join(REPO_ROOT, "tts")
    script = os.path.join(tts_dir, "start_tts.bat" if os.name == "nt" else "start_tts.sh")
    if not os.path.isfile(script):
        return False
    try:
        with open(tts_log_path(), "ab") as log:
            try:
                proc = launch_tts(script, log)
            except OSError as error:
                log.write(f"tts: launch failed: {error}\n".encode("utf-8"))
                return False
    except OSError:
        return False
    with open(tts_pid_path(), "w") as f:
        f.write(str(proc.pid))
    return True


def stop_tts_process_tree(pid):
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True, timeout=10, **_tts_windows_options())


def tts_stop():
    """Stop the bridge via the PID file (whole tree on Windows).
    Returns True when a process was stopped. When no PID file
    exists but the bridge is up (an older boot that never recorded
    one), there is nothing safe to kill by PID, so it reports
    False and the UI says to restart the Hub."""
    path = tts_pid_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            pid = int(f.read().strip())
    except Exception:
        return False
    try:
        if os.name == "nt":
            stop_tts_process_tree(pid)
        else:
            os.kill(pid, 15)
    except Exception:
        pass
    try:
        os.remove(path)
    except OSError:
        pass
    return True


def tts_voices():
    """Voices the bridge knows, from its voices.db (id, name, type).
    Falls back to the two built-ins when the DB is missing or
    unreadable, so the panel always has something to offer."""
    builtin = [{"id": "mya-03", "name": "Mya 03", "type": "builtin"},
               {"id": "mya-02", "name": "Mya 02", "type": "builtin"}]
    db = os.path.join(REPO_ROOT, "tts", "data", "voices.db")
    if not os.path.isfile(db):
        return builtin
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute("SELECT id, name, type FROM voices ORDER BY name").fetchall()
        finally:
            conn.close()
    except Exception:
        return builtin
    out = [{"id": str(r[0]), "name": str(r[1]), "type": str(r[2])} for r in rows]
    return out or builtin
