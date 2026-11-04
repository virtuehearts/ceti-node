"""SSH terminal tests — stdlib unittest only, temp dirs only.

Covers hub/sshterm.py (host validation, port range, id safety, key
generation at 0600 with no private material in list output, PTY
plumbing via the SshManager command_builder seam) and the web endpoints
(hosts/keys/connect/input/output/status/resize/disconnect, the
strict /assets allowlist, and the SSH workspace type). The PTY tests
spawn `cat` through the command_builder seam — never a real network
connection or sshd. Served from a real server thread on a throwaway
HUB_DATA_DIR. Never touches the real data/ directory.
"""
import base64
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from hub import sshterm
from hub import web as webmod
from hub.identity import ensure_identity
from hub.sshterm import SshManager, SshStore, _PipeSession, validate_host, validate_id, validate_port
from hub.web import Handler


class ValidationTest(unittest.TestCase):
    def test_good_hosts(self):
        for h in ("chat.hf.co", "example.com", "localhost", "192.168.1.10",
                  "host-name.example.org", "a"):
            self.assertEqual(validate_host(h), h)
        self.assertEqual(validate_host("[::1]"), "[::1]")

    def test_bad_hosts(self):
        for h in ("", "host name", "a/b", "a;b", "a|b", "a$b", "a`b",
                  "a'b", 'a"b', "a\\b", "a<b", "256.1.1.1", "-bad-",
                  ".bad", "bad.", None, 42):
            with self.assertRaises(ValueError, msg=repr(h)):
                validate_host(h)

    def test_port_range(self):
        self.assertEqual(validate_port(22), 22)
        self.assertEqual(validate_port("2222"), 2222)
        for p in (0, -1, 65536, "abc", None, True):
            with self.assertRaises(ValueError, msg=repr(p)):
                validate_port(p)

    def test_id_safety(self):
        self.assertEqual(validate_id("chat-hf-co"), "chat-hf-co")
        for bad in ("", "..", "../etc", "a/b", "a b", "a;b", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_id(bad)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SshStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_preseed_default_host(self):
        hosts = self.store.list_hosts()
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0]["host"], "chat.hf.co")
        self.assertEqual(hosts[0]["port"], 22)
        self.assertEqual(hosts[0]["username"], "")
        self.assertEqual(hosts[0]["key_id"], "")

    def test_save_update_delete_host(self):
        saved = self.store.save_host({"name": "mine", "host": "example.com",
                                      "port": 2222, "username": "ops"})
        self.assertEqual(saved["host"], "example.com")
        self.assertEqual(saved["port"], 2222)
        again = self.store.save_host({"id": saved["id"], "name": "mine",
                                      "host": "example.com", "port": 22})
        self.assertEqual(again["id"], saved["id"])
        self.assertEqual(again["port"], 22)
        self.assertTrue(self.store.delete_host(saved["id"]))
        self.assertFalse(self.store.delete_host(saved["id"]))
        self.assertEqual(len(self.store.list_hosts()), 1)  # default remains

    def test_save_host_rejects_bad(self):
        with self.assertRaises(ValueError):
            self.store.save_host({"host": "bad host"})
        with self.assertRaises(ValueError):
            self.store.save_host({"host": "example.com", "port": 70000})
        with self.assertRaises(ValueError):
            self.store.save_host({"host": "example.com",
                                  "key_id": "no-such-key"})

    def test_key_generate_mode_and_list_hides_private(self):
        meta = self.store.generate_key("testkey")
        priv = os.path.join(self.tmp.name, "ssh_keys", "testkey")
        self.assertEqual(oct(os.stat(priv).st_mode & 0o777), "0o600")
        self.assertTrue(os.path.exists(priv + ".pub"))
        self.assertIn("public", meta)
        listed = self.store.list_keys()
        self.assertEqual(len(listed), 1)
        blob = json.dumps(listed)
        with open(priv, encoding="utf-8") as f:
            private_text = f.read().strip()
        self.assertNotIn(private_text, blob)
        self.assertNotIn("PRIVATE KEY", blob)
        with self.assertRaises(ValueError):
            self.store.generate_key("testkey")
        self.assertTrue(self.store.delete_key("testkey"))
        self.assertEqual(self.store.list_keys(), [])


class PtyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = SshManager(self.tmp.name,
                              command_builder=lambda *a: ["cat"])

    def tearDown(self):
        self.mgr.shutdown()
        self.tmp.cleanup()

    def test_spawn_write_read_disconnect(self):
        sid = self.mgr.connect("example.com")
        self.assertTrue(self.mgr.status(sid)["alive"])
        self.mgr.write(sid, b"hello-pty\n")
        deadline = time.time() + 5
        got = b""
        while time.time() < deadline:
            data, off, _alive = self.mgr.read_since(sid, 0)
            got += data
            if b"hello-pty" in got:
                break
            time.sleep(0.1)
        self.assertIn(b"hello-pty", got)
        self.mgr.resize(sid, 120, 40)
        self.assertEqual(self.mgr.status(sid)["cols"], 120)
        self.assertTrue(self.mgr.disconnect(sid))
        with self.assertRaises(KeyError):
            self.mgr.status(sid)

    def test_read_offset_advances(self):
        sid = self.mgr.connect("example.com")
        self.mgr.write(sid, b"abc\n")
        time.sleep(0.5)
        data1, off, _ = self.mgr.read_since(sid, 0)
        self.assertIn(b"abc", data1)
        data2, _off2, _ = self.mgr.read_since(sid, off)
        self.assertEqual(data2, b"")

    def test_idle_reap(self):
        mgr = SshManager(self.tmp.name,
                         command_builder=lambda *a: ["cat"],
                         idle_timeout=1)
        try:
            sid = mgr.connect("example.com")
            time.sleep(1.3)
            self.assertIn(sid, mgr.reap_idle())
            with self.assertRaises(KeyError):
                mgr.status(sid)
        finally:
            mgr.shutdown()


class EndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_env = os.environ.get("HUB_DATA_DIR")
        cls._old_builder = webmod._ssh_command_builder
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["HUB_DATA_DIR"] = cls.tmp.name
        webmod._ssh_command_builder = lambda *a: ["cat"]
        ensure_identity(cls.tmp.name)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        if cls._old_env is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = cls._old_env
        webmod._ssh_command_builder = cls._old_builder
        cls.tmp.cleanup()

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_ssh_session_type(self):
        status, j = self._req("POST", "/api/sessions",
                              {"title": "term", "messages": [],
                               "type": "ssh"})
        self.assertEqual(status, 200)
        sid = j["id"]
        status, j = self._req("GET", "/api/sessions")
        rec = [s for s in j["sessions"] if s["id"] == sid]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["type"], "SSH")

    def test_ui_surfaces_stream_errors_and_byte_check(self):
        # 0.0.1o: a blank pane must never fail silently — the UI
        # wires an EventSource error handler and a 3s byte-count
        # check into the status line.
        req = urllib.request.Request("http://127.0.0.1:%d/" % self.port)
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8")
        self.assertIn("sshEs.onerror", html)
        self.assertIn("bytes received", html)
        # 0.0.1r: the connect status line shows the running build.
        self.assertIn("j.build", html)
        # 0.0.1p: the vendored addon-fit UMD exposes the
        # constructor as FitAddon.FitAddon, not FitAddon.
        self.assertIn("FitAddon.FitAddon", html)

    def test_ui_init_ssh_term_before_connect(self):
        # 0.0.1u: the terminal is mounted and fitted BEFORE
        # connect, and connect carries the measured size.
        # sshAttach must reuse that terminal, not dispose and
        # recreate it, and a failed attach must disconnect
        # the server session instead of orphaning it.
        req = urllib.request.Request("http://127.0.0.1:%d/" % self.port)
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8")
        self.assertIn("async function initSshTerm", html)
        self.assertIn("function sshCreateTerm", html)
        connect = html[html.index("async function sshConnect"):
                       html.index("async function sshDisconnect")]
        self.assertIn("await initSshTerm()", connect)
        self.assertIn("cols:dims.cols", connect)
        self.assertIn("rows:dims.rows", connect)
        self.assertNotIn("cols:80", connect)
        self.assertIn("/api/ssh/disconnect", connect)
        attach = html[html.index("async function sshAttach"):
                      html.index("async function sshConnect")]
        self.assertIn("initSshTerm", attach)
        self.assertNotIn("dispose", attach)

    def test_addon_fit_constructs_via_namespace(self):
        # 0.0.1p regression: `new window.FitAddon()` threw
        # "window.FitAddon is not a constructor" on the laptop
        # because the UMD global is a namespace object. Prove the
        # UI's resolution expression yields a constructible class.
        if shutil.which("node") is None:
            self.skipTest("node not available")
        root = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        path = os.path.join(root, "hub", "assets", "addon-fit.js")
        code = (
            "const fs = require('fs'); const vm = require('vm');\n"
            "const src = fs.readFileSync(" + json.dumps(path) + ", 'utf8');\n"
            "const sb = {};\n"
            "sb.self = sb; sb.window = sb;\n"
            "vm.runInNewContext(src, sb);\n"
            "const C = sb.FitAddon && (sb.FitAddon.FitAddon || sb.FitAddon);\n"
            "if (typeof C !== 'function') throw new Error('no constructor');\n"
            "const inst = new C();\n"
            "if (typeof inst.activate !== 'function' || typeof inst.fit !== 'function')\n"
            "  throw new Error('bad instance');\n"
            "console.log('fit-addon-ok');\n"
        )
        r = subprocess.run(["node", "-e", code],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("fit-addon-ok", r.stdout)

    def test_hosts_crud_and_keys(self):
        status, j = self._req("GET", "/api/ssh/hosts")
        self.assertEqual(status, 200)
        self.assertTrue(any(h["host"] == "chat.hf.co"
                            for h in j["hosts"]))
        status, j = self._req("POST", "/api/ssh/hosts",
                              {"name": "web", "host": "example.com",
                               "port": 2222})
        self.assertEqual(status, 200)
        hid = j["host"]["id"]
        status, j = self._req("POST", "/api/ssh/hosts",
                              {"host": "bad host"})
        self.assertEqual(status, 400)
        status, j = self._req("POST", "/api/ssh/keys/generate",
                              {"id": "webkey"})
        self.assertEqual(status, 200)
        self.assertIn("public", j["key"])
        self.assertNotIn("PRIVATE KEY", json.dumps(j))
        status, j = self._req("GET", "/api/ssh/keys")
        self.assertTrue(any(k["id"] == "webkey" for k in j["keys"]))
        self.assertNotIn("PRIVATE KEY", json.dumps(j))
        status, j = self._req("POST", "/api/ssh/keys/delete",
                              {"id": "webkey"})
        self.assertEqual(status, 200)
        status, j = self._req("POST", "/api/ssh/hosts/delete",
                              {"id": hid})
        self.assertEqual(status, 200)

    def test_connect_input_output_disconnect(self):
        status, j = self._req("POST", "/api/ssh/connect",
                              {"host": "example.com"})
        self.assertEqual(status, 200)
        sid = j["session_id"]
        status, j = self._req("POST", "/api/ssh/input",
                              {"session_id": sid,
                               "data_b64": base64.b64encode(
                                   b"echo-back\n").decode()})
        self.assertEqual(status, 200)
        # Stream the SSE output until the echo arrives, then stop.
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/ssh/output?session_id=%s&since=0"
            % (self.port, sid))
        seen = b""
        with urllib.request.urlopen(req, timeout=10) as r:
            while True:
                line = r.readline()
                if not line:
                    break
                if line.startswith(b"data: ") and b"[DONE]" not in line:
                    try:
                        chunk = json.loads(line[6:].decode("utf-8"))
                    except ValueError:
                        continue
                    if chunk.get("data_b64"):
                        seen += base64.b64decode(chunk["data_b64"])
                    if b"echo-back" in seen:
                        break
        self.assertIn(b"echo-back", seen)
        status, j = self._req("GET", "/api/ssh/status?session_id=" + sid)
        self.assertEqual(status, 200)
        self.assertTrue(j["alive"])
        status, j = self._req("POST", "/api/ssh/resize",
                              {"session_id": sid, "cols": 100, "rows": 30})
        self.assertEqual(status, 200)
        status, j = self._req("POST", "/api/ssh/disconnect",
                              {"session_id": sid})
        self.assertEqual(status, 200)
        status, j = self._req("GET", "/api/ssh/status?session_id=" + sid)
        self.assertEqual(status, 404)

    def test_assets_allowlist(self):
        for name in ("xterm.js", "xterm.css", "addon-fit.js"):
            req = urllib.request.Request(
                "http://127.0.0.1:%d/assets/%s" % (self.port, name))
            with urllib.request.urlopen(req, timeout=10) as r:
                self.assertEqual(r.status, 200)
                self.assertTrue(len(r.read()) > 100)
        for bad in ("web.py", "../web.py", "..%2Fweb.py", "evil.js"):
            req = urllib.request.Request(
                "http://127.0.0.1:%d/assets/%s" % (self.port, bad))
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(req, timeout=10)


