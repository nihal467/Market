"""Client-compatible encryption for the dashboard's hidden amounts.

Encrypts a JSON-serialisable object with AES-256-GCM using a key derived from a
password via PBKDF2-HMAC-SHA256. The output format matches the browser Web
Crypto API so the dashboard can decrypt it client-side:

    key  = PBKDF2(password, salt, iterations, sha256, 32 bytes)
    ct   = AES-GCM(key, iv).encrypt(plaintext)   # ciphertext||tag (Web Crypto compatible)

All parts are base64-encoded. Without the password the blob is unreadable.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

ITERATIONS = 200_000


def encrypt_payload(obj, password: str) -> dict:
    """Return {'salt','iv','ct','iter'} base64 blob decryptable in the browser."""
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = _derive_key(password, salt)
    plaintext = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    ct = AESGCM(key).encrypt(iv, plaintext, None)
    return {
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode(),
        "iter": ITERATIONS,
    }


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))
