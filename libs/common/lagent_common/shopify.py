"""Shopify basics shared by the store service (install, sign-in, token renewal) and billing.

Listing Agent's public Shopify app runs *inside* the Shopify admin (an embedded app):
- Shopify installs it (Shopify-managed install) and loads our website in an iframe.
- App Bridge in that page gives us a short-lived ID token (a JWT signed with our client secret).
- The store service checks it (verify_id_token) and swaps it for an offline access token
  (token_exchange). Public apps created after 1 April 2026 must use *expiring* offline tokens:
  the access token lasts 1 hour, the refresh token 90 days, and each refresh returns a new pair.
- The older "Connect" button on our own site uses the authorization-code flow (code_exchange),
  also with expiring tokens.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass

import httpx

API_VERSION = "2026-07"
SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
OFFLINE_TOKEN_TYPE = "urn:shopify:params:oauth:token-type:offline-access-token"

SHOP_INFO = """{ shop { name email contactEmail currencyCode
  billingAddress { countryCodeV2 } plan { partnerDevelopment } } }"""


class ShopifyError(Exception):
    """auth=True: the merchant has to open/approve the app again. retryable=True: Shopify is busy/down."""

    def __init__(self, message: str, *, status: int = 0, retryable: bool = False, auth: bool = False):
        super().__init__(message)
        self.status, self.retryable, self.auth = status, retryable, auth


def shop_handle(shop: str) -> str:
    return shop.split(".")[0]


def admin_app_url(shop: str, client_id: str, path: str = "") -> str:
    """Where the app lives inside the Shopify admin; `path` is appended to our App URL there."""
    return f"https://admin.shopify.com/store/{shop_handle(shop)}/apps/{client_id}{path}"


# ---------------------------------------------------------------- ID (session) tokens

def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", str(url or "")).split("/")[0].lower()


def verify_id_token(token: str, client_id: str, secret: str, *, now: float | None = None, leeway: int = 10) -> dict:
    """Checks an App Bridge ID token: HS256 signature with our client secret, aud = our client ID,
    exp/nbf, and that iss and dest name the same *.myshopify.com shop. Returns the claims plus "shop"."""
    if not (token and client_id and secret):
        raise ShopifyError("Missing Shopify session token.", auth=True)
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url_decode(head_b64))
        claims = json.loads(_b64url_decode(body_b64))
        signature = _b64url_decode(sig_b64)
    except (ValueError, TypeError):
        raise ShopifyError("Malformed Shopify session token.", auth=True)
    if header.get("alg") != "HS256":
        raise ShopifyError("Unexpected token algorithm.", auth=True)
    expected = hmac.new(secret.encode(), f"{head_b64}.{body_b64}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise ShopifyError("Shopify session token signature doesn't match.", auth=True)
    t = time.time() if now is None else now
    aud = claims.get("aud")
    if (aud if isinstance(aud, str) else None) != client_id and not (isinstance(aud, list) and client_id in aud):
        raise ShopifyError("Shopify session token is for another app.", auth=True)
    if float(claims.get("exp", 0)) < t - leeway:
        raise ShopifyError("Shopify session token expired.", auth=True)
    if float(claims.get("nbf", 0)) > t + leeway:
        raise ShopifyError("Shopify session token isn't valid yet.", auth=True)
    shop = _host(claims.get("dest"))
    if not SHOP_RE.match(shop) or _host(claims.get("iss")) != shop:
        raise ShopifyError("Shopify session token names an unknown shop.", auth=True)
    return {**claims, "shop": shop}


# ---------------------------------------------------------------- access tokens

@dataclass
class Tokens:
    access_token: str
    scope: str = ""
    expires_at: int | None = None           # None = a non-expiring token (older apps / custom apps)
    refresh_token: str | None = None
    refresh_expires_at: int | None = None

    def as_credentials(self, client_id: str = "") -> dict:
        """What the store service saves (encrypted). Which app the token belongs to is recorded by the
        store service in its own column (via_app), never inside credentials a seller could submit."""
        creds = {"access_token": self.access_token, "scope": self.scope}
        if self.expires_at:
            creds["expires_at"] = str(self.expires_at)
        if self.refresh_token:
            creds["refresh_token"] = self.refresh_token
            creds["refresh_expires_at"] = str(self.refresh_expires_at or 0)
        return creds


def _tokens(body: dict, now: float) -> Tokens:
    token = str(body.get("access_token") or "")
    if not token:
        raise ShopifyError("Shopify didn't return an access token.", auth=True)
    expires_in = body.get("expires_in")
    refresh_in = body.get("refresh_token_expires_in")
    return Tokens(
        access_token=token,
        scope=str(body.get("scope") or ""),
        expires_at=int(now + int(expires_in)) if expires_in else None,
        refresh_token=body.get("refresh_token") or None,
        refresh_expires_at=int(now + int(refresh_in)) if refresh_in else None,
    )


async def _access_token_call(http: httpx.AsyncClient, shop: str, form: dict) -> Tokens:
    if not SHOP_RE.match(shop):
        raise ShopifyError("Not a Shopify store address.", auth=True)
    now = time.time()
    try:
        r = await http.post(f"https://{shop}/admin/oauth/access_token", data=form,
                            headers={"Accept": "application/json"})
    except httpx.HTTPError:
        raise ShopifyError(f"Couldn't reach {shop}.", retryable=True)
    if r.status_code >= 500 or r.status_code == 429:
        raise ShopifyError("Shopify isn't answering right now.", status=r.status_code, retryable=True)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        msg = body.get("error_description") or body.get("error") or f"Shopify refused ({r.status_code})."
        raise ShopifyError(str(msg), status=r.status_code, auth=True)
    return _tokens(body, now)


async def token_exchange(http: httpx.AsyncClient, shop: str, client_id: str, secret: str, id_token: str) -> Tokens:
    """ID token (from App Bridge) -> expiring offline access token for this shop."""
    return await _access_token_call(http, shop, {
        "client_id": client_id, "client_secret": secret, "grant_type": TOKEN_EXCHANGE,
        "subject_token": id_token, "subject_token_type": ID_TOKEN_TYPE,
        "requested_token_type": OFFLINE_TOKEN_TYPE, "expiring": "1",
    })


async def code_exchange(http: httpx.AsyncClient, shop: str, client_id: str, secret: str, code: str) -> Tokens:
    """Authorization code (from the OAuth redirect) -> expiring offline access token."""
    return await _access_token_call(http, shop, {
        "client_id": client_id, "client_secret": secret, "code": code, "expiring": "1"})


async def refresh(http: httpx.AsyncClient, shop: str, client_id: str, secret: str, refresh_token: str) -> Tokens:
    """New access token + new refresh token (the old refresh token stops working)."""
    return await _access_token_call(http, shop, {
        "client_id": client_id, "client_secret": secret, "grant_type": "refresh_token",
        "refresh_token": refresh_token})


# ---------------------------------------------------------------- Admin GraphQL

async def graphql(http: httpx.AsyncClient, shop: str, access_token: str, query: str,
                  variables: dict | None = None, *, version: str = API_VERSION) -> dict:
    try:
        r = await http.post(f"https://{shop}/admin/api/{version}/graphql.json",
                            json={"query": query, "variables": variables or {}},
                            headers={"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"})
    except httpx.HTTPError:
        raise ShopifyError(f"Couldn't reach {shop}.", retryable=True)
    if r.status_code in (401, 403):
        raise ShopifyError("Shopify access was revoked — open Listing Agent in Shopify again.",
                           status=r.status_code, auth=True)
    if r.status_code >= 500 or r.status_code == 429:
        raise ShopifyError("Shopify isn't answering right now.", status=r.status_code, retryable=True)
    try:
        body = r.json()
    except ValueError:
        raise ShopifyError(f"Unexpected answer from Shopify ({r.status_code}).", status=r.status_code)
    if r.status_code >= 400 or body.get("errors"):
        errors = body.get("errors")
        msg = errors[0].get("message") if isinstance(errors, list) and errors and isinstance(errors[0], dict) else errors
        throttled = "throttled" in str(msg).lower()
        raise ShopifyError(str(msg or f"Shopify error ({r.status_code})."), status=r.status_code, retryable=throttled)
    return body.get("data") or {}
