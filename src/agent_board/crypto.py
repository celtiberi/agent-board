"""Symmetric secret encryption at rest using Fernet derived via HKDF-SHA256."""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def _derive_fernet(master_secret: str) -> Fernet:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"agent-board-v1-at-rest-salt",
        info=b"agent-board-fernet-key",
    )
    key = base64.urlsafe_b64encode(hkdf.derive(master_secret.encode("utf-8")))
    return Fernet(key)


class SecretBox:
    """Encrypts and decrypts stored subscription and TOTP secrets."""

    def __init__(self, master_secret: str) -> None:
        self._fernet = _derive_fernet(master_secret)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
