"""Session archive/delete helpers: archive flag round-trips, delete
removes, and bad ids are refused before any path is built.

Runs against a throwaway HUB_DATA_DIR so the real web-sessions/
directory is never touched.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.web import (
    _delete_session_file,
    _session_file,
    _set_session_archived,
    sessions_dir,
)


def _seed(sid, title="Test chat"):
    pth = os.path.join(sessions_dir(), sid + ".json")
    with open(pth, "w", encoding="utf-8") as f:
        json.dump({"id": sid, "title": title, "messages": [{"role": "user", "content": "hi"}],
                   "type": "CHAT"}, f, indent=2)
    return pth


class SessionFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("HUB_DATA_DIR")
        os.environ["HUB_DATA_DIR"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("HUB_DATA_DIR", None)
        else:
            os.environ["HUB_DATA_DIR"] = self._old
        self._tmp.cleanup()

    def test_archive_sets_flag_and_preserves_rest(self):
        pth = _seed("chat-test1")
        rec = _set_session_archived("chat-test1", True)
        self.assertTrue(rec["archived"])
        self.assertEqual(rec["title"], "Test chat")
        self.assertEqual(len(rec["messages"]), 1)
        with open(pth, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["archived"])

    def test_unarchive_clears_flag(self):
        _seed("chat-test1")
        _set_session_archived("chat-test1", True)
        rec = _set_session_archived("chat-test1", False)
        self.assertFalse(rec["archived"])

    def test_delete_removes_and_missing_raises(self):
        pth = _seed("chat-test1")
        _delete_session_file("chat-test1")
        self.assertFalse(os.path.exists(pth))
        with self.assertRaises(OSError):
            _delete_session_file("chat-test1")

    def test_bad_ids_refused(self):
        for bad in ("", "../escape", "a/b", "a b", "chat..x"):
            with self.assertRaises(ValueError, msg=bad):
                _session_file(bad)
            with self.assertRaises(ValueError, msg=bad):
                _set_session_archived(bad, True)
            with self.assertRaises(ValueError, msg=bad):
                _delete_session_file(bad)

    def test_archive_missing_session_raises(self):
        with self.assertRaises(OSError):
            _set_session_archived("chat-nope", True)


if __name__ == "__main__":
    unittest.main()
