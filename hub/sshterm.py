"""Local-operator-only SSH terminal support for the Hub web UI.

Stdlib + the system ssh binary only (no paramiko). Nothing in this
module is reachable from the node network: the web UI handlers in
hub/web.py are the only callers, and the Hub binds to loopback. An SSH
session opened here is the operator's own shell to a host they chose —
never a capability offered to peers.

Platforms: POSIX uses pty/fcntl; native Windows uses ConPTY via
ctypes (Windows 10+). The Unix-only imports are guarded, so this
module can never again take down the web UI at import time the way
0.0.1m did on Windows — a platform with no PTY fails at connect
with a clear error instead.

Security notes:
- ssh arguments are built as a list, never a shell string.
- Host key policy is StrictHostKeyChecking=accept-new against a
  node-local known_hosts file: a changed host key hard-fails instead
  of silently accepting.
- Private key material is never returned by any function here and
  never logged; key files are created 0600.
- Tests drive the PTY plumbing with `cat` by passing a
  command_builder to SshManager — there is no environment override
  in shipped code, so nothing outside the operator's own process
  can redirect the spawned command.
"""

import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time

# Unix-only PTY modules: fcntl/pty/termios do not exist on native
# Windows, and importing them unguarded killed the whole web UI on
# the laptop (0.0.1m). They are optional here: POSIX uses them,
# Windows uses ConPTY (below), and a platform with neither gets a
# clear error at connect time — never an import-time crash.
try:
    import fcntl
    import pty
    import termios
    _HAVE_POSIX_PTY = True
except ImportError:
    fcntl = None
    pty = None
    termios = None
    _HAVE_POSIX_PTY = False

DEFAULT_HOST = {
    "id": "chat-hf-co",
    "name": "chat.hf.co",
    "host": "chat.hf.co",
    "port": 22,
    "username": "",
    "key_id": "",
}

ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_IPV6_BRACKET_RE = re.compile(r"^\[[0-9A-Fa-f:]+\]$")
_SHELL_META = set(" \t\r\n/;|&$`\"'\\<>(){}[]!*?~#")

IDLE_TIMEOUT_SECONDS = 30 * 60
_MAX_BUFFER = 4 * 1024 * 1024


def validate_id(value):
    """A safe id: alphanumerics, dot, underscore, hyphen; no '..'."""
    if not isinstance(value, str) or not value or ".." in value:
        raise ValueError("invalid id")
    if ID_RE.match(value) is None:
        raise ValueError("invalid id")
    return value


def validate_host(host):
    """A non-empty hostname or IP. Rejects whitespace, slashes and
    shell metacharacters outright — this value is passed to ssh as
    a single argv element, but it must still look like a host."""
    if not isinstance(host, str) or not host:
        raise ValueError("host is required")
    if _IPV6_BRACKET_RE.match(host):
        return host
    if any(ch in _SHELL_META for ch in host):
        raise ValueError("invalid host")
    m = _IPV4_RE.match(host)
    if m:
        if all(0 <= int(g) <= 255 for g in m.groups()):
            return host
        raise ValueError("invalid host")
    if _HOSTNAME_RE.match(host) is None:
        raise ValueError("invalid host")
    return host


def validate_port(port):
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValueError("invalid port")
    if isinstance(port, bool) or not 1 <= p <= 65535:
        raise ValueError("port out of range")
    return p


_DEFAULT_COLS, _DEFAULT_ROWS = 80, 24
_MIN_COLS, _MAX_COLS = 10, 500
_MIN_ROWS, _MAX_ROWS = 5, 200


def _clamp_size(cols, rows):
    """Clamp a terminal size to a sane, non-zero range.

    0.0.1u: a measured size can be 0 when the pane was hidden or
    not yet laid out at fit time, and CreatePseudoConsole with a
    0x0 size (or ssh negotiating 0x0) is exactly the vertical-
    input bug. Never let a 0, negative, or absurd size reach a
    session: unparseable values fall back to 80x24, everything
    else is clamped into range."""
    try:
        c = int(cols)
    except (TypeError, ValueError):
        c = _DEFAULT_COLS
    try:
        r = int(rows)
    except (TypeError, ValueError):
        r = _DEFAULT_ROWS
    # Zero or negative is not a small terminal, it is no
    # terminal at all (the 0x0 case) — fall back, don't clamp
    # it to the minimum and ship a 10x5 TUI.
    if c <= 0:
        c = _DEFAULT_COLS
    if r <= 0:
        r = _DEFAULT_ROWS
    c = max(_MIN_COLS, min(_MAX_COLS, c))
    r = max(_MIN_ROWS, min(_MAX_ROWS, r))
    return c, r


