"""Trust boundary helpers (contracts/README.md §2).

Every service endpoint depends on `require_internal`, so only the gateway and
other services (which hold INTERNAL_TOKEN) can call it. `current_user_id` reads
the user the gateway already authenticated.
"""
import hmac
import os
import uuid

from fastapi import Header, HTTPException, status


def _internal_token() -> str:
    token = os.environ.get("INTERNAL_TOKEN", "")
    if not token:
        raise RuntimeError("INTERNAL_TOKEN is not set")
    return token


def require_internal(x_internal_token: str | None = Header(default=None)) -> None:
    if not x_internal_token or not hmac.compare_digest(x_internal_token, _internal_token()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not an internal request.")


def current_user_id(x_user_id: str | None = Header(default=None)) -> uuid.UUID:
    if not x_user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in required.")
    try:
        return uuid.UUID(x_user_id)
    except ValueError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid user.")
