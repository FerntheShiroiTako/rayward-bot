"""Encrypts per-guild secrets (Rayward / Bloxlink API keys) before they touch SQLite.

One process-wide key, MASTER_KEY (a Fernet key: 32 url-safe base64 bytes), set once by whoever runs
the bot. Losing it means every stored API key must be re-entered via /setup; it never has to be
consistent across bot versions or shared with server admins. Generate one with:

    python -m banbot.crypto
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(ValueError):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


class SecretBox:
    def __init__(self, master_key: str):
        try:
            self._fernet = Fernet(master_key.encode("ascii"))
        except (ValueError, TypeError) as e:
            raise CryptoError(
                "MASTER_KEY is not a valid Fernet key. Generate one with: python -m banbot.crypto"
            ) from e

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as e:
            raise CryptoError("stored secret could not be decrypted; MASTER_KEY may have changed") from e


if __name__ == "__main__":
    print(generate_key())