def _slug(name):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "host").strip())
    slug = slug.strip(".-") or "host"
    return slug[:64]


class SshStore:
    """Saved hosts (JSON) and saved keys (ssh-keygen files) under
    <data_dir>/ssh_hosts.json and <data_dir>/ssh_keys/."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.hosts_path = os.path.join(data_dir, "ssh_hosts.json")
        self.keys_dir = os.path.join(data_dir, "ssh_keys")
        self.known_hosts_path = os.path.join(data_dir, "ssh_known_hosts")
        self._lock = threading.Lock()
        os.makedirs(self.keys_dir, exist_ok=True)

    # ---- hosts ------------------------------------------------------
    def ensure_default_host(self):
        with self._lock:
            if not os.path.exists(self.hosts_path):
                with open(self.hosts_path, "w", encoding="utf-8") as f:
                    json.dump([dict(DEFAULT_HOST)], f, indent=2)
                return dict(DEFAULT_HOST)
            return None

    def list_hosts(self):
        self.ensure_default_host()
        with self._lock:
            try:
                with open(self.hosts_path, encoding="utf-8") as f:
                    hosts = json.load(f)
            except (OSError, ValueError):
                raise ValueError("saved hosts file is unreadable")
            if not isinstance(hosts, list):
                raise ValueError("saved hosts file is unreadable")
            return [dict(h) for h in hosts]

    def get_host(self, host_id):
        validate_id(host_id)
        for h in self.list_hosts():
            if h.get("id") == host_id:
                return h
        return None

    def save_host(self, entry):
        """Create or update a host by id. Returns the stored entry."""
        host = validate_host(entry.get("host", ""))
        port = validate_port(entry.get("port", 22))
        username = entry.get("username", "") or ""
        key_id = entry.get("key_id", "") or ""
        if not isinstance(username, str) or any(
                ch in username for ch in " \t\r\n/;|&$`\"'\\"):
            raise ValueError("invalid username")
        if key_id:
            validate_id(key_id)
            if not os.path.exists(self.key_path(key_id)):
                raise ValueError("unknown key_id")
        name = entry.get("name", "") or host
        if not isinstance(name, str):
            raise ValueError("invalid name")
        hosts = self.list_hosts()
        host_id = entry.get("id") or ""
        if host_id:
            validate_id(host_id)
        else:
            host_id = _slug(name)
            base, n = host_id, 2
            taken = {h.get("id") for h in hosts}
            while host_id in taken:
                host_id = "%s-%d" % (base, n)
                n += 1
        stored = {"id": host_id, "name": name, "host": host,
                  "port": port, "username": username, "key_id": key_id}
        with self._lock:
            hosts = [h for h in hosts if h.get("id") != host_id]
            hosts.append(stored)
            tmp = self.hosts_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(hosts, f, indent=2)
            os.replace(tmp, self.hosts_path)
        return stored

    def delete_host(self, host_id):
        validate_id(host_id)
        hosts = self.list_hosts()
        kept = [h for h in hosts if h.get("id") != host_id]
        if len(kept) == len(hosts):
            return False
        with self._lock:
            tmp = self.hosts_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(kept, f, indent=2)
            os.replace(tmp, self.hosts_path)
        return True

    # ---- keys -------------------------------------------------------
    def key_path(self, key_id):
        validate_id(key_id)
        return os.path.join(self.keys_dir, key_id)

    def list_keys(self):
        """Public metadata only — private key material never leaves
        this module."""
        out = []
        for name in sorted(os.listdir(self.keys_dir)):
            if name.endswith(".pub"):
                continue
            if ID_RE.match(name) is None:
                continue
            pub_path = self.key_path(name) + ".pub"
            pub = ""
            fingerprint = ""
            try:
                with open(pub_path, encoding="utf-8") as f:
                    pub = f.read().strip()
            except OSError:
                continue
            try:
                r = subprocess.run(
                    ["ssh-keygen", "-lf", pub_path],
                    capture_output=True, text=True, timeout=15)
                if r.returncode == 0 and r.stdout.strip():
                    fingerprint = r.stdout.strip().split()[1]
            except Exception:
                pass
            out.append({"id": name, "public": pub,
                        "fingerprint": fingerprint})
        return out

    def generate_key(self, key_id):
        validate_id(key_id)
        path = self.key_path(key_id)
        if os.path.exists(path) or os.path.exists(path + ".pub"):
            raise ValueError("key already exists")
        if shutil.which("ssh-keygen") is None:
            raise RuntimeError("ssh-keygen not found")
        r = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", path,
             "-C", "ceti-node-sshterm"],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError("ssh-keygen failed")
        os.chmod(path, 0o600)
        for k in self.list_keys():
            if k["id"] == key_id:
                return k
        raise RuntimeError("key generation did not produce a public key")

    def delete_key(self, key_id):
        validate_id(key_id)
        removed = False
        for pth in (self.key_path(key_id), self.key_path(key_id) + ".pub"):
            try:
                os.remove(pth)
                removed = True
            except OSError:
                pass
        return removed


def default_ssh_argv(host, port, username, key_path, known_hosts_path):
    # -tt (not -t) forces a remote PTY even when local stdin is a
    # pipe: 0.0.1r used a single -t and the laptop's Windows
    # OpenSSH printed "Pseudo-terminal will not be allocated
    # because stdin is not a terminal", so the HF gateway closed
    # the session with "Requires an active PTY". The pipe session
    # has no local TTY by construction (piped stdio +
    # CREATE_NO_WINDOW), and -tt is the documented form that
    # forces allocation even with no local tty. The fullscreen
    # TUI needs the remote PTY to render.
    # IdentitiesOnly=yes stops ssh offering stray default keys:
    # with no key selected the gateway treats the session as
    # anonymous (its TUI), and with a key selected only that key
    # is offered.
    argv = ["ssh", "-tt", "-p", str(port),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "IdentitiesOnly=yes",
            "-o", "UserKnownHostsFile=" + known_hosts_path]
    if username:
        argv += ["-l", username]
    if key_path:
        argv += ["-i", key_path]
    argv.append(host)
    return argv


class _PosixSession:
    session_type = "posix"

    @property
    def pid(self):
        return self.proc.pid

    @property
    def buffered(self):
        with self._lock:
            return len(self.buffer)

    def __init__(self, session_id, argv, cols, rows):
        self.id = session_id
        self.cols = cols
        self.rows = rows
        self.buffer = bytearray()
        self.base_offset = 0
        self.last_activity = time.time()
        self._lock = threading.Lock()
        self._dead = False
        master, slave = pty.openpty()
        self._set_winsize(master, cols, rows)
        self.proc = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, start_new_session=True)
        os.close(slave)
        self.master = master
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    @staticmethod
    def _set_winsize(fd, cols, rows):
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def _read_loop(self):
        try:
            while True:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                with self._lock:
                    self.buffer += data
                    if len(self.buffer) > _MAX_BUFFER:
                        drop = len(self.buffer) - _MAX_BUFFER
                        del self.buffer[:drop]
                        self.base_offset += drop
                    self.last_activity = time.time()
        finally:
            with self._lock:
                self._dead = True

    def alive(self):
        with self._lock:
            dead = self._dead
        if dead:
            return False
        return self.proc.poll() is None

    def read_since(self, offset):
        # Delivery polls count as reads: a connected browser tab
        # keeps the session alive, and idle reap fires once polling
        # stops (e.g. the tab closed) for the idle timeout.
        with self._lock:
            self.last_activity = time.time()
            if offset < self.base_offset:
                offset = self.base_offset
            rel = offset - self.base_offset
            if rel >= len(self.buffer):
                return b"", self.base_offset + len(self.buffer)
            data = bytes(self.buffer[rel:])
            return data, self.base_offset + len(self.buffer)

    def write(self, data):
        with self._lock:
            self.last_activity = time.time()
        try:
            os.write(self.master, data)
        except OSError:
            with self._lock:
                self._dead = True
            raise

    def resize(self, cols, rows):
        cols, rows = _clamp_size(cols, rows)
        self.cols, self.rows = cols, rows
        with self._lock:
            self.last_activity = time.time()
        self._set_winsize(self.master, cols, rows)

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass
        with self._lock:
            self._dead = True


_CREATE_NO_WINDOW = 0x08000000


def _process_has_console():
    """True when this process is attached to a console (win32
    only). Any failure falls back to False — the probe must never
    crash a session. On non-Windows this is always False."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return False


