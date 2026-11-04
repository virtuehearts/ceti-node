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

mgr = sshterm.SshManager(
    tempfile.mkdtemp(),
    command_builder=lambda *a: ["cmd", "/c", "echo", "CONPTY-PROOF-12345"])
sid = mgr.connect("example.com")
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
