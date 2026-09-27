"""Encryption for store credentials.

Sellers give us WooCommerce API keys and Shopify access tokens. Those are
stored encrypted (Fernet = AES + HMAC) so a database leak alone doesn't hand
anyone access to their stores. Only the backend, holding
CREDENTIALS_ENCRYPTION_KEY, can decrypt them — and the API never sends them
back to the browser.
"""
import json

from cryptography.fernet import Fernet, InvalidToken

import os


class CredentialsError(Exception):
    pass


def _fernet() -> Fernet:
    key = os.environ.get("CREDENTIALS_ENCRYPTION_KEY", "")
    if not key:
        raise CredentialsError("CREDENTIALS_ENCRYPTION_KEY is not set.")
    return Fernet(key.encode())


def encrypt_credentials(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_credentials(token: str) -> dict:
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken as exc:
        raise CredentialsError("Stored credentials can't be decrypted (was the key changed?).") from exc
