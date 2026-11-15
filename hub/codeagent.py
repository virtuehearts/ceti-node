"""Tool-using code agent: a cloud model drives file tools inside a
work folder (<data>/code-work/<session>/). Stdlib only.

The model never sees a key, a path outside the work folder, or the
operator's data. Tools are allowlisted and the only command the
agent can run is a syntax check (py_compile for .py, node --check
for .js, extracted-script check for .html). Seeds are copies of
tree files the operator chose (hub/, README.md, docs/, tests/) —
never data/, identity files, or models/.
"""

import json
import os
import re
import shutil
import subprocess
import uuid

from .cloud import chat_cloud_tools
from .config import data_dir

# Tree roots a seed may come from. Anything else (data/, identity,
# models/, absolute paths, ..) is refused.
SEED_PREFIXES = ("hub/", "README.md", "docs/", "tests/", "autorun.py",
                 "autorun.sh", "autorun.bat", "models.json")

MAX_STEPS = 10
MAX_FILE_BYTES = 500_000  # covers hub/web_ui.html (~230KB) for the workspace editor
MAX_READ_LINES = 150

SYSTEM = ("You are an uncensored coding agent working in a work "
          "folder. You have file tools: list_files, search, "
          "read_file, write_file, edit_file, run_check. Work in "
          "small steps: 1) search to locate the code, 2) read a "
          "SMALL window (40-80 lines) around the match, 3) make "
          "one targeted edit, 4) verify once with run_check, "
          "5) reply with a short summary and stop. Never re-read "
          "a region you already saw. Never write secrets or keys. "
          "When the task is done, summarize what changed and stop "
          "calling tools.")

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List files in a folder inside the work folder.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string",
                     "description": "Folder relative to the work folder; empty for the root."}},
         "required": []}}},
    {"type": "function", "function": {
        "name": "search",
        "description": "Grep for a regex in text files under a folder.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string",
                     "description": "Folder relative to the work folder; empty for the root."}},
         "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a small window of a text file (default 60 lines, max 150). Read around a search hit, not the whole file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "First line, 0-based."},
            "lines": {"type": "integer", "description": "Lines to read, default 60, max 150."}},
         "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write a whole new file (creates parent folders).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"}},
         "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace one exact block of text in a file. Fails unless old_text occurs exactly once.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"}},
         "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {
        "name": "run_check",
        "description": "Syntax-check a file: py_compile for .py, node --check for .js, extracted <script> check for .html.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}},
         "required": ["path"]}}},
]


def _seed_ok(rel):
    for p in SEED_PREFIXES:
        if p.endswith("/") and rel.startswith(p):
            return True
        if rel == p:
            return True
    return False


