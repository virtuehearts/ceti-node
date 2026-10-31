"""Signed chat packages: export a session as a portable, node-signed file.

A package is one JSON object: the session transcript + metadata, the
signer's node id / fingerprint / compressed public key, and one ECDSA
signature. Anyone holding the public key can verify the package later,
offline, without the node.

Signature scheme (reused, not invented): exactly the regula_core scheme
from the Jero node stack (jero/crypto.py) — secp256k1 ECDSA over
double-SHA256, DER-encoded signature — implemented here on top of
hub/identity.py's stdlib curve math, because the Hub ships stdlib-only
(the `ecdsa` package is not installed here) and the signer's secret is
the Hub's own identity.dat secret. Deterministic nonces follow RFC 6979
(HMAC-SHA256), so signing the same payload twice gives the same
signature, and verification accepts any valid (r, s) pair.

Security rules, enforced by construction:
- The private key never appears in a package, a response, a log, or a
  file written by this module. Signing takes the secret in memory only.
- Verification needs nothing but the package itself (its embedded
  public key), plus an optional expected-key pin.
- Export is read-only: this module never writes sessions or identity.
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone

from .identity import N, P, _add, _mul, fingerprint

FORMAT = "hub-chat-package"
VERSION = 1
SCHEME = "ECDSA-secp256k1-SHA256d-DER"


def sha256d(data: bytes) -> bytes:
    """Double SHA-256, as in the regula_core scheme."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def canonical(obj) -> bytes:
    """Canonical JSON bytes for signing: sorted keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _der_int(x: int) -> bytes:
    b = x.to_bytes((x.bit_length() + 7) // 8 or 1, "big")
    if b[0] & 0x80:
        b = b"\x00" + b
    return b"\x02" + bytes([len(b)]) + b


def _der_encode(r: int, s: int) -> bytes:
    body = _der_int(r) + _der_int(s)
    return b"\x30" + bytes([len(body)]) + body


def _der_decode(data: bytes):
    """Parse a DER (r, s) pair. Raises ValueError on any malformation."""
    if len(data) < 2 or data[0] != 0x30:
        raise ValueError("not a DER sequence")
    total = data[1]
    if total & 0x80:
        raise ValueError("long-form DER length not supported")
    if len(data) != 2 + total:
        raise ValueError("trailing bytes in DER signature")
    body = data[2:]
    if not body or body[0] != 0x02:
        raise ValueError("bad DER integer tag")
    l1 = body[1]
    if len(body) < 2 + l1:
        raise ValueError("truncated DER integer")
    r = int.from_bytes(body[2:2 + l1], "big")
    rest = body[2 + l1:]
    if not rest or rest[0] != 0x02:
        raise ValueError("bad DER integer tag")
    l2 = rest[1]
    if len(rest) != 2 + l2:
        raise ValueError("trailing bytes in DER signature")
    s = int.from_bytes(rest[2:2 + l2], "big")
    return r, s


def _decompress_pubkey(comp: bytes):
    """Decompress a 33-byte compressed secp256k1 public key to (x, y)."""
    if len(comp) != 33 or comp[0] not in (2, 3):
        raise ValueError("not a compressed secp256k1 public key")
    x = int.from_bytes(comp[1:], "big")
    if x >= P:
        raise ValueError("public key x out of range")
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if (y * y - y_sq) % P != 0:
        raise ValueError("public key point not on curve")
    if (y & 1) != (comp[0] & 1):
        y = P - y
    return (x, y)


def _rfc6979_k(secret_int: int, digest: bytes) -> int:
    """Deterministic nonce k per RFC 6979 (HMAC-SHA256, bits2octets)."""
    z1 = int.from_bytes(digest, "big") % N
    bx = secret_int.to_bytes(32, "big") + z1.to_bytes(32, "big")
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + bx, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + bx, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign_digest(secret: bytes, digest: bytes) -> str:
    """ECDSA-sign a 32-byte double-SHA256 digest. Returns DER hex."""
    secret_int = int.from_bytes(secret, "big") % N
    if secret_int == 0:
        raise ValueError("invalid secret")
    k = _rfc6979_k(secret_int, digest)
    r_point = _mul(k)
    r = r_point[0] % N
    if r == 0:
        raise ValueError("degenerate nonce")
    z = int.from_bytes(digest, "big")
    s = (pow(k, -1, N) * (z + r * secret_int)) % N
    if s == 0:
        raise ValueError("degenerate signature")
    if s > N // 2:
        s = N - s  # low-S canonical form
    return _der_encode(r, s).hex()


def sign_payload(secret: bytes, payload: bytes) -> str:
    """Sign payload bytes: ECDSA over double-SHA256. Returns DER hex."""
    return sign_digest(secret, sha256d(payload))


def verify_signature(pubkey_hex: str, payload: bytes, signature_hex: str) -> bool:
    """Verify a DER-hex ECDSA signature over double-SHA256(payload).

    pubkey_hex is the 33-byte compressed public key, hex-encoded.
    Returns False (never raises) on any malformation or mismatch.
    """
    try:
        raw = bytes.fromhex(pubkey_hex)
        point = _decompress_pubkey(raw)
        r, s = _der_decode(bytes.fromhex(signature_hex))
        if not (1 <= r < N and 1 <= s < N):
            return False
        digest = sha256d(payload)
        z = int.from_bytes(digest, "big")
        w = pow(s, -1, N)
        u1 = (z * w) % N
        u2 = (r * w) % N
        point_sum = _add(_mul(u1), _mul(u2, point))
        if point_sum is None:
            return False
        return point_sum[0] % N == r
    except Exception:
        return False


def create_package(identity, session: dict) -> dict:
    """Build a signed package for one session dict.

    identity is a hub.identity.Identity (secret stays in memory only).
    session is the stored session object ({id, title, saved_at,
    messages}); only those fields are copied into the package.
    """
    payload = {
        "session": {
            "id": session.get("id", ""),
            "title": session.get("title", ""),
            "saved_at": session.get("saved_at", ""),
            "messages": session.get("messages", []),
        },
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    package = {
        "format": FORMAT,
        "version": VERSION,
        "scheme": SCHEME,
        "signer": {
            "node_id": identity.node_id,
            "fingerprint": identity.fingerprint,
            "public_key": identity.public_hex(),
        },
        "payload": payload,
    }
    package["signature"] = sign_payload(identity.secret, canonical(package))
    return package


def verify_package(package, expected_pubkey_hex=None) -> dict:
    """Verify a package object. Returns {valid, reason, fingerprint, node_id}.

    Uses the public key embedded in the package unless an expected key
    is pinned (then a key mismatch is itself invalid). The claimed
    fingerprint must match the fingerprint derived from the public key.
    Never raises on malformed input.
    """
    def fail(reason):
        return {"valid": False, "reason": reason, "fingerprint": None, "node_id": None}

    if not isinstance(package, dict):
        return fail("not an object")
    if package.get("format") != FORMAT:
        return fail("unknown format")
    if package.get("version") != VERSION:
        return fail("unsupported version")
    if package.get("scheme") != SCHEME:
        return fail("unknown scheme")
    signer = package.get("signer")
    payload = package.get("payload")
    signature = package.get("signature")
    if not isinstance(signer, dict) or not isinstance(payload, dict) or not isinstance(signature, str):
        return fail("missing fields")
    pubkey_hex = signer.get("public_key")
    claimed_fp = signer.get("fingerprint")
    node_id = signer.get("node_id")
    if not isinstance(pubkey_hex, str) or not isinstance(claimed_fp, str):
        return fail("missing signer key")
    if expected_pubkey_hex is not None and expected_pubkey_hex.lower() != pubkey_hex.lower():
        return fail("signer key mismatch")
    try:
        derived_fp = fingerprint(bytes.fromhex(pubkey_hex))
    except Exception:
        return fail("bad public key")
    if derived_fp != claimed_fp:
        return fail("fingerprint mismatch")
    unsigned = {k: v for k, v in package.items() if k != "signature"}
    if not verify_signature(pubkey_hex, canonical(unsigned), signature):
        return fail("bad signature")
    return {"valid": True, "reason": "ok", "fingerprint": claimed_fp, "node_id": node_id}


def main(argv=None):
    """CLI: python -m hub.package verify <package.json> [--key <pubkey hex>]"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="hub.package")
    sub = parser.add_subparsers(dest="cmd", required=True)
    verify_parser = sub.add_parser("verify", help="verify a chat package file")
    verify_parser.add_argument("path")
    verify_parser.add_argument("--key", default=None,
                               help="expected compressed public key hex (pins the signer)")
    args = parser.parse_args(argv)
    if args.cmd == "verify":
        try:
            with open(args.path, encoding="utf-8") as f:
                package = json.load(f)
        except Exception as exc:
            print(f"invalid: cannot read package: {exc}")
            return 1
        result = verify_package(package, args.key)
        if result["valid"]:
            print(f"valid — signed by {result['fingerprint']}" +
                  (f" ({result['node_id']})" if result["node_id"] else ""))
            return 0
        print(f"invalid — {result['reason']}")
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