def _pipe_creationflags(has_console):
    # 0.0.1t: CREATE_NO_WINDOW strips the child's console, and
    # Windows OpenSSH reads the remote PTY size from the console
    # — with no console it negotiates 0x0, and the HF TUI lays
    # its input out one char per line (laptop, 0.0.1s) while a
    # normal console connection renders standard size. When the
    # parent HAS a console (launched from PowerShell/cmd),
    # inheriting it gives ssh a real size with no new window:
    # stdio stays piped, so nothing writes to that console, and
    # no window appears that was not already there. With no
    # console, keep CREATE_NO_WINDOW (same as before, no worse).
    return 0 if has_console else _CREATE_NO_WINDOW


def _pipe_env(cols, rows):
    # TERM is set because there is no local terminal to inherit
    # it from; COLUMNS/LINES ride along for TUIs that consult
    # the environment as a size fallback.
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["COLUMNS"] = str(cols)
    env["LINES"] = str(rows)
    return env


class _PipeSession:
    """Pipe-based session: the Windows default since 0.0.1r, and
    the ConPTY fallback since 0.0.1u.

    ConPTY (below) deterministically opened a visible Terminal
    window on the laptop across 0.0.1n–q, with empty pipes, so
    it was dropped as the Windows path in r. This one cannot
    open a window by construction: ssh runs with piped stdio
    and, on Windows, CREATE_NO_WINDOW, so it never gets a
    console at all. The remote side still gets a PTY via
    `ssh -tt` in the argv, so TUIs render; TERM is set
    explicitly because there is no local terminal to inherit
    it from. Same interface as _PosixSession, except resize
    only records the size: with no local PTY there is nothing
    to propagate it to, so the remote PTY keeps ssh's
    connect-time size — since 0.0.1t, the inherited parent
    console's size when one exists, else 0x0. That is why the
    frontend now measures the pane before connecting: for a
    pipe session, connect-time size is the only size that
    ever reaches the remote side.
    """

    session_type = "pipe"

    @property
    def pid(self):
        return self.proc.pid

    @property
    def buffered(self):
        with self._lock:
            return len(self.buffer)

    def __init__(self, session_id, argv, cols, rows):
        self.id = session_id
        self.cols = cols
        self.rows = rows
        self.buffer = bytearray()
        self.base_offset = 0
        self.last_activity = time.time()
        self._lock = threading.Lock()
        self._dead = False
        env = _pipe_env(cols, rows)
        kwargs = {"stdin": subprocess.PIPE,
                  "stdout": subprocess.PIPE,
                  "stderr": subprocess.STDOUT,
                  "env": env, "close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = _pipe_creationflags(
                _process_has_console())
        self.proc = subprocess.Popen(argv, **kwargs)
        self._out_fd = self.proc.stdout.fileno()
        self._reader = threading.Thread(target=self._read_loop,
                                        daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            while True:
                try:
                    data = os.read(self._out_fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                with self._lock:
                    self.buffer += data
                    if len(self.buffer) > _MAX_BUFFER:
                        drop = len(self.buffer) - _MAX_BUFFER
                        del self.buffer[:drop]
                        self.base_offset += drop
                    self.last_activity = time.time()
        finally:
            with self._lock:
                self._dead = True

    def alive(self):
        with self._lock:
            dead = self._dead
        if dead:
            return False
        return self.proc.poll() is None

    def read_since(self, offset):
        with self._lock:
            self.last_activity = time.time()
            if offset < self.base_offset:
                offset = self.base_offset
            rel = offset - self.base_offset
            if rel >= len(self.buffer):
                return b"", self.base_offset + len(self.buffer)
            data = bytes(self.buffer[rel:])
            return data, self.base_offset + len(self.buffer)

    def write(self, data):
        with self._lock:
            self.last_activity = time.time()
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            with self._lock:
                self._dead = True
            raise

    def resize(self, cols, rows):
        # No local PTY to resize; record only.
        cols, rows = _clamp_size(cols, rows)
        self.cols, self.rows = cols, rows
        with self._lock:
            self.last_activity = time.time()

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        with self._lock:
            self._dead = True


if sys.platform == "win32":
    # Windows Pseudo Console (ConPTY) via ctypes — stdlib only, no
    # pywinpty. Same session interface as _PosixSession. Windows 10+
    # only; older systems fail at connect with a clear error.
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.windll.kernel32
    _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    # 0.0.1v headless flag: STARTF_USESHOWWINDOW + SW_HIDE
    # tells a GUI child to start hidden. CREATE_NO_WINDOW was
    # tried in u and is REMOVED in v: it sets the child's
    # ConsoleHandle to a reserved no-window value, which can
    # override the pseudo-console reference passed in the
    # attribute list — exactly the empty-pipes symptom u
    # showed on the laptop (conpty pid, no output yet). The
    # pseudo console itself is the child's console, so no
    # separate window should appear without it. Laptop gate:
    # tools/conpty_selftest.py must pass with no window before
    # this path is trusted — the fallback below only fires on
    # a creation exception, not on a window popping up or on
    # empty pipes after a successful start.
    _STARTF_USESHOWWINDOW = 0x00000001
    _SW_HIDE = 0
    _STILL_ACTIVE = 259
    _S_OK = 0

    class _COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class _STARTUPINFO(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD),
                    ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD),
                    ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD),
                    ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD),
                    ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p),
                    ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE),
                    ("hStdError", wintypes.HANDLE)]

    class _STARTUPINFOEX(ctypes.Structure):
        _fields_ = [("StartupInfo", _STARTUPINFO),
                    ("lpAttributeList", ctypes.c_void_p)]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE),
                    ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    _kernel32.CreatePseudoConsole.argtypes = [
        _COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE)]
    _kernel32.CreatePseudoConsole.restype = ctypes.HRESULT
    _kernel32.ResizePseudoConsole.argtypes = [wintypes.HANDLE, _COORD]
    _kernel32.ResizePseudoConsole.restype = ctypes.HRESULT
    _kernel32.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
    _kernel32.ClosePseudoConsole.restype = None
    _kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t)]
    _kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    _kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_void_p]
    _kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    _kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _kernel32.DeleteProcThreadAttributeList.restype = None
    _kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
        wintypes.LPCWSTR, ctypes.POINTER(_STARTUPINFOEX),
        ctypes.POINTER(_PROCESS_INFORMATION)]
    _kernel32.CreateProcessW.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE,
                                              wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD

    class _ConPtySession:
        """Windows Pseudo Console session. Same interface as
        _PosixSession: buffer/base_offset/last_activity plus
        alive/read_since/write/resize/close. Since 0.0.1v it
        starts headless via STARTF_USESHOWWINDOW/SW_HIDE
        only (CREATE_NO_WINDOW removed — it overrode the
        pseudo-console reference in u and gave empty pipes),
        with bInheritHandles=True and a mutable command
        line, matching Microsoft's ConPTY sample, and
        resize() really propagates via ResizePseudoConsole
        — unlike the pipe session, whose resize only
        records. The initial size is the pane size the
        frontend measured before connecting, not a
        hardcoded 80x24."""

        session_type = "conpty"

        @property
        def pid(self):
            return self._pid

        @property
        def buffered(self):
            with self._lock:
                return len(self.buffer)

        def __init__(self, session_id, argv, cols, rows):
            self.id = session_id
            self.cols = cols
            self.rows = rows
            self.buffer = bytearray()
            self.base_offset = 0
            self.last_activity = time.time()
            self._lock = threading.Lock()
            self._dead = False
            self._hpc = None
            self._hproc = None
            self._pid = None
            self._fds = []
            self._in_w = None
            self._out_r = None
            try:
                in_r, in_w = os.pipe()
                self._fds += [in_r, in_w]
                out_r, out_w = os.pipe()
                self._fds += [out_r, out_w]
                # 0.0.1q: the handles handed to CreatePseudoConsole
                # must be inheritable, exactly as in Microsoft's
                # ConPTY sample. os.pipe() fds are non-inheritable
                # by default (PEP 446) — on the laptop the child
                # kept landing in a visible Terminal window with
                # empty pipes instead of on the pseudo console.
                for _fd in (in_r, in_w, out_r, out_w):
                    os.set_inheritable(_fd, True)
                self._in_w = in_w
                self._out_r = out_r
                # The console reads h_in and writes h_out; we write
                # in_w and read out_r. Do not close in_r/out_w
                # before CreateProcessW — that deadlocks or
                # breaks the console. They are closed in the
                # parent AFTER CreateProcessW (0.0.1v, as in
                # the sample); in_w/out_r stay open until
                # ClosePseudoConsole.
                h_in = msvcrt.get_osfhandle(in_r)
                h_out = msvcrt.get_osfhandle(out_w)
                hpc = wintypes.HANDLE()
                hr = _kernel32.CreatePseudoConsole(
                    _COORD(cols, rows), h_in, h_out, 0,
                    ctypes.byref(hpc))
                if hr != _S_OK:
                    raise RuntimeError(
                        "ConPTY unavailable (CreatePseudoConsole "
                        "failed: 0x%08x)" % (hr & 0xFFFFFFFF))
                self._hpc = hpc
                size = ctypes.c_size_t(0)
                _kernel32.InitializeProcThreadAttributeList(
                    None, 1, 0, ctypes.byref(size))
                attr_buf = ctypes.create_string_buffer(size.value)
                if not _kernel32.InitializeProcThreadAttributeList(
                        attr_buf, 1, 0, ctypes.byref(size)):
                    raise RuntimeError(
                        "ConPTY unavailable (attribute list failed)")
                try:
                    hpc_value = ctypes.c_void_p(hpc.value)
                    if not _kernel32.UpdateProcThreadAttribute(
                            attr_buf, 0,
                            _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                            ctypes.byref(hpc_value),
                            ctypes.sizeof(hpc_value), None, None):
                        raise RuntimeError(
                            "ConPTY unavailable (attribute update failed)")
                    sie = _STARTUPINFOEX()
                    sie.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEX)
                    # 0.0.1v: start hidden, via SW_HIDE only.
                    # CREATE_NO_WINDOW is deliberately NOT
                    # here (see the v note above).
                    sie.StartupInfo.dwFlags = _STARTF_USESHOWWINDOW
                    sie.StartupInfo.wShowWindow = _SW_HIDE
                    sie.lpAttributeList = ctypes.cast(attr_buf,
                                                      ctypes.c_void_p)
                    pi = _PROCESS_INFORMATION()
                    # CreateProcessW modifies its command
                    # line in place, so it must be a mutable
                    # buffer, not a Python str — the
                    # Microsoft sample passes a mutable
                    # wchar buffer, and u passed a str.
                    cmdline = ctypes.create_unicode_buffer(
                        subprocess.list2cmdline(argv))
                    # 0.0.1v: bInheritHandles=True, matching
                    # Microsoft's ConPTY sample. u passed
                    # False, a deviation from the sample
                    # that was never tested on its own.
                    ok = _kernel32.CreateProcessW(
                        None, cmdline, None, None, True,
                        _EXTENDED_STARTUPINFO_PRESENT
                        | _CREATE_UNICODE_ENVIRONMENT,
                        None, None, ctypes.byref(sie),
                        ctypes.byref(pi))
                    if not ok:
                        err = ctypes.get_last_error()
                        raise RuntimeError(
                            "ConPTY process start failed (error %d)"
                            % err)
                    self._hproc = pi.hProcess
                    self._pid = pi.dwProcessId
                    _kernel32.CloseHandle(pi.hThread)
                    # 0.0.1v: close the console-owned pipe
                    # ends in the parent, as the sample
                    # does. The parent keeps only in_w
                    # (write to the console) and out_r
                    # (read from it); keeping out_w open
                    # in the parent also means the output
                    # pipe can never report EOF when the
                    # child dies.
                    for _fd in (in_r, out_w):
                        try:
                            os.close(_fd)
                        except OSError:
                            pass
                        if _fd in self._fds:
                            self._fds.remove(_fd)
                finally:
                    _kernel32.DeleteProcThreadAttributeList(attr_buf)
            except Exception:
                self._cleanup_handles()
                raise
            self._reader = threading.Thread(target=self._read_loop,
                                            daemon=True)
            self._reader.start()

        def _read_loop(self):
            try:
                while True:
                    try:
                        data = os.read(self._out_r, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    with self._lock:
                        self.buffer += data
                        if len(self.buffer) > _MAX_BUFFER:
                            drop = len(self.buffer) - _MAX_BUFFER
                            del self.buffer[:drop]
                            self.base_offset += drop
                        self.last_activity = time.time()
            finally:
                with self._lock:
                    self._dead = True

        def alive(self):
            with self._lock:
                dead = self._dead
            if dead or self._hproc is None:
                return False
            code = wintypes.DWORD(0)
            if not _kernel32.GetExitCodeProcess(self._hproc,
                                                ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE

        def read_since(self, offset):
            with self._lock:
                self.last_activity = time.time()
                if offset < self.base_offset:
                    offset = self.base_offset
                rel = offset - self.base_offset
                if rel >= len(self.buffer):
                    return b"", self.base_offset + len(self.buffer)
                data = bytes(self.buffer[rel:])
                return data, self.base_offset + len(self.buffer)

        def write(self, data):
            with self._lock:
                self.last_activity = time.time()
            try:
                os.write(self._in_w, data)
            except OSError:
                with self._lock:
                    self._dead = True
                raise

        def resize(self, cols, rows):
            cols, rows = _clamp_size(cols, rows)
            self.cols, self.rows = cols, rows
            with self._lock:
                self.last_activity = time.time()
            if self._hpc is not None:
                _kernel32.ResizePseudoConsole(self._hpc,
                                              _COORD(cols, rows))

        def _cleanup_handles(self):
            # Console first, then process handle, then pipe fds.
            if self._hpc is not None:
                try:
                    _kernel32.ClosePseudoConsole(self._hpc)
                except Exception:
                    pass
                self._hpc = None
            if self._hproc is not None:
                try:
                    _kernel32.CloseHandle(self._hproc)
                except Exception:
                    pass
                self._hproc = None
            for fd in self._fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._fds = []

        def close(self):
            try:
                if self._hproc is not None and self.alive():
                    _kernel32.TerminateProcess(self._hproc, 1)
                    _kernel32.WaitForSingleObject(self._hproc, 2000)
            except Exception:
                pass
            self._cleanup_handles()
            with self._lock:
                self._dead = True


def _make_windows_session(session_id, argv, cols, rows):
    """Windows session factory, split out so it is testable
    from Linux: try ConPTY first, fall back to the pipe
    session on any creation failure. The fallback is
    creation-failure only — it cannot rescue a ConPTY child
    that started successfully but popped a window or fed
    empty pipes, which is why the laptop self-test gate in
    tools/conpty_selftest.py (no window, output on the pipe,
    session_type conpty) must pass before this path ships
    as the default. The pipe fallback keeps the 0.0.1t
    console-inherit behaviour, so a ConPTY failure is no
    worse than t."""
    cols, rows = _clamp_size(cols, rows)
    try:
        return _ConPtySession(session_id, argv, cols, rows)
    except Exception:
        return _PipeSession(session_id, argv, cols, rows)


def _make_session(session_id, argv, cols, rows):
    """Platform session factory. Never crashes at import: a platform
    with no PTY at all fails here, at connect time, with a clear
    error the endpoint returns as JSON."""
    cols, rows = _clamp_size(cols, rows)
    if sys.platform == "win32":
        # 0.0.1u: ConPTY first (headless, real resize), pipe
        # fallback on creation failure. Pipe was the sole
        # Windows path in r–t because ConPTY opened a visible
        # window on the laptop across n–q; the u headless
        # flags plus the laptop self-test gate are what make
        # the return to ConPTY safe to attempt.
        return _make_windows_session(session_id, argv, cols, rows)
    if not _HAVE_POSIX_PTY:
        raise RuntimeError("PTY unavailable on this platform")
    return _PosixSession(session_id, argv, cols, rows)


class SshManager:
    """Thread-safe registry of live PTY sessions for one data dir."""

    def __init__(self, data_dir, command_builder=None,
                 idle_timeout=IDLE_TIMEOUT_SECONDS):
        self.store = SshStore(data_dir)
        self._command_builder = command_builder
        self.idle_timeout = idle_timeout
        self._sessions = {}
        self._lock = threading.Lock()
        self._reaper = None

    def _build_argv(self, host, port, username, key_id):
        if self._command_builder is not None:
            return self._command_builder(host, port, username, key_id)
        if shutil.which("ssh") is None:
            raise RuntimeError("ssh binary not found")
        key_path = None
        if key_id:
            key_path = self.store.key_path(key_id)
            if not os.path.exists(key_path):
                raise ValueError("unknown key_id")
        return default_ssh_argv(host, port, username, key_path,
                                self.store.known_hosts_path)

    def _start_reaper(self):
        if self._reaper is not None:
            return
        def sweep():
            while True:
                time.sleep(60)
                self.reap_idle()
        self._reaper = threading.Thread(target=sweep, daemon=True)
        self._reaper.start()

    def connect(self, host, port=22, username="", key_id="",
                cols=80, rows=24):
        host = validate_host(host)
        port = validate_port(port)
        # 0.0.1u: clamp here as well as in _make_session, so a
        # measured 0x0 (hidden pane) or absurd size can never
        # reach CreatePseudoConsole or the remote PTY.
        cols, rows = _clamp_size(cols, rows)
        if username and (not isinstance(username, str) or any(
                ch in username for ch in " \t\r\n/;|&$`\"'\\")):
            raise ValueError("invalid username")
        if key_id:
            validate_id(key_id)
        argv = self._build_argv(host, port, username, key_id)
        session_id = "ssh-" + secrets.token_hex(8)
        sess = _make_session(session_id, argv, cols, rows)
        with self._lock:
            self._sessions[session_id] = sess
        self._start_reaper()
        return session_id

    def _get(self, session_id):
        validate_id(session_id)
        with self._lock:
            sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError("unknown session")
        return sess

    def read_since(self, session_id, offset):
        sess = self._get(session_id)
        data, new_offset = sess.read_since(offset)
        return data, new_offset, sess.alive()

    def write(self, session_id, data):
        self._get(session_id).write(data)

    def resize(self, session_id, cols, rows):
        cols, rows = _clamp_size(cols, rows)
        self._get(session_id).resize(cols, rows)

    def status(self, session_id):
        sess = self._get(session_id)
        return {"session_id": session_id, "alive": sess.alive(),
                "cols": sess.cols, "rows": sess.rows,
                "session_type": sess.session_type, "pid": sess.pid,
                "buffered": sess.buffered,
                "idle_seconds": round(time.time() - sess.last_activity, 1)}

    def disconnect(self, session_id):
        with self._lock:
            sess = self._sessions.pop(session_id, None)
        if sess is None:
            return False
        sess.close()
        return True

    def reap_idle(self):
        now = time.time()
        stale = []
        with self._lock:
            for sid, sess in self._sessions.items():
                if now - sess.last_activity > self.idle_timeout:
                    stale.append(sid)
        for sid in stale:
            self.disconnect(sid)
        return stale

    def shutdown(self):
        with self._lock:
            sids = list(self._sessions)
        for sid in sids:
            self.disconnect(sid)
