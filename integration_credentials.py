"""Fernet encryption for tenant-owned third-party integration credentials."""

from __future__ import annotations

import logging

import config

logger = logging.getLogger(__name__)


class IntegrationCredentialError(RuntimeError):
    """Credential encryption is missing, invalid, or cannot decrypt stored data."""


def _fernet():
    from cryptography.fernet import Fernet

    key = (config.INTEGRATION_CREDENTIAL_ENCRYPTION_KEY or "").strip()
    if not key:
        raise IntegrationCredentialError(
            "Integration credential encryption is not configured."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:
        raise IntegrationCredentialError(
            "Integration credential encryption key is invalid."
        ) from exc


def is_configured() -> bool:
    return bool((config.INTEGRATION_CREDENTIAL_ENCRYPTION_KEY or "").strip())


def encrypt_secret(plaintext: str | None) -> str | None:
    if not plaintext:
        return None
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.warning("Integration credential could not be decrypted.")
        return None