class PipeSessionTest(unittest.TestCase):
    """0.0.1r: the Windows pipe session — same interface as the
    POSIX PTY session, but no local PTY and no console, so no
    window can ever appear. It runs on Linux too (Popen +
    pipes), which is what makes it testable here."""

    def test_spawn_write_read_disconnect(self):
        sess = _PipeSession("pipe-test-1", ["cat"], 80, 24)
        try:
            self.assertEqual(sess.session_type, "pipe")
            self.assertTrue(sess.alive())
            self.assertIsInstance(sess.pid, int)
            self.assertGreater(sess.pid, 0)
            sess.write(b"hello-pipe\n")
            deadline = time.time() + 5
            got = b""
            while time.time() < deadline:
                data, _off = sess.read_since(0)
                got += data
                if b"hello-pipe" in got:
                    break
                time.sleep(0.1)
            self.assertIn(b"hello-pipe", got)
            self.assertGreater(sess.buffered, 0)
            sess.resize(120, 40)
            self.assertEqual(sess.cols, 120)
        finally:
            sess.close()
        self.assertFalse(sess.alive())

    def test_argv_forces_pty_without_local_tty(self):
        # 0.0.1s: r used a single -t and the laptop's Windows
        # OpenSSH refused the PTY ("stdin is not a terminal") with
        # piped stdio, so the HF gateway closed the session. -tt
        # is the form that forces allocation with no local tty,
        # and it must sit before the host token — after it, ssh
        # would read it as the remote command instead.
        argv = sshterm.default_ssh_argv(
            "chat.hf.co", 22, "", None, "/tmp/known_hosts_test")
        self.assertIn("-tt", argv)
        self.assertNotIn("-t", argv)
        self.assertLess(argv.index("-tt"), argv.index("chat.hf.co"))
        self.assertEqual(argv[-1], "chat.hf.co")

    def test_pipe_creationflags_console_decision(self):
        # 0.0.1t: with a parent console, the child inherits it so
        # Windows OpenSSH can read a real PTY size (no new window:
        # stdio stays piped); with no console, CREATE_NO_WINDOW
        # is kept so no window can flash into existence.
        self.assertEqual(sshterm._pipe_creationflags(True), 0)
        self.assertEqual(sshterm._pipe_creationflags(False), 0x08000000)

    def test_pipe_env_sets_size(self):
        # 0.0.1t: the pipe child carries TERM plus COLUMNS/LINES
        # from the session size, for TUIs that consult env.
        env = sshterm._pipe_env(200, 50)
        self.assertEqual(env["TERM"], "xterm-256color")
        self.assertEqual(env["COLUMNS"], "200")
        self.assertEqual(env["LINES"], "50")
    """0.0.1o: status() must expose what the laptop screen needs to
    prove where output goes — session type, child pid, and bytes
    buffered. A blank pane with alive=True and buffered=0 means the
    child never fed the pipes; buffered>0 with a blank pane means
    the delivery path (SSE/xterm) is the fault, not the PTY."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = SshManager(self.tmp.name,
                              command_builder=lambda *a: ["cat"])

    def tearDown(self):
        self.mgr.shutdown()
        self.tmp.cleanup()

    def test_status_carries_type_pid_buffered(self):
        sid = self.mgr.connect("example.com")
        self.mgr.write(sid, b"diag-bytes\n")
        deadline = time.time() + 5
        st = None
        while time.time() < deadline:
            st = self.mgr.status(sid)
            if st["buffered"] > 0:
                break
            time.sleep(0.1)
        self.assertEqual(st["session_type"], "posix")
        self.assertIsInstance(st["pid"], int)
        self.assertGreater(st["pid"], 0)
        self.assertGreater(st["buffered"], 0)


class ConPtyFirstTest(unittest.TestCase):
    """0.0.1u: ConPTY first on Windows with a pipe fallback,
    and a size clamp so a measured 0x0 can never reach a
    session. The ConPTY branch itself is win32-only and
    cannot run on Linux, so these tests drive the factory
    through monkeypatched session classes plus the real
    clamp — they prove the ordering and the fallback, not
    that a Windows pseudo console starts headlessly. That
    proof is tools/conpty_selftest.py on the laptop."""

    def _patch(self, name, value):
        old = getattr(sshterm, name, None)
        had = hasattr(sshterm, name)
        setattr(sshterm, name, value)
        return old, had

    def _restore(self, name, old, had):
        if had:
            setattr(sshterm, name, old)
        else:
            try:
                delattr(sshterm, name)
            except AttributeError:
                pass

    def test_clamp_size(self):
        self.assertEqual(sshterm._clamp_size(0, 0), (80, 24))
        self.assertEqual(sshterm._clamp_size(1, 1), (10, 5))
        self.assertEqual(sshterm._clamp_size(120, 40), (120, 40))
        self.assertEqual(sshterm._clamp_size(9999, 999), (500, 200))
        self.assertEqual(sshterm._clamp_size("bad", None), (80, 24))
        self.assertEqual(sshterm._clamp_size(-5, -5), (80, 24))

    def test_conpty_first_and_clamped(self):
        seen = {}

        class FakeConPty:
            session_type = "conpty"

            def __init__(self, sid, argv, cols, rows):
                seen.update(sid=sid, argv=argv, cols=cols, rows=rows)

        old, had = self._patch("_ConPtySession", FakeConPty)
        try:
            sess = sshterm._make_windows_session("s1", ["cat"], 0, 0)
        finally:
            self._restore("_ConPtySession", old, had)
        self.assertIsInstance(sess, FakeConPty)
        # 0x0 in, clamped 80x24 out — never a 0-size console.
        self.assertEqual((seen["cols"], seen["rows"]), (80, 24))

    def test_fallback_on_conpty_failure(self):
        class FailingConPty:
            def __init__(self, *a):
                raise RuntimeError("ConPTY unavailable")

        class FakePipe:
            session_type = "pipe"

            def __init__(self, sid, argv, cols, rows):
                self.cols, self.rows = cols, rows

        old_c, had_c = self._patch("_ConPtySession", FailingConPty)
        old_p, had_p = self._patch("_PipeSession", FakePipe)
        try:
            sess = sshterm._make_windows_session("s2", ["cat"], 100, 30)
        finally:
            self._restore("_ConPtySession", old_c, had_c)
            self._restore("_PipeSession", old_p, had_p)
        self.assertIsInstance(sess, FakePipe)
        self.assertEqual((sess.cols, sess.rows), (100, 30))

    def test_manager_connect_and_resize_clamp(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            mgr = SshManager(tmp.name,
                             command_builder=lambda *a: ["cat"])
            try:
                sid = mgr.connect("example.com", cols=0, rows=0)
                st = mgr.status(sid)
                self.assertEqual((st["cols"], st["rows"]), (80, 24))
                mgr.resize(sid, 0, 0)
                st = mgr.status(sid)
                self.assertEqual((st["cols"], st["rows"]), (80, 24))
                mgr.resize(sid, 9999, 999)
                st = mgr.status(sid)
                self.assertEqual((st["cols"], st["rows"]), (500, 200))
            finally:
                mgr.shutdown()
        finally:
            tmp.cleanup()

    def test_headless_flags_in_source(self):
        root = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        with open(os.path.join(root, "hub", "sshterm.py"),
                  encoding="utf-8") as f:
            src = f.read()
        self.assertIn("_STARTF_USESHOWWINDOW = 0x00000001", src)
        self.assertIn("_SW_HIDE = 0", src)
        self.assertIn("dwFlags = _STARTF_USESHOWWINDOW", src)
        self.assertIn("wShowWindow = _SW_HIDE", src)
        # 0.0.1v: the ConPTY CreateProcessW call must NOT
        # carry CREATE_NO_WINDOW — u showed that it gives
        # a conpty pid with empty pipes, because the
        # reserved no-window ConsoleHandle overrides the
        # pseudo-console reference. The pipe session (a
        # different call site) still uses it legitimately,
        # so scope the check to the ConPTY call: the text
        # between "class _ConPtySession" and "_make_windows_session".
        conpty = src[src.index("class _ConPtySession"):
                      src.index("def _make_windows_session")]
        self.assertNotIn("_CREATE_NO_WINDOW", conpty)
        # v also matches Microsoft's sample in two more
        # ways u did not: bInheritHandles=True, and a
        # mutable command-line buffer for CreateProcessW.
        self.assertIn("None, cmdline, None, None, True,", conpty)
        self.assertIn("create_unicode_buffer", conpty)
        # And the parent closes the console-owned pipe
        # ends after CreateProcessW, as the sample does.
        self.assertIn("for _fd in (in_r, out_w):", conpty)
        # SSE client disconnects must not print a
        # traceback: WinError 10053 is ConnectionAbortedError.
        with open(os.path.join(root, "hub", "web.py"),
                  encoding="utf-8") as f:
            web_src = f.read()
        self.assertIn("ConnectionAbortedError", web_src)


class WindowsImportSafetyTest(unittest.TestCase):
    """0.0.1m regression: sshterm imported fcntl/pty/termios at module
    level, which killed the whole web UI on native Windows
    (ModuleNotFoundError: fcntl). Simulates that platform in a
    subprocess: the module must still import, and connect must fail
    cleanly with a RuntimeError — never at import time."""

    def test_blocked_unix_imports(self):
        root = os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))
        code = (
            "import importlib.abc, sys, tempfile\n"
            "class Blocker(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name in ('fcntl', 'pty', 'termios'):\n"
            "            raise ImportError('blocked: ' + name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, Blocker())\n"
            "from hub import sshterm\n"
            "assert sshterm._HAVE_POSIX_PTY is False\n"
            "mgr = sshterm.SshManager(tempfile.mkdtemp(),\n"
            "    command_builder=lambda *a: ['cat'])\n"
            "try:\n"
            "    mgr.connect('example.com')\n"
            "    raise SystemExit('connect should have failed')\n"
            "except RuntimeError as e:\n"
            "    assert 'PTY unavailable' in str(e), str(e)\n"
            "print('import-safety-ok')\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = root
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=60,
                           env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("import-safety-ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
