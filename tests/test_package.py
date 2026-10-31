"""Signed chat package tests — stdlib unittest only, temp dirs only.

Covers hub/package.py: sign -> verify valid; tampered content, tampered
signature, wrong public key, and spoofed fingerprint all fail; the
signature is deterministic; no secret ever appears in a package.
Never touches the real data/ directory (fresh temp identity per test).
"""
import json
import os
import tempfile
import unittest

from hub.identity import ensure_identity
from hub.package import SCHEME, create_package, verify_package, verify_signature


def _session():
    return {
        "id": "chat-20261026-test",
        "title": "test chat",
        "saved_at": "2026-10-26 10:00",
        "messages": [
            {"role": "user", "content": "hello node"},
            {"role": "assistant", "content": "hello back"},
        ],
    }


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ident, _ = ensure_identity(self.tmp.name)
        self.other, _ = ensure_identity(tempfile.mkdtemp())

    def tearDown(self):
        self.tmp.cleanup()

    def test_sign_then_verify_valid(self):
        pkg = create_package(self.ident, _session())
        self.assertEqual(pkg["format"], "hub-chat-package")
        self.assertEqual(pkg["version"], 1)
        self.assertEqual(pkg["scheme"], SCHEME)
        self.assertEqual(pkg["signer"]["fingerprint"], self.ident.fingerprint)
        self.assertEqual(pkg["signer"]["node_id"], self.ident.node_id)
        self.assertEqual(pkg["signer"]["public_key"], self.ident.public_hex())
        result = verify_package(pkg)
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["fingerprint"], self.ident.fingerprint)

    def test_signature_is_deterministic(self):
        a = create_package(self.ident, _session())
        b = create_package(self.ident, _session())
        # Same session content, same identity: same canonical payload, but
        # exported_at differs per second — so compare on a fixed payload.
        self.assertEqual(a["payload"]["session"], b["payload"]["session"])
        a["payload"]["exported_at"] = b["payload"]["exported_at"] = "2026-10-26T00:00:00Z"
        from hub.package import canonical, sign_payload
        unsigned_a = {k: v for k, v in a.items() if k != "signature"}
        unsigned_b = {k: v for k, v in b.items() if k != "signature"}
        self.assertEqual(sign_payload(self.ident.secret, canonical(unsigned_a)),
                         sign_payload(self.ident.secret, canonical(unsigned_b)))

    def test_tampered_message_fails(self):
        pkg = create_package(self.ident, _session())
        pkg["payload"]["session"]["messages"][1]["content"] = "forged reply"
        result = verify_package(pkg)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "bad signature")

    def test_tampered_title_fails(self):
        pkg = create_package(self.ident, _session())
        pkg["payload"]["session"]["title"] = "forged title"
        self.assertFalse(verify_package(pkg)["valid"])

    def test_tampered_signature_fails(self):
        pkg = create_package(self.ident, _session())
        sig = pkg["signature"]
        # Flip one hex digit inside the DER body (not the tag/length).
        flipped = sig[:10] + ("0" if sig[10] != "0" else "1") + sig[11:]
        pkg["signature"] = flipped
        self.assertFalse(verify_package(pkg)["valid"])

    def test_wrong_public_key_fails(self):
        pkg = create_package(self.ident, _session())
        # Pin verification to a different node's key: must fail.
        result = verify_package(pkg, self.other.public_hex())
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "signer key mismatch")
        # And a bare signature check under the wrong key fails too.
        from hub.package import canonical
        unsigned = {k: v for k, v in pkg.items() if k != "signature"}
        self.assertFalse(verify_signature(self.other.public_hex(),
                                          canonical(unsigned), pkg["signature"]))

    def test_spoofed_fingerprint_fails(self):
        pkg = create_package(self.ident, _session())
        pkg["signer"]["fingerprint"] = self.other.fingerprint
        result = verify_package(pkg)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "fingerprint mismatch")

    def test_malformed_packages_fail_softly(self):
        for bad in (None, "nope", {}, {"format": "hub-chat-package"},
                    {"format": "hub-chat-package", "version": 99, "scheme": SCHEME}):
            result = verify_package(bad)
            self.assertFalse(result["valid"], bad)

    def test_no_secret_in_package(self):
        pkg = create_package(self.ident, _session())
        blob = json.dumps(pkg)
        self.assertNotIn(self.ident.secret.hex(), blob)
        self.assertNotIn("secret", blob)
        self.assertNotIn("private", blob)

    def test_package_json_round_trips(self):
        pkg = create_package(self.ident, _session())
        reloaded = json.loads(json.dumps(pkg))
        self.assertTrue(verify_package(reloaded)["valid"])


if __name__ == "__main__":
    unittest.main()
