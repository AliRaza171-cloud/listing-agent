"""Daraz Open Platform client (open.daraz.com) — shared by the store service (seller login,
token renewal) and the publisher (product calls).

Daraz runs on Lazada's open platform:
- every call carries app_key, timestamp (ms), sign_method=sha256 (+ access_token for seller calls)
- sign = HMAC-SHA256(app_secret, api_path + k1v1k2v2… over all params sorted by key).hex().upper()
  (file uploads are not part of the signature)
- seller login: {AUTH_PAGE}/oauth/authorize?response_type=code&client_id=APP_KEY&redirect_uri=…&state=…
  -> ?code=… (valid 30 min) -> /auth/token/create -> access_token (30 days) + refresh_token (180 days)
- answers look like {"code": "0", "data": {...}}; any other code is an error with "message"/"detail".

URLs are settings because Daraz documents them per country (Pakistan defaults below).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

import httpx

API_URL = "https://api.daraz.pk/rest"          # seller API gateway (Pakistan)
AUTH_URL = "https://api.daraz.com/rest"        # /auth/token/create and /auth/token/refresh
AUTH_PAGE = "https://api.daraz.pk"             # where the seller logs in and clicks Authorize


class DarazError(Exception):
    def __init__(self, message: str, *, code: str = "", retryable: bool = False, auth: bool = False):
        super().__init__(message)
        self.code, self.retryable, self.auth = code, retryable, auth


def sign(api_path: str, params: dict[str, str], app_secret: str) -> str:
    base = api_path + "".join(f"{k}{params[k]}" for k in sorted(params))
    return hmac.new(app_secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()


def authorize_url(app_key: str, redirect_uri: str, state: str, auth_page: str = AUTH_PAGE) -> str:
    from urllib.parse import urlencode
    q = urlencode({"response_type": "code", "force_auth": "true", "redirect_uri": redirect_uri,
                   "client_id": app_key, "state": state})
    return f"{auth_page.rstrip('/')}/oauth/authorize?{q}"


# Daraz/Lazada error codes that mean "the seller must log in again"
AUTH_CODES = {"IllegalAccessToken", "InvalidToken", "IllegalRefreshToken", "MissingAccessToken", "AppAuthExpired"}


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: int            # unix seconds
    refresh_expires_at: int
    seller_id: str = ""
    account: str = ""
    short_code: str = ""

    def as_credentials(self) -> dict:
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "expires_at": str(self.expires_at), "refresh_expires_at": str(self.refresh_expires_at),
                "seller_id": self.seller_id, "account": self.account, "short_code": self.short_code}


class DarazClient:
    def __init__(self, app_key: str, app_secret: str, *, api_url: str = API_URL, auth_url: str = AUTH_URL,
                 http: httpx.AsyncClient | None = None, timeout: float = 45.0):
        if not (app_key and app_secret):
            raise DarazError("Daraz isn't set up on this server yet (DARAZ_APP_KEY / DARAZ_APP_SECRET).")
        self.app_key, self.app_secret = app_key, app_secret
        self.api_url, self.auth_url = api_url.rstrip("/"), auth_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    def _signed(self, path: str, params: dict, access_token: str | None) -> dict[str, str]:
        p = {k: str(v) for k, v in params.items() if v is not None}
        p.update(app_key=self.app_key, timestamp=str(int(time.time() * 1000)), sign_method="sha256")
        if access_token:
            p["access_token"] = access_token
        p["sign"] = sign(path, p, self.app_secret)
        return p

    async def call(self, path: str, params: dict | None = None, *, access_token: str | None = None,
                   method: str = "GET", files: dict | None = None, base: str | None = None) -> dict:
        url = f"{base or self.api_url}{path}"
        p = self._signed(path, params or {}, access_token)
        try:
            if method == "GET":
                r = await self._http.get(url, params=p)
            else:
                r = await self._http.post(url, data=p, files=files)
        except httpx.TimeoutException:
            raise DarazError("Daraz took too long to answer.", retryable=True)
        except httpx.HTTPError as exc:
            raise DarazError(f"Couldn't reach Daraz ({exc.__class__.__name__}).", retryable=True)
        if r.status_code == 429 or r.status_code >= 500:
            raise DarazError(f"Daraz had a problem ({r.status_code}). We'll retry.", retryable=True)
        try:
            body = r.json()
        except ValueError:
            raise DarazError(f"Daraz sent an unexpected answer ({r.status_code}).", retryable=r.status_code >= 500)
        code = str(body.get("code", "0"))
        if code != "0":
            detail = body.get("detail") or []
            extra = "; ".join(f"{d.get('field', '')}: {d.get('message', '')}".strip(": ") for d in detail
                              if isinstance(d, dict))[:300]
            msg = str(body.get("message") or code)
            throttled = code in ("ApiCallLimit", "AppCallLimit", "SellerCallLimit", "ServiceTimeout")
            raise DarazError(f"Daraz: {msg}" + (f" ({extra})" if extra else ""), code=code,
                             retryable=throttled, auth=code in AUTH_CODES)
        return body

    # ------------------------------------------------------------ seller login / token renewal

    def _tokens(self, body: dict) -> Tokens:
        now = int(time.time())
        info = (body.get("country_user_info") or [{}])[0] or {}
        if not body.get("access_token"):
            raise DarazError("Daraz didn't give an access token.", auth=True)
        return Tokens(
            access_token=body["access_token"], refresh_token=body.get("refresh_token", ""),
            expires_at=now + int(body.get("expires_in") or 30 * 86400),
            refresh_expires_at=now + int(body.get("refresh_expires_in") or 180 * 86400),
            seller_id=str(info.get("seller_id") or ""), account=str(body.get("account") or ""),
            short_code=str(info.get("short_code") or ""))

    async def create_token(self, code: str) -> Tokens:
        return self._tokens(await self.call("/auth/token/create", {"code": code}, base=self.auth_url))

    async def refresh_token(self, refresh_token: str) -> Tokens:
        return self._tokens(await self.call("/auth/token/refresh", {"refresh_token": refresh_token}, base=self.auth_url))
