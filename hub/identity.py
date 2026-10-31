"""Identity: a CETI-style identity.dat, implemented in pure stdlib Python.

On first run a fresh secp256k1 master key is generated and stored in
data/identity.dat with mode 0600. The secret never leaves this machine.
Only the compressed public key is ever shared. The fingerprint and the
identity hash are derived from that public key, so memory stored under
one identity is never visible to another identity, even in the same DB.
"""
import hashlib
import os
import secrets

# secp256k1 curve parameters (stdlib only, no pip dependency)
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    x1, y1 = p
    x2, y2 = q
    if x1 == x2 and y1 != y2:
        return None
    if p == q:
        lam = (3 * x1 * x1) * pow(2 * y1, -1, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def _mul(k, point=(Gx, Gy)):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def compressed_pubkey(secret: bytes) -> bytes:
    k = int.from_bytes(secret, "big") % N
    if k == 0:
        k = 1
    x, y = _mul(k)
    prefix = b"\x03" if (y & 1) else b"\x02"
    return prefix + x.to_bytes(32, "big")


def identity_hash(pubkey: bytes) -> str:
    return hashlib.sha256(pubkey).hexdigest()


def fingerprint(pubkey: bytes) -> str:
    h = identity_hash(pubkey)[:16].upper()
    return "HUB-" + "-".join(h[i:i + 4] for i in range(0, 16, 4))


class Identity:
    def __init__(self, secret: bytes):
        self.secret = secret
        self.pubkey = compressed_pubkey(secret)
        self.hash = identity_hash(self.pubkey)
        self.fingerprint = fingerprint(self.pubkey)
        self.node_id = "hub" + self.hash[:12]

    def public_hex(self) -> str:
        return self.pubkey.hex()


def identity_path(data_dir: str) -> str:
    return os.path.join(data_dir, "identity.dat")


def ensure_identity(data_dir: str):
    """Load identity.dat, or generate and store a new one. Returns (Identity, existed)."""
    os.makedirs(data_dir, exist_ok=True)
    path = identity_path(data_dir)
    if os.path.exists(path):
        with open(path, "rb") as f:
            secret = f.read()
        if len(secret) != 32:
            raise ValueError("identity.dat is not a 32-byte secret; back it up and delete it to regenerate")
        return Identity(secret), True
    secret = secrets.token_bytes(32)
    # Write atomically-ish, then lock down permissions (best effort on Windows).
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(secret)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return Identity(secret), False
