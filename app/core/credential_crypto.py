"""관리자 재조회가 필요한 API Key의 가역 암호화."""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialCipher:
    def __init__(self, encoded_key: str | None) -> None:
        self._key = base64.urlsafe_b64decode(encoded_key.encode()) if encoded_key else None
        if self._key is not None and len(self._key) not in {16, 24, 32}:
            raise ValueError("API_KEY_ENCRYPTION_KEY must be a URL-safe base64 AES key")

    @property
    def configured(self) -> bool:
        return self._key is not None

    def encrypt(self, plaintext: str) -> tuple[str, str]:
        if self._key is None:
            raise ValueError("API Key encryption is not configured")
        nonce = os.urandom(12)
        encrypted = AESGCM(self._key).encrypt(nonce, plaintext.encode(), b"genie-gateway-api-key")
        return base64.urlsafe_b64encode(encrypted).decode(), base64.urlsafe_b64encode(nonce).decode()

    def decrypt(self, encrypted: str, nonce: str) -> str:
        if self._key is None:
            raise ValueError("API Key encryption is not configured")
        value = AESGCM(self._key).decrypt(
            base64.urlsafe_b64decode(nonce),
            base64.urlsafe_b64decode(encrypted),
            b"genie-gateway-api-key",
        )
        return value.decode()
