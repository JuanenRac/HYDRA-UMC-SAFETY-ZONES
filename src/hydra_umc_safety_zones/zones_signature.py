# =============================================================================
# HYDRA-UMC-SAFETY-ZONES - src/hydra_umc_safety_zones/zones_signature.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A detached signature for the zones file, so the zones that the service holds fixed are the ones somebody approved.

`serve --zones-file` makes the zones authoritative, but whoever can write the file decides them. With `--zones-key-file` the service also demands a signature
file next to it (`<zones file>.sig`, or `--zones-signature-file`), made by `sign-zones` with the same key, and refuses to start when it is missing, unreadable,
for another file, or does not verify. The signature is an HMAC-SHA256 over a fixed domain string and the SHA-256 of the exact bytes of the file, so a single
changed byte, in the zones or in the calibration, breaks it. Off unless a key is given; the key is read from a file and never printed.

    {"alg": "HMAC-SHA256", "key_id": "...", "sha256": "<hex of the file>", "sig": "<hex>"}
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from .signing import DEFAULT_KEY_ID, SigningError, load_key_file

ALGORITHM = "HMAC-SHA256"
DOMAIN = b"hydra-umc-safety-zones-file-v1\n"


class ZonesSignatureError(ValueError):
    """The zones file is not backed by a valid signature; `str()` is the reason (it names no key)."""


def _mac(key: bytes, key_id: str, digest: str) -> str:
    return hmac.new(key, DOMAIN + f"{key_id}\n{digest}".encode("utf-8"), hashlib.sha256).hexdigest()


def sign_zones(zones_file: str | Path, key_file: str | Path, key_id: str = DEFAULT_KEY_ID) -> dict[str, str]:
    """The signature document of the file as it is now."""
    key = load_key_file(key_file)
    digest = hashlib.sha256(Path(zones_file).read_bytes()).hexdigest()
    return {"alg": ALGORITHM, "key_id": key_id, "sha256": digest, "sig": _mac(key, key_id, digest)}


def verify_zones(zones_file: str | Path, signature_file: str | Path, key_file: str | Path) -> str:
    """The key id of a valid signature; raises ZonesSignatureError (or SigningError for the key) otherwise."""
    key = load_key_file(key_file)
    try:
        document = json.loads(Path(signature_file).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ZonesSignatureError(f"the signature file cannot be read: {exc.strerror or type(exc).__name__}") from exc
    except ValueError as exc:
        raise ZonesSignatureError("the signature file is not valid JSON") from exc
    if not isinstance(document, dict) or document.get("alg") != ALGORITHM:
        raise ZonesSignatureError(f"the signature is not an {ALGORITHM} signature")
    key_id, claimed, sig = document.get("key_id"), document.get("sha256"), document.get("sig")
    if not (isinstance(key_id, str) and key_id and isinstance(claimed, str) and claimed and isinstance(sig, str) and sig):
        raise ZonesSignatureError("the signature lacks key_id, sha256 or sig")
    digest = hashlib.sha256(Path(zones_file).read_bytes()).hexdigest()
    if not hmac.compare_digest(claimed.encode("utf-8"), digest.encode("utf-8")):
        raise ZonesSignatureError("the signature is for another version of the zones file")
    if not hmac.compare_digest(_mac(key, key_id, digest).encode("utf-8"), sig.encode("utf-8")):
        raise ZonesSignatureError("the signature does not verify with this key")
    return key_id


__all__ = ["ZonesSignatureError", "SigningError", "sign_zones", "verify_zones", "ALGORITHM"]
