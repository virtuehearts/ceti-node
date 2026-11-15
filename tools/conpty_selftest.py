"""Windows-only ConPTY self-test for the Hub SSH terminal.

Run from the extracted release folder on the laptop:

    python tools\\conpty_selftest.py

It spawns `cmd /c echo CONPTY-PROOF-12345` through the same
ConPTY session code the web UI uses, and reports whether the output
reached the pipe buffer. No window should appear: the child runs
headless on a pseudo console. If a console window pops up, or no
output arrives, the ConPTY attachment itself is broken on this
machine — that is the difference between "the terminal works but
lands in the wrong window" and "the browser pane never receives
anything". Since 0.0.1u this test FAILS if the session silently
fell back to the pipe session: a pipe pass would prove nothing
about ConPTY, and the laptop gate for the ConPTY-first default
is exactly "conpty, headless, output on the pipe". Exits 0 on
PASS, 1 on FAIL, 2 when not run on Windows.

    python tools\\conpty_selftest.py ssh

Same ConPTY path, but with the real ssh argv to chat.hf.co
(the default host, no key, accept-new) instead of cmd echo.
This separates "ConPTY is broken" from "ssh itself failed":
a live pid with zero bytes is the PTY; an exit code (e.g. 255)
is ssh failing at the network/auth layer, and its error text
is in the buffer. 15 seconds of patience for the handshake.

    python tools\\conpty_selftest.py ssh-pipe

The real ssh argv through the default pipe session (the
0.0.1-beta2 Windows default). PASS is bytes on the pipe.
"""

import os
import sys
import tempfile
import time

if sys.platform != "win32":
    print("conpty self-test: Windows only; this platform uses POSIX PTYs.")
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hub import sshterm  # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "echo"

if MODE == "echo":
    mgr = sshterm.SshManager(
        tempfile.mkdtemp(),
        command_builder=lambda *a: ["cmd", "/c", "echo", "CONPTY-PROOF-12345"])
    # Echo mode explicitly opts into ConPTY: it is the ConPTY
    # gate, and the default is now the pipe session (beta2).
    sid = mgr.connect("example.com", use_conpty=True)
    st = mgr.status(sid)
    print("session_type=%s pid=%s alive=%s" % (
        st["session_type"], st["pid"], st["alive"]))
    deadline = time.time() + 10
    data = b""
    while time.time() < deadline:
        data, _off, alive = mgr.read_since(sid, 0)
        if b"CONPTY-PROOF" in data:
            break
        if not alive:
            break
        time.sleep(0.2)
    mgr.disconnect(sid)
    if st["session_type"] != "conpty":
        print("FAIL: session fell back to %s, not conpty — the "
              "ConPTY-first gate is not met, even if output arrived."
              % st["session_type"])
        sys.exit(1)
    if b"CONPTY-PROOF" in data:
        print("PASS: ConPTY carried output to the pipe buffer.")
    else:
        print("FAIL: no output reached the pipe buffer.")
        sys.exit(1)
elif MODE in ("ssh", "ssh-pipe"):
    use_conpty = MODE == "ssh"
    mgr = sshterm.SshManager(tempfile.mkdtemp())
    sid = mgr.connect("chat.hf.co", use_conpty=use_conpty)
    st = mgr.status(sid)
    print("mode=%s session_type=%s pid=%s alive=%s" % (
        MODE, st["session_type"], st["pid"], st["alive"]))
    deadline = time.time() + 15
    data = b""
    alive = True
    while time.time() < deadline:
        data, _off, alive = mgr.read_since(sid, 0)
        if data:
            break
        if not alive:
            break
        time.sleep(0.2)
    st = mgr.status(sid)
    print("after 15s: alive=%s buffered=%d exit_code=%s" % (
        st["alive"], st["buffered"], st["exit_code"]))
    if data:
        print("first bytes: %r" % data[:200])
    mgr.disconnect(sid)
    if data:
        print("PASS: ssh output reached the %s buffer." % st["session_type"])
    elif not st["alive"] and st["exit_code"] is not None:
        print("FAIL: ssh exited with code %s before any output "
              "(network/auth/host-key failure, not the PTY). "
              "Its error text is above, if any." % st["exit_code"])
        sys.exit(1)
    else:
        print("FAIL: ssh still running but zero bytes — the PTY "
              "never delivered (the u–beta symptom).")
        sys.exit(1)
else:
    print("usage: conpty_selftest.py [echo|ssh|ssh-pipe]")
    sys.exit(2)
