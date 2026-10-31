"""Tests for 0.0.1i: transfers tracking, serving defaults, chat
packages, and serving-aware text dispatch. No network: transfers and
serving run against temp data dirs, the package test signs locally,
and text dispatch backends are stubbed."""

import json
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import node as node_mod  # noqa: E402
import tunnel as tunnel_mod  # noqa: E402
from hub import nodeapi  # noqa: E402
from hub import package as pkg_mod  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + ((" — " + detail) if detail else ""))


def main():
    tmp = tempfile.mkdtemp(prefix="node-i-")
    try:
        # ---- transfers: record -> done/failed -> list -> resend ----
        row = nodeapi.record_transfer("job-1", "ab" * 33, "tts",
                                      {"text": "hello voice", "voice": "mya-03"},
                                      data_dir=tmp)
        check("i: transfer recorded as sent",
              row and row["status"] == "sent" and row["voice"] == "mya-03")
        nodeapi.update_transfer("job-1", {"audio_b64": "AAAA"}, data_dir=tmp)
        nodeapi.record_transfer("job-2", "cd" * 33, "text",
                                {"text": "hello text"}, data_dir=tmp)
        nodeapi.update_transfer("job-2", {"error": "peer denied"}, data_dir=tmp)
        lst = nodeapi.list_transfers(data_dir=tmp)["transfers"]
        check("i: transfers list newest first, done + failed",
              len(lst) == 2 and lst[0]["job_id"] == "job-2"
              and lst[0]["status"] == "failed"
              and lst[1]["status"] == "done"
              and lst[1]["reply_kind"] == "audio", str(lst))
        real_send = nodeapi.send_job

        def fake_send(to, cap, payload, data_dir=None):
            nodeapi.record_transfer("job-3", to, cap, payload, data_dir=data_dir)
            return {"ok": True, "job_id": "job-3", "status": "queued"}
        nodeapi.send_job = fake_send
        try:
            out = nodeapi.resend_transfer("job-2", data_dir=tmp)
        finally:
            nodeapi.send_job = real_send
        rows = nodeapi.list_transfers(data_dir=tmp)["transfers"]
        old = next((r for r in rows if r["job_id"] == "job-2"), None)
        check("i: resend creates a new tracked job, old marked resent",
              out.get("job_id") == "job-3" and old and old["status"] == "resent"
              and any(r["job_id"] == "job-3" for r in rows), str(out))
        check("i: resend of unknown transfer errors",
              "error" in nodeapi.resend_transfer("nope", data_dir=tmp))

        # ---- serving: validation, redaction, key lifecycle ----
        check("i: serving rejects a bad backend",
              "error" in nodeapi.set_serving({"text_backend": "warp"}, data_dir=tmp))
        check("i: serving cloud backend needs a model",
              "error" in nodeapi.set_serving(
                  {"text_backend": "openrouter"}, data_dir=tmp))
        check("i: serving endpoint must be https",
              "error" in nodeapi.set_serving(
                  {"text_backend": "llama", "public_endpoint": "http://x"},
                  data_dir=tmp))
        out = nodeapi.set_serving(
            {"text_backend": "openrouter", "text_model": "m/free",
             "cloud_key": "sk-test-key", "tts_voice": "mya-03",
             "public_endpoint": "https://tun.example.com"}, data_dir=tmp)
        serving_path = os.path.join(tmp, "serving.json")
        mode = stat.S_IMODE(os.stat(serving_path).st_mode)
        got = nodeapi.get_serving(data_dir=tmp)
        check("i: serving cloud config stored 0600, key redacted",
              out.get("ok") and mode == 0o600
              and got.get("has_cloud_key") is True
              and "cloud_key" not in got
              and got.get("public_endpoint") == "https://tun.example.com",
              "mode=%o got=%s" % (mode, got))
        out = nodeapi.set_serving({"text_backend": "llama"}, data_dir=tmp)
        got = nodeapi.get_serving(data_dir=tmp)
        check("i: switching to local drops the stored cloud key",
              got.get("has_cloud_key") is False
              and "cloud_key" not in json.load(open(serving_path)))

        # ---- chat packages: sign, verify, tamper fails ----
        from hub.identity import ensure_identity
        ident, _ = ensure_identity(os.path.join(tmp, "ident"))
        session = {"id": "s1", "title": "test chat", "saved_at": "2026-10-30",
                   "messages": [{"role": "user", "content": "hi"}]}
        package = pkg_mod.create_package(ident, session)
        ver = pkg_mod.verify_package(package)
        check("i: signed chat package verifies",
              ver.get("valid") is True, str(ver))
        bad = json.loads(json.dumps(package))
        bad["payload"]["session"]["messages"][0]["content"] = "forged"
        ver = pkg_mod.verify_package(bad)
        check("i: tampered chat package fails verification",
              ver.get("valid") is False, str(ver))

        # ---- text dispatch honors serving.json ----
        # Fresh dir: the serving tests above left a serving.json in tmp.
        tmp2 = os.path.join(tmp, "dispatch")
        os.makedirs(tmp2, exist_ok=True)
        real_llama = tunnel_mod.llama_text
        real_ollama = tunnel_mod.ollama_text
        tunnel_mod.llama_text = lambda text, url=None: {"text": "llama:" + text}
        tunnel_mod.ollama_text = lambda text, model=None, url=None: {"text": "ollama:" + text}
        try:
            out = tunnel_mod.text_dispatch(tmp2, "ollama", None, {"text": "q"})
            check("i: dispatch falls back to CLI backend without serving.json",
                  out.get("text") == "ollama:q", str(out))
            with open(os.path.join(tmp2, "serving.json"), "w") as f:
                json.dump({"text_backend": "llama"}, f)
            out = tunnel_mod.text_dispatch(tmp2, "ollama", None, {"text": "q"})
            check("i: dispatch honors serving.json llama backend",
                  out.get("text") == "llama:q", str(out))
            with open(os.path.join(tmp2, "serving.json"), "w") as f:
                json.dump({"text_backend": "openrouter", "text_model": "m/free"}, f)
            out = tunnel_mod.text_dispatch(tmp2, "ollama", None, {"text": "q"})
            check("i: dispatch fails loud on cloud backend without a key",
                  "key" in (out.get("error") or ""), str(out))
        finally:
            tunnel_mod.llama_text = real_llama
            tunnel_mod.ollama_text = real_ollama
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
