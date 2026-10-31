"""PTY smoke test: drive the real curses TUI through menu -> status -> quit."""
import fcntl
import os
import pty
import select
import struct
import sys
import tempfile
import termios
import time

tmp = tempfile.mkdtemp(prefix="hub-tui-smoke-")
env = dict(os.environ, HUB_DATA_DIR=tmp, TERM="xterm-256color")
pid, fd = pty.fork()
if pid == 0:
    os.chdir("/home/hatch/workspace/uncensored-hub")
    os.execvpe("python3", ["python3", "-m", "hub", "tui"], env)
    os._exit(1)

fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))


def drain(seconds):
    out = b""
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    return out


def send(text):
    os.write(fd, text.encode())
    time.sleep(0.5)


buf = drain(3)
send("4\n")      # status door
buf += drain(2)
send("/menu\n")  # back to menu
buf += drain(1)
send("0\n")      # quit
buf += drain(2)
try:
    os.close(fd)
except OSError:
    pass
_, status = os.waitpid(pid, 0)
code = os.waitstatus_to_exitcode(status)
text = buf.decode("utf-8", "replace")
print("exit code:", code)
print("traceback present:", "Traceback" in text)
print("bytes captured:", len(buf))
sys.exit(0 if code == 0 and "Traceback" not in text else 1)
