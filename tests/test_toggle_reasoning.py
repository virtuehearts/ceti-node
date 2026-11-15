"""Tests for the Thinking toggle's per-request reasoning control.

The UI's "Thinking" switch now sends thinking=false in the /api/chat
body; the Hub passes it to the llama-server as
chat_template_kwargs {"enable_thinking": false} so the model answers
directly instead of burning its output budget thinking. These tests
assert the request payload carries the control when reasoning is off
and omits it (reasoning on) by default — no live server, no network:
urllib is stubbed and the built Request is inspected.
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import servers


class _FakeResp:
    """Stateful stand-in for a llama-server response.

    Returns one payload then EOF, so both the non-streaming
    (json.load) and streaming (read-until-empty) callers terminate.
    """

    def __init__(self, payload):
        self._payload = payload
        self._sent = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *a):
        if self._sent:
            return b""
        self._sent = True
        return self._payload


def _capture_request(fn, *args, **kwargs):
    """Run fn with urllib stubbed; return the parsed request body."""
    seen = {}

    class _Opener:
        def open(self, req, timeout=None):
            body = json.loads(req.data.decode("utf-8"))
            seen["body"] = body
            seen["url"] = req.full_url
            if body.get("stream"):
                return _FakeResp(b"data: [DONE]\n")
            return _FakeResp(json.dumps({
                "choices": [{"message": {"content": "hello",
                                         "reasoning_content": ""}}],
            }).encode("utf-8"))

    with patch.object(servers.urllib.request, "build_opener",
                      return_value=_Opener()):
        result = fn(*args, **kwargs)
        # Generators only execute when iterated; tuples return directly.
        if hasattr(result, "__next__"):
            list(result)
    return seen


class ReasoningPayloadTest(unittest.TestCase):
    def test_chat_complete_default_omits_control(self):
        seen = _capture_request(servers.chat_complete,
                                [{"role": "user", "content": "hi"}],
                                port=11440, max_tokens=10)
        self.assertNotIn("chat_template_kwargs", seen["body"])
        self.assertEqual(seen["url"],
                         "http://127.0.0.1:11440/v1/chat/completions")

    def test_chat_complete_reasoning_true_omits_control(self):
        seen = _capture_request(servers.chat_complete,
                                [{"role": "user", "content": "hi"}],
                                port=11440, max_tokens=10, reasoning=True)
        self.assertNotIn("chat_template_kwargs", seen["body"])

    def test_chat_complete_reasoning_false_disables_thinking(self):
        seen = _capture_request(servers.chat_complete,
                                [{"role": "user", "content": "hi"}],
                                port=11440, max_tokens=10, reasoning=False)
        self.assertEqual(seen["body"].get("chat_template_kwargs"),
                         {"enable_thinking": False})

    def test_stream_chat_events_reasoning_false_disables_thinking(self):
        seen = _capture_request(servers.stream_chat_events,
                                [{"role": "user", "content": "hi"}],
                                port=11440, max_tokens=10, reasoning=False)
        self.assertEqual(seen["body"].get("chat_template_kwargs"),
                         {"enable_thinking": False})
        self.assertTrue(seen["body"].get("stream"))

    def test_stream_chat_events_default_omits_control(self):
        seen = _capture_request(servers.stream_chat_events,
                                [{"role": "user", "content": "hi"}],
                                port=11440, max_tokens=10)
        self.assertNotIn("chat_template_kwargs", seen["body"])

    def test_stream_chat_reasoning_false_disables_thinking(self):
        seen = _capture_request(servers.stream_chat,
                                [{"role": "user", "content": "hi"}],
                                port=11440, max_tokens=10, reasoning=False)
        self.assertEqual(seen["body"].get("chat_template_kwargs"),
                         {"enable_thinking": False})


if __name__ == "__main__":
    unittest.main()