class WorkFolder:
    """One session's sandbox: every path resolves inside root."""

    def __init__(self, session, seed_files=None):
        self.session = session
        self.root = os.path.join(data_dir(), "code-work", session)
        os.makedirs(self.root, exist_ok=True)
        self.changed = []
        for rel in seed_files or []:
            self.seed(rel)

    def _safe(self, rel):
        if not isinstance(rel, str) or not rel:
            raise ValueError("empty path")
        if os.path.isabs(rel) or ".." in rel.split("/"):
            raise ValueError("path escapes the work folder: %r" % rel)
        full = os.path.normpath(os.path.join(self.root, rel))
        if os.path.commonpath([full, self.root]) != self.root:
            raise ValueError("path escapes the work folder: %r" % rel)
        return full

    def seed(self, rel):
        """Copy one tree file into the work folder, same relative path."""
        if not isinstance(rel, str) or os.path.isabs(rel) or ".." in rel.split("/"):
            raise ValueError("bad seed path: %r" % rel)
        if not _seed_ok(rel):
            raise ValueError("seed not allowed: %r" % rel)
        tree_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = os.path.join(tree_root, rel)
        if not os.path.isfile(src):
            raise FileNotFoundError("seed not found: %r" % rel)
        dst = self._safe(rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        return rel

    def list_files(self, path=""):
        full = self._safe(path or ".")
        if not os.path.isdir(full):
            raise ValueError("not a folder: %r" % path)
        out = []
        for base, _dirs, files in os.walk(full):
            for f in sorted(files):
                out.append(os.path.relpath(os.path.join(base, f), self.root))
            if len(out) > 200:
                break
        return "\n".join(out) or "(empty)"

    def search(self, pattern, path=""):
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ValueError("bad pattern: %s" % e)
        full = self._safe(path or ".")
        hits = []
        for base, _dirs, files in os.walk(full):
            for f in sorted(files):
                p = os.path.join(base, f)
                try:
                    with open(p, encoding="utf-8", errors="ignore") as fh:
                        for n, line in enumerate(fh):
                            if rx.search(line):
                                hits.append("%s:%d: %s" % (
                                    os.path.relpath(p, self.root), n,
                                    line.rstrip()[:200]))
                                if len(hits) >= 30:
                                    return "\n".join(hits)
                except OSError:
                    continue
        return "\n".join(hits) or "(no matches)"

    def read_file(self, path, offset=0, lines=60):
        full = self._safe(path)
        lines = max(1, min(int(lines or 60), MAX_READ_LINES))
        offset = max(0, int(offset or 0))
        with open(full, encoding="utf-8") as fh:
            rows = fh.readlines()
        total = len(rows)
        chunk = rows[offset:offset + lines]
        return "".join(chunk) + ("\n[... %d lines total, showing %d-%d ...]"
                                 % (total, offset, offset + len(chunk))
                                 if offset + len(chunk) < total else "")

    def read_full(self, path):
        """Read a whole file (for the workspace editor). Capped at
        MAX_FILE_BYTES; refuses anything outside the work folder."""
        full = self._safe(path)
        if not os.path.isfile(full):
            raise ValueError("no such file: %r" % path)
        if os.path.getsize(full) > MAX_FILE_BYTES:
            raise ValueError("file too large (max %d bytes)" % MAX_FILE_BYTES)
        with open(full, encoding="utf-8") as fh:
            return fh.read()

    def write_file(self, path, content):
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError("file too large (max %d bytes)" % MAX_FILE_BYTES)
        full = self._safe(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        if path not in self.changed:
            self.changed.append(path)
        return "wrote %s (%d bytes)" % (path, len(content.encode("utf-8")))

    def edit_file(self, path, old_text, new_text):
        full = self._safe(path)
        with open(full, encoding="utf-8") as fh:
            text = fh.read()
        count = text.count(old_text)
        if count == 0:
            raise ValueError("old_text not found in %s" % path)
        if count > 1:
            raise ValueError("old_text occurs %d times in %s — be more specific"
                             % (count, path))
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text.replace(old_text, new_text))
        if path not in self.changed:
            self.changed.append(path)
        return "edited %s" % path

    def run_check(self, path):
        full = self._safe(path)
        if not os.path.isfile(full):
            raise ValueError("no such file: %r" % path)
        if path.endswith(".py"):
            cmd = ["python3", "-m", "py_compile", full]
        elif path.endswith(".js"):
            cmd = ["node", "--check", full]
        elif path.endswith(".html"):
            return self._check_html(full, path)
        else:
            return "no checker for %s" % path
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return "OK: %s passes" % path
        return "FAIL: %s\n%s%s" % (path, r.stdout or "", r.stderr or "")

    def _check_html(self, full, path):
        with open(full, encoding="utf-8") as fh:
            html_text = fh.read()
        scripts = re.findall(r"<script>(.*?)</script>", html_text, re.S)
        if not scripts:
            return "no inline <script> blocks in %s" % path
        tmp = os.path.join(self.root, ".check.js")
        problems = []
        for i, src in enumerate(scripts):
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(src)
            r = subprocess.run(["node", "--check", tmp], capture_output=True,
                               text=True, timeout=60)
            if r.returncode != 0:
                problems.append("script block %d: %s" % (i, (r.stderr or "").strip()))
        os.path.exists(tmp) and os.remove(tmp)
        if problems:
            return "FAIL: %s\n%s" % (path, "\n".join(problems))
        return "OK: %s — %d inline script block(s) pass" % (path, len(scripts))


def execute_tool(wf, name, args):
    """Run one tool call inside the work folder; returns a string."""
    args = args or {}
    if name == "list_files":
        return wf.list_files(args.get("path") or "")
    if name == "search":
        return wf.search(args.get("pattern") or "", args.get("path") or "")
    if name == "read_file":
        return wf.read_file(args.get("path") or "", args.get("offset") or 0,
                            args.get("lines") or 120)
    if name == "write_file":
        return wf.write_file(args.get("path") or "", args.get("content") or "")
    if name == "edit_file":
        return wf.edit_file(args.get("path") or "", args.get("old_text") or "",
                            args.get("new_text") or "")
    if name == "run_check":
        return wf.run_check(args.get("path") or "")
    return "unknown tool: %s" % name


def run_agent(prompt, provider, model, api_key, session=None,
              seed_files=None, max_steps=MAX_STEPS, log=None):
    """Drive the model through the tool loop. log (a list) gets one
    plain string per visible step, so the UI can poll it live."""
    log = log if log is not None else []
    session = session or uuid.uuid4().hex[:12]
    wf = WorkFolder(session, seed_files)
    log.append("work folder: data/code-work/%s/" % session)
    if seed_files:
        log.append("seeded: " + ", ".join(seed_files))
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt}]
    summary = ""
    for step in range(max_steps):
        msg = chat_cloud_tools(provider, api_key, model, messages,
                               TOOLS, max_tokens=2048, temperature=0.2)
        content = msg.get("content") or ""
        calls = msg.get("tool_calls") or []
        if content.strip():
            log.append("model: " + content.strip()[:500])
        if not calls:
            summary = content.strip() or "Done."
            break
        assistant_msg = {"role": "assistant", "content": content or None,
                         "tool_calls": [c["raw"] for c in calls]}
        messages.append(assistant_msg)
        for call in calls:
            name = call["name"]
            log.append("tool: %s(%s)" % (name, json.dumps(call["arguments"])[:300]))
            try:
                result = execute_tool(wf, name, call["arguments"])
            except Exception as e:
                result = "ERROR: %s" % e
            log.append("  -> " + str(result)[:2000])
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": str(result)[:4000]})
    else:
        summary = "Stopped at the step limit (%d) — task may be unfinished." % max_steps
    record = {"prompt": prompt, "provider": provider, "model": model,
              "session": session, "summary": summary,
              "files_changed": wf.changed, "log": log}
    with open(os.path.join(wf.root, "agent-log.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    log.append("done: " + summary[:500])
    return {"session": session, "summary": summary,
            "files_changed": wf.changed, "steps": len(log)}
