"""eBay REST API basics shared by the store service (seller login, token renewal) and the publisher.

- Seller login (OAuth authorization code): auth.ebay.com/oauth2/authorize?client_id&redirect_uri=<RuName>
  &response_type=code&scope=…&state=… -> ?code=… -> identity/v1/oauth2/token (Basic client_id:secret)
  -> access_token (2 hours) + refresh_token (~18 months). Renewal: grant_type=refresh_token.
- Application token (client_credentials) for the Taxonomy API (category suggestions, item specifics).
- eBay calls the redirect URL by its "RuName" (set up in the developer portal), not by the URL itself.
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

API = "https://api.ebay.com"
APIZ = "https://apiz.ebay.com"          # identity API host
APIM = "https://apim.ebay.com"          # media API host (photo uploads)
AUTH = "https://auth.ebay.com"
SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly",
]


@dataclass(frozen=True)
class EbayMarket:
    id: str          # EBAY_US
    name: str        # eBay US
    site: str        # https://www.ebay.com
    currency: str
    language: str    # Content-Language for inventory calls
    country: str     # ISO code for the item location


# English-language eBay sites (listings are written in English). More can be added here.
MARKETPLACES: dict[str, EbayMarket] = {m.id: m for m in [
    EbayMarket("EBAY_US", "eBay US", "https://www.ebay.com", "USD", "en-US", "US"),
    EbayMarket("EBAY_GB", "eBay UK", "https://www.ebay.co.uk", "GBP", "en-GB", "GB"),
    EbayMarket("EBAY_CA", "eBay Canada", "https://www.ebay.ca", "CAD", "en-CA", "CA"),
    EbayMarket("EBAY_AU", "eBay Australia", "https://www.ebay.com.au", "AUD", "en-AU", "AU"),
]}


def marketplace_for_site(url: str) -> EbayMarket | None:
    for m in MARKETPLACES.values():
        if url.startswith(m.site):
            return m
    return None


class EbayError(Exception):
    def __init__(self, message: str, *, status: int = 0, retryable: bool = False, auth: bool = False):
        super().__init__(message)
        self.status, self.retryable, self.auth = status, retryable, auth


def authorize_url(client_id: str, ru_name: str, state: str, auth: str = AUTH) -> str:
    q = urlencode({"client_id": client_id, "redirect_uri": ru_name, "response_type": "code",
                   "scope": " ".join(SCOPES), "state": state, "prompt": "login"})
    return f"{auth.rstrip('/')}/oauth2/authorize?{q}"


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: int
    refresh_expires_at: int


def error_message(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200] or str(r.status_code)
    if isinstance(body, dict):
        if body.get("error_description") or body.get("error"):
            return str(body.get("error_description") or body.get("error"))
        errs = body.get("errors") or body.get("warnings") or []
        if errs and isinstance(errs, list):
            e = errs[0]
            return str(e.get("longMessage") or e.get("message") or e.get("errorId"))
    return str(body)[:200]


class EbayAuth:
    def __init__(self, client_id: str, client_secret: str, ru_name: str = "", *, api: str = API,
                 http: httpx.AsyncClient | None = None):
        if not (client_id and client_secret):
            raise EbayError("eBay isn't set up on this server yet (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET).")
        self.client_id, self.client_secret, self.ru_name, self.api = client_id, client_secret, ru_name, api.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=30)
        self._app_token: tuple[str, float] | None = None

    async def _token(self, form: dict) -> dict:
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        try:
            r = await self._http.post(f"{self.api}/identity/v1/oauth2/token", data=form,
                                      headers={"Authorization": f"Basic {basic}",
                                               "Content-Type": "application/x-www-form-urlencoded"})
        except httpx.HTTPError as exc:
            raise EbayError(f"Couldn't reach eBay ({exc.__class__.__name__}).", retryable=True)
        if r.status_code >= 500 or r.status_code == 429:
            raise EbayError(f"eBay had a problem ({r.status_code}).", status=r.status_code, retryable=True)
        if r.status_code != 200:
            raise EbayError(f"eBay: {error_message(r)}", status=r.status_code, auth=True)
        return r.json()

    async def create_token(self, code: str) -> Tokens:
        d = await self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": self.ru_name})
        now = int(time.time())
        return Tokens(d["access_token"], d.get("refresh_token", ""), now + int(d.get("expires_in") or 7200),
                      now + int(d.get("refresh_token_expires_in") or 47304000))

    async def refresh(self, refresh_token: str) -> tuple[str, int]:
        d = await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token, "scope": " ".join(SCOPES)})
        return d["access_token"], int(time.time()) + int(d.get("expires_in") or 7200)

    async def app_token(self) -> str:
        """Application token (no seller) for the Taxonomy API; cached until near expiry."""
        if self._app_token and self._app_token[1] > time.time() + 120:
            return self._app_token[0]
        d = await self._token({"grant_type": "client_credentials", "scope": SCOPES[0]})
        self._app_token = (d["access_token"], time.time() + int(d.get("expires_in") or 7200))
        return self._app_token[0]
