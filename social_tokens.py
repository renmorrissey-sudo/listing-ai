"""Fernet-based encryption for social-media OAuth tokens at rest.

No reversible credential storage exists anywhere else in this app (webhook
secrets are hashed, not encrypted), so this is new, narrowly-scoped
infrastructure: it only ever encrypts/decrypts `social_connections`
access/refresh tokens, and is lazy-checked (like other optional integrations)
so its absence never blocks app startup — only an actual "Connect" attempt.
"""

from __future__ import annotations

import logging

import config

logger = logging.getLogger(__name__)


class SocialTokenEncryptionError(RuntimeError):
    """SOCIAL_TOKEN_ENCRYPTION_KEY is missing or invalid."""


def _fernet():
    from cryptography.fernet import Fernet, InvalidToken  # noqa: F401

    key = (config.SOCIAL_TOKEN_ENCRYPTION_KEY or "").strip()
    if not key:
        raise SocialTokenEncryptionError(
            "Social account connections are not configured yet. "
            "SOCIAL_TOKEN_ENCRYPTION_KEY is not set."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:
        raise SocialTokenEncryptionError(
            "Social account connections are misconfigured (invalid encryption key)."
        ) from exc


def is_configured() -> bool:
    return bool((config.SOCIAL_TOKEN_ENCRYPTION_KEY or "").strip())


def encrypt_token(plaintext: str | None) -> str | None:
    """Return an encrypted, storage-safe string, or None for empty input."""
    if not plaintext:
        return None
    fernet = _fernet()
    return fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(ciphertext: str | None) -> str | None:
    """Decrypt a stored token. Returns None on missing input or bad/rotated key."""
    if not ciphertext:
        return None
    from cryptography.fernet import InvalidToken

    fernet = _fernet()
    try:
        return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.warning("Social token could not be decrypted (invalid/rotated key).")
        return None
