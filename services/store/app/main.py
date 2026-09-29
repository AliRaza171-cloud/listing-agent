"""Store service: a seller's connected stores and their encrypted credentials.

It never talks to Shopify/WooCommerce itself — connection tests go to publisher
(contracts/README.md §2.2). Credentials leave this service only through
GET /internal/stores/{id}/credentials, which the gateway never routes.
"""
import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import Column, DateTime, Enum, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base

from lagent_common import daraz, ebay
from lagent_common import shopify as shopify_api
from lagent_common.correlation import outgoing_headers
from lagent_common.db import make_db
from lagent_common.internal import current_user_id, require_internal
from lagent_common.service import create_service

from app import detect
from app.crypto import decrypt_credentials, encrypt_credentials


class Settings(BaseSettings):
    DATABASE_URL: str
    INTERNAL_TOKEN: str
    PUBLISHER_URL: str = "http://publisher:8000"
    AUTH_URL: str = "http://auth:8000"
    # Local development only: lets you connect a store running on your own machine
    # (e.g. Smart Click at http://localhost:8000). Keep false in production.
    ALLOW_HTTP_STORES: bool = False

    # One-click "Connect" buttons. The stores send the seller (Shopify) or the keys (WooCommerce)
    # back to this API, so it needs its public address; APP_URL is where the seller lands afterwards.
    APP_URL: str = "http://localhost:3000"
    PUBLIC_BASE_URL: str = ""
    GATEWAY_PORT: int = 8000
    # Listing Agent's own Shopify app (Shopify Dev Dashboard). Empty = Shopify button hidden.
    SHOPIFY_CLIENT_ID: str = ""
    SHOPIFY_CLIENT_SECRET: str = ""
    SHOPIFY_SCOPES: str = "write_products,read_locations,write_inventory,write_publications"
    # The app's Shopify App Store page, once Shopify has approved it. When set, the Stores page
    # sends sellers there to install (Shopify requires installs to start on Shopify).
    SHOPIFY_APP_STORE_URL: str = ""
    # Inside Docker "localhost" is this container; a store running on your own PC is reached
    # through this name instead (same as the publisher). Empty on a server.
    STORE_LOCALHOST_ALIAS: str = ""
    # Listing Agent's Daraz app (open.daraz.com, app console). Empty = Daraz button hidden.
    DARAZ_APP_KEY: str = ""
    DARAZ_APP_SECRET: str = ""
    DARAZ_API_URL: str = daraz.API_URL
    DARAZ_AUTH_URL: str = daraz.AUTH_URL
    DARAZ_AUTH_PAGE: str = daraz.AUTH_PAGE
    # Listing Agent's eBay app (developer.ebay.com → Application keys, Production). The RuName is the name
    # eBay gives the "accept" redirect URL (User Tokens → Get a Token from eBay via Your Application).
    EBAY_CLIENT_ID: str = ""
    EBAY_CLIENT_SECRET: str = ""
    EBAY_RU_NAME: str = ""
    # Marketplace account deletion notifications (required by eBay for production keys)
    EBAY_VERIFICATION_TOKEN: str = ""
    EBAY_API_URL: str = ebay.API
    EBAY_AUTH_URL: str = ebay.AUTH
    EBAY_IDENTITY_URL: str = ebay.APIZ

    @property
    def api_base(self) -> str:
        return (self.PUBLIC_BASE_URL or f"http://localhost:{self.GATEWAY_PORT}").rstrip("/")


settings = Settings()
engine, SessionLocal, get_db = make_db(settings.DATABASE_URL)
Base = declarative_base()
_http = httpx.AsyncClient(timeout=30)
log = logging.getLogger("lagent.store")

# What each platform needs to connect (Shopify's token comes from its OAuth flow, added in Phase 4c).
REQUIRED_CREDENTIALS = {
    "woocommerce": {"consumer_key", "consumer_secret"},
    "custom": {"api_key"},
    "shopify": {"access_token"},   # or client_id + client_secret (see _missing)
}


# What a seller may submit for a manual ("Advanced") connection — anything else is dropped, so nobody can
# slip in fields the server sets itself (e.g. refresh tokens or expiry times).
ALLOWED_CREDENTIALS = {
    "woocommerce": {"consumer_key", "consumer_secret", "wp_username", "wp_app_password"},
    "custom": {"api_key"},
    "shopify": {"access_token", "client_id", "client_secret"},
}


def _missing(platform: str, credentials: dict) -> set[str]:
    have = {k for k, v in credentials.items() if str(v or "").strip()}
    if platform == "shopify" and {"client_id", "client_secret"} <= have:
        return set()
    return REQUIRED_CREDENTIALS[platform] - have


class StoreConnection(Base):
    __tablename__ = "store_connections"
    __table_args__ = (UniqueConstraint("user_id", "platform", "store_url"),)
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    platform = Column(Enum("custom", "woocommerce", "shopify", "daraz", "ebay", name="store_platform", create_type=False), nullable=False)
    name = Column(String, nullable=False)
    store_url = Column(String, nullable=False)
    credentials_encrypted = Column(String, nullable=False)
    status = Column(Enum("active", "error", "disconnected", name="connection_status", create_type=False),
                    default="active", nullable=False)
    last_error = Column(String)
    # Shopify: the client ID of OUR app when the token came from it (set by the server only; '*' = our
    # app before this was recorded). A shop connected with its own app's keys has None.
    via_app = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ConnectRequest(Base):
    __tablename__ = "connect_requests"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    platform = Column(Enum("custom", "woocommerce", "shopify", "daraz", "ebay", name="store_platform", create_type=False), nullable=False)
    name = Column(String, nullable=False)
    store_url = Column(String, nullable=False)
    status = Column(String, default="pending", nullable=False)
    error = Column(String)
    store_id = Column(UUID(as_uuid=True))
    extra = Column(JSONB)          # e.g. eBay: {"marketplace", "city", "postal_code"}
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ConnectIn(BaseModel):
    platform: str = Field(pattern="^(custom|woocommerce|shopify)$")
    name: str = Field(min_length=1, max_length=80)
    store_url: str = Field(pattern=r"^https?://", max_length=300)
    credentials: dict[str, str]


def _public(s: StoreConnection) -> dict:  # never includes credentials
    return {"id": str(s.id), "platform": s.platform, "name": s.name, "store_url": s.store_url,
            "status": s.status, "last_error": s.last_error, "created_at": s.created_at.isoformat()}


router = APIRouter(dependencies=[Depends(require_internal)])


@router.get("/stores")
def list_stores(user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    rows = db.query(StoreConnection).filter(StoreConnection.user_id == user_id).order_by(StoreConnection.created_at)
    return [_public(s) for s in rows]


class ConnectionFailed(Exception):
    """Seller-safe message."""


async def _test(platform: str, store_url: str, credentials: dict) -> None:
    try:
        test = await _http.post(
            f"{settings.PUBLISHER_URL}/internal/connectors/test",
            json={"platform": platform, "store_url": store_url, "credentials": credentials},
            headers=outgoing_headers(settings.INTERNAL_TOKEN),
        )
    except httpx.HTTPError:
        raise ConnectionFailed("Couldn't check the store right now — try again in a moment.")
    if test.status_code == 422:
        raise ConnectionFailed(test.json().get("detail", "The store rejected these details."))
    if test.status_code == 501:
        raise ConnectionFailed(f"Publishing to {platform} isn't available yet.")
    if test.status_code >= 400:
        raise ConnectionFailed("Couldn't check the store right now — try again in a moment.")


def _save(db: Session, user_id, platform: str, name: str, store_url: str, credentials: dict,
          replace: bool, via_app: str | None = None) -> StoreConnection:
    """Creates the connection, or (replace=True, used by one-click reconnects) refreshes its keys."""
    existing = db.query(StoreConnection).filter_by(user_id=user_id, platform=platform, store_url=store_url).first()
    if existing and not replace and existing.status != "disconnected":
        raise HTTPException(409, "This store is already connected.")
    store = existing or StoreConnection(user_id=user_id, platform=platform, store_url=store_url)
    store.name = name
    store.via_app = via_app
    store.credentials_encrypted = encrypt_credentials(credentials)
    store.status, store.last_error = "active", None
    db.add(store)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This store is already connected.")
    db.refresh(store)
    return store


@router.post("/stores", status_code=201)
async def connect_store(data: ConnectIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    if data.store_url.lower().startswith("http://") and not settings.ALLOW_HTTP_STORES:
        raise HTTPException(422, "The store address must start with https://")
    data.credentials = {k: v for k, v in data.credentials.items() if k in ALLOWED_CREDENTIALS[data.platform]}
    missing = _missing(data.platform, data.credentials)
    if missing:
        if data.platform == "shopify":
            raise HTTPException(422, "Enter an Admin API access token, or the app's Client ID and Client secret.")
        raise HTTPException(422, f"Missing: {', '.join(sorted(missing))}")
    store_url = data.store_url.rstrip("/")
    try:
        await _test(data.platform, store_url, data.credentials)
    except ConnectionFailed as exc:
        raise HTTPException(422, str(exc))
    return _public(_save(db, user_id, data.platform, data.name, store_url, data.credentials, replace=False))


# ---------------------------------------------------------------- one-click connect

REQUEST_MINUTES = 60
SHOP_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")


class StartIn(BaseModel):
    store: str = Field(default="", max_length=300)   # WooCommerce site address or Shopify store name (not used for Daraz)
    name: str | None = Field(default=None, max_length=80)
    # eBay only: which eBay site, and where items ship from (eBay requires an item location)
    marketplace: str | None = Field(default=None, max_length=20)
    city: str | None = Field(default=None, max_length=80)
    postal_code: str | None = Field(default=None, max_length=20)


def _new_request(db: Session, user_id, platform: str, name: str, store_url: str) -> ConnectRequest:
    req = ConnectRequest(user_id=user_id, platform=platform, name=name, store_url=store_url)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _open_request(db: Session, state: str, platform: str) -> ConnectRequest | None:
    try:
        req = db.get(ConnectRequest, uuid.UUID(str(state)))
    except ValueError:
        return None
    if not req or req.platform != platform or req.status != "pending":
        return None
    if req.created_at < datetime.utcnow() - timedelta(minutes=REQUEST_MINUTES):
        return None
    return req


def _finish(db: Session, req: ConnectRequest, *, store: StoreConnection | None = None, error: str | None = None):
    req.status = "connected" if store else "failed"
    req.store_id, req.error = (store.id if store else None), (error[:300] if error else None)
    db.commit()


def _woo_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    if not parts.hostname or "." not in parts.hostname:
        raise HTTPException(422, "Enter your store's web address, e.g. yourstore.com")
    if parts.scheme == "http" and not settings.ALLOW_HTTP_STORES:
        raise HTTPException(422, "The store address must start with https://")
    return url


async def _shopify_from_address(raw: str) -> str:
    """A store's own website (custom domain) -> its *.myshopify.com name, found from the public site."""
    host = re.sub(r"^https?://", "", raw.strip().lower()).split("/")[0]
    if "." not in host or host.endswith(".myshopify.com"):
        return _shop_domain(raw)
    try:
        found = await detect.detect(raw, _http, allow_local=settings.ALLOW_HTTP_STORES, alias=settings.STORE_LOCALHOST_ALIAS)
    except detect.DetectError as exc:
        raise HTTPException(422, str(exc))
    if found.platform != "shopify":
        what = f" — it runs on {found.name}" if found.platform not in ("unknown",) else ""
        raise HTTPException(422, f"{host} doesn't look like a Shopify store{what}. Try the “Your store’s address” box above.")
    if not found.store:
        raise HTTPException(422, "This is a Shopify store, but it hides its Shopify name. Enter it instead "
                                 "(yourstore.myshopify.com) — the owner sees it in Shopify admin → Settings → Domains.")
    return found.store


def _shop_domain(raw: str) -> str:
    shop = re.sub(r"^https?://", "", raw.strip().lower()).split("/")[0]
    if "." not in shop:
        shop += ".myshopify.com"
    if not SHOP_RE.match(shop):
        raise HTTPException(422, "Enter your Shopify store name, e.g. yourstore (from yourstore.myshopify.com)")
    return shop


@router.get("/connect/options")
def connect_options():
    # WordPress only sends keys to an https callback it can reach, so WooCommerce one-click needs
    # Listing Agent online (PUBLIC_BASE_URL). On a PC, sellers use API keys instead.
    online = settings.api_base.lower().startswith("https://")
    return {"shopify": bool(settings.SHOPIFY_CLIENT_ID and settings.SHOPIFY_CLIENT_SECRET) and online,
            "woocommerce": online, "custom": True,
            "daraz": bool(settings.DARAZ_APP_KEY and settings.DARAZ_APP_SECRET) and online,
            "ebay": bool(settings.EBAY_CLIENT_ID and settings.EBAY_CLIENT_SECRET and settings.EBAY_RU_NAME) and online,
            "ebay_marketplaces": [{"id": m.id, "name": m.name, "currency": m.currency} for m in ebay.MARKETPLACES.values()],
            "shopify_app_store_url": settings.SHOPIFY_APP_STORE_URL or None}


def _reachable(url: str) -> str:
    """URL as this container can reach it (localhost -> STORE_LOCALHOST_ALIAS in local dev)."""
    parts = urlsplit(url)
    if settings.STORE_LOCALHOST_ALIAS and parts.hostname in ("localhost", "127.0.0.1"):
        parts = parts._replace(netloc=settings.STORE_LOCALHOST_ALIAS + (f":{parts.port}" if parts.port else ""))
    return parts.geturl()


class DetectIn(BaseModel):
    store: str = Field(min_length=3, max_length=300)


@router.post("/connect/detect")
async def detect_store(data: DetectIn, user_id: uuid.UUID = Depends(current_user_id)):
    """The Stores page's single address box: which platform runs this site, and can we connect to it?"""
    try:
        found = await detect.detect(data.store, _http, allow_local=settings.ALLOW_HTTP_STORES,
                                    alias=settings.STORE_LOCALHOST_ALIAS)
    except detect.DetectError as exc:
        raise HTTPException(422, str(exc))
    log.info("store detect: %s -> %s", urlsplit(detect.normalize(data.store, allow_http=True)).hostname, found.platform)
    return found.out()


@router.post("/connect/custom")
async def start_custom(data: StartIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    """Smart Click and other sites that support Listing Agent: the seller types the WEBSITE address;
    the site tells us where its API is (/listing-agent/discover), and its admin approves on the site."""
    site = _woo_url(data.store)   # same normalising/https rules as WooCommerce
    r = await detect.safe_get(_http, f"{site}/listing-agent/discover", allow_local=settings.ALLOW_HTTP_STORES,
                              alias=settings.STORE_LOCALHOST_ALIAS)
    info = detect._json(r)
    api_url = str((info or {}).get("api_url") or "").rstrip("/")
    if not info or not re.match(r"^https?://", api_url):
        raise HTTPException(422, "This website doesn't support one-click connect yet. Use “Advanced” and enter its "
                                 "API address and key instead.")
    if api_url.lower().startswith("http://") and not settings.ALLOW_HTTP_STORES:
        raise HTTPException(422, "The store's API must use https://")
    name = (data.name or "").strip() or str(info.get("store_name") or "").strip()[:80] or urlsplit(site).hostname
    req = _new_request(db, user_id, "custom", name, api_url)
    path = str(info.get("connect_path") or "/listing-agent/connect")
    query = urlencode({
        "app": "Listing Agent", "state": str(req.id),
        "callback_url": f"{settings.api_base}/api/store/connect/custom/callback",
        "return_url": f"{settings.APP_URL.rstrip('/')}/stores?connect={req.id}",
    })
    return {"request_id": str(req.id), "authorize_url": f"{site}{path if path.startswith('/') else '/' + path}?{query}"}


@router.post("/connect/custom/callback")
async def custom_callback(request: Request, db: Session = Depends(get_db)):
    """Public. Trusted only via the one-time request id; the key is tested against the store
    before it's saved. Two ways in:
    - JSON from the store's backend (server to server) -> JSON answer;
    - a form the store admin's BROWSER submits after Approve -> redirect back to /stores. This is
      what makes one-click work while Listing Agent runs on a PC (localhost), which a hosted store
      backend can't reach but the admin's own browser can."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    from_browser = ctype == "application/x-www-form-urlencoded"
    if from_browser:
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        body = {k: v[0] for k, v in form.items() if v}
    else:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Expected JSON.")
    stores_page = f"{settings.APP_URL.rstrip('/')}/stores"

    req = _open_request(db, str(body.get("state") or ""), "custom")
    if req is None:
        if from_browser:
            return RedirectResponse(f"{stores_page}?connect_error=expired", status_code=303)
        raise HTTPException(400, "This connection link has expired — start again from Listing Agent.")
    back = f"{stores_page}?connect={req.id}"   # the Stores page shows the request's result
    key = str(body.get("api_key") or "").strip()
    if len(key) < 16:
        _finish(db, req, error="The store didn't send a valid key.")
        if from_browser:
            return RedirectResponse(back, status_code=303)
        raise HTTPException(400, "Invalid key.")
    creds = {"api_key": key}
    try:
        await _test("custom", req.store_url, creds)
    except ConnectionFailed as exc:
        _finish(db, req, error=str(exc))
        if from_browser:
            return RedirectResponse(back, status_code=303)
        raise HTTPException(400, str(exc))
    _finish(db, req, store=_save(db, req.user_id, "custom", req.name, req.store_url, creds, replace=True))
    if from_browser:
        return RedirectResponse(f"{back}&success=1", status_code=303)
    return {"ok": True}


@router.post("/connect/woocommerce")
def start_woocommerce(data: StartIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    """WooCommerce's built-in app approval (wc-auth/v1/authorize): the seller logs in to WordPress,
    clicks Approve, and WooCommerce sends new read/write keys to our callback."""
    url = _woo_url(data.store)
    req = _new_request(db, user_id, "woocommerce", (data.name or "").strip() or urlsplit(url).hostname, url)
    query = urlencode({
        "app_name": "Listing Agent",
        "scope": "read_write",
        "user_id": str(req.id),
        "return_url": f"{settings.APP_URL.rstrip('/')}/stores?connect={req.id}",
        "callback_url": f"{settings.api_base}/api/store/connect/woocommerce/callback",
    })
    return {"request_id": str(req.id), "authorize_url": f"{url}/wc-auth/v1/authorize?{query}"}


@router.post("/connect/woocommerce/callback")
async def woocommerce_callback(request: Request, db: Session = Depends(get_db)):
    """Public (WooCommerce's server calls it). Trusted only via the one-time request id it echoes back."""
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Expected JSON.")
    req = _open_request(db, str(body.get("user_id") or ""), "woocommerce")
    if req is None:
        raise HTTPException(400, "This connection link has expired — start again from Listing Agent.")
    creds = {"consumer_key": str(body.get("consumer_key") or ""), "consumer_secret": str(body.get("consumer_secret") or "")}
    if not creds["consumer_key"].startswith("ck_") or not creds["consumer_secret"].startswith("cs_"):
        _finish(db, req, error="WooCommerce didn't send valid keys.")
        raise HTTPException(400, "Invalid keys.")
    try:
        await _test("woocommerce", req.store_url, creds)
    except ConnectionFailed as exc:
        _finish(db, req, error=str(exc))
        raise HTTPException(400, str(exc))
    _finish(db, req, store=_save(db, req.user_id, "woocommerce", req.name, req.store_url, creds, replace=True))
    return {"ok": True}


# The website's Connect button: when Listing Agent's app is already installed, Shopify may skip its
# approval screen. So a Connect link started by someone else and opened by a merchant must not connect the
# merchant's shop to that other person's account. The callback therefore only parks the token and sends
# the browser back to the Stores page with a one-time code (in the URL #fragment, which never reaches a
# server). The connection is made only when the signed-in account that started it hands that code back.
# (Works whatever domains the website and API use — no cookies.)

@router.post("/connect/shopify")
async def start_shopify(data: StartIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    """Shopify OAuth: the seller approves Listing Agent's app in their Shopify admin. They can type the
    Shopify name (yourstore / yourstore.myshopify.com) or the store's own website (e.g. tentree.com)."""
    if not (settings.SHOPIFY_CLIENT_ID and settings.SHOPIFY_CLIENT_SECRET):
        raise HTTPException(503, "Shopify connections aren't set up on this server yet.")
    shop = await _shopify_from_address(data.store)
    req = _new_request(db, user_id, "shopify", (data.name or "").strip() or shop.split(".")[0], f"https://{shop}")
    query = urlencode({"client_id": settings.SHOPIFY_CLIENT_ID, "scope": settings.SHOPIFY_SCOPES,
                       "redirect_uri": f"{settings.api_base}/api/store/connect/shopify/callback", "state": str(req.id)})
    return {"request_id": str(req.id), "authorize_url": f"https://{shop}/admin/oauth/authorize?{query}"}


def shopify_hmac_ok(params: dict[str, str], secret: str) -> bool:
    given = params.get("hmac", "")
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in ("hmac", "signature"))
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return bool(given) and hmac.compare_digest(expected, given)


@router.get("/shopify/install")
def shopify_install(request: Request):
    """Public. Shopify's App URL: where a merchant lands after clicking Install in the Shopify App
    Store (and each time they open the app from their Shopify admin). We check Shopify's signature,
    then hand the shop to the web app's Stores page, which signs the seller in (or up) and OFFERS to
    connect that shop (they confirm with a click). Nothing is saved here. (The embedded app's App URL
    is the website itself; this older entry point just keeps old links working.)"""
    params = dict(request.query_params)
    stores_page = f"{settings.APP_URL.rstrip('/')}/stores"
    shop = params.get("shop", "").lower()
    if not (settings.SHOPIFY_CLIENT_SECRET and SHOP_RE.match(shop) and shopify_hmac_ok(params, settings.SHOPIFY_CLIENT_SECRET)):
        return RedirectResponse(stores_page, status_code=303)
    return RedirectResponse(f"{stores_page}?{urlencode({'shopify_install': shop})}", status_code=303)


@router.get("/connect/shopify/callback")
async def shopify_callback(request: Request, db: Session = Depends(get_db)):
    """Public (the seller's browser comes back here from Shopify). Parks the token; see above."""
    params = dict(request.query_params)
    app_url = settings.APP_URL.rstrip("/")
    req = _open_request(db, params.get("state", ""), "shopify")
    if req is None:
        return RedirectResponse(f"{app_url}/stores?connect_error=expired", status_code=303)
    back = f"{app_url}/stores?connect={req.id}"
    shop = params.get("shop", "").lower()
    if not shopify_hmac_ok(params, settings.SHOPIFY_CLIENT_SECRET) or not SHOP_RE.match(shop) \
            or f"https://{shop}" != req.store_url:
        _finish(db, req, error="Shopify's reply couldn't be verified. Try connecting again.")
        return RedirectResponse(back, status_code=303)
    if not params.get("code"):
        _finish(db, req, error="The connection was cancelled in Shopify.")
        return RedirectResponse(back, status_code=303)
    if (req.extra or {}).get("parked"):
        return RedirectResponse(back, status_code=303)       # this link was already used
    try:
        tokens = await shopify_api.code_exchange(_http, shop, settings.SHOPIFY_CLIENT_ID,
                                                 settings.SHOPIFY_CLIENT_SECRET, params["code"])
    except shopify_api.ShopifyError:
        _finish(db, req, error="Shopify didn't give access. Try connecting again.")
        return RedirectResponse(back, status_code=303)
    code = secrets.token_urlsafe(24)
    req.extra = {**(req.extra or {}), "parked": encrypt_credentials(tokens.as_credentials()),
                 "confirm": hashlib.sha256(code.encode()).hexdigest()}
    db.commit()
    return RedirectResponse(f"{back}#confirm={code}", status_code=303)


class ConfirmIn(BaseModel):
    code: str = Field(min_length=10, max_length=200)


@router.post("/connect/requests/{request_id}/confirm")
async def confirm_connect(request_id: uuid.UUID, data: ConfirmIn, user_id: uuid.UUID = Depends(current_user_id),
                          db: Session = Depends(get_db)):
    """The Stores page, signed in, hands back the one-time code from the #fragment. Only the account that
    started the Connect, in a browser that actually came back from Shopify, can finish it."""
    req = db.get(ConnectRequest, request_id)
    if not req or req.user_id != user_id or req.platform != "shopify":
        raise HTTPException(404, "Not found.")
    extra = req.extra or {}
    if req.status != "pending" or not extra.get("parked") or \
            not hmac.compare_digest(hashlib.sha256(data.code.encode()).hexdigest(), extra.get("confirm") or ""):
        raise HTTPException(409, "This connection link isn't valid any more — press Connect again.")
    if req.created_at < datetime.utcnow() - timedelta(minutes=REQUEST_MINUTES):
        _finish(db, req, error="That connection link expired. Please click Connect again.")
        raise HTTPException(409, "That connection link expired. Please click Connect again.")
    creds = decrypt_credentials(extra["parked"])
    req.extra = {k: v for k, v in extra.items() if k not in ("parked", "confirm")}   # the token lives only in the connection
    try:
        await _test("shopify", req.store_url, creds)
    except ConnectionFailed as exc:
        _finish(db, req, error=str(exc))
        raise HTTPException(422, str(exc))
    _finish(db, req, store=_save(db, req.user_id, "shopify", req.name, req.store_url, creds, replace=True,
                                 via_app=settings.SHOPIFY_CLIENT_ID))
    return connect_status(request_id, user_id, db)


# ---------------------------------------------------------------- Shopify: embedded app + expiring tokens
# Listing Agent's public Shopify app opens inside the Shopify admin. The page there sends us the
# ID token App Bridge gives it; we check it, make sure we hold a working offline token for the
# shop, and answer with a normal Listing Agent session (the shop's account, created on install).

SHOPIFY_RENEW_SECONDS = 300          # renew the 1-hour access token when under 5 minutes remain
SHOPIFY_REEXCHANGE_DAYS = 7          # swap for fresh tokens when the 90-day refresh token nears its end
SHOPIFY_REOPEN = ("Shopify access expired — open Listing Agent from your Shopify admin (Apps) "
                  "to renew it, or press Connect on Shopify again.")


def _shopify_ready() -> bool:
    return bool(settings.SHOPIFY_CLIENT_ID and settings.SHOPIFY_CLIENT_SECRET)


def _is_our_app(store: StoreConnection) -> bool:
    return bool(store.via_app) and store.via_app in (settings.SHOPIFY_CLIENT_ID, "*")


async def _fresh_shopify_credentials(db: Session, store: StoreConnection) -> dict:
    """Expiring Shopify tokens: renew with the refresh token when under 5 minutes remain. Refresh
    tokens rotate (each renewal kills the old one), so the renewal holds a row lock."""
    creds = decrypt_credentials(store.credentials_encrypted)
    if not creds.get("refresh_token") or int(creds.get("expires_at") or 0) - time.time() > SHOPIFY_RENEW_SECONDS:
        return creds                   # non-expiring token (own app / older connection) or still fresh
    locked = (db.query(StoreConnection).filter(StoreConnection.id == store.id)
              .populate_existing().with_for_update().one())
    creds = decrypt_credentials(locked.credentials_encrypted)
    if int(creds.get("expires_at") or 0) - time.time() > SHOPIFY_RENEW_SECONDS:
        db.commit()                    # renewed by another request while we waited
        return creds
    shop = shopify_api._host(locked.store_url)
    if int(creds.get("refresh_expires_at") or 0) <= time.time() or not _shopify_ready():
        locked.status, locked.last_error = "error", SHOPIFY_REOPEN
        db.commit()
        raise HTTPException(409, SHOPIFY_REOPEN)
    try:
        tokens = await shopify_api.refresh(_http, shop, settings.SHOPIFY_CLIENT_ID, settings.SHOPIFY_CLIENT_SECRET,
                                           creds["refresh_token"])
    except shopify_api.ShopifyError as exc:
        if exc.retryable:
            db.rollback()
            raise HTTPException(503, "Shopify isn't answering right now — try again in a minute.")
        locked.status, locked.last_error = "error", SHOPIFY_REOPEN
        db.commit()
        raise HTTPException(409, SHOPIFY_REOPEN)
    creds = {**creds, **tokens.as_credentials(settings.SHOPIFY_CLIENT_ID)}
    locked.credentials_encrypted = encrypt_credentials(creds)
    locked.status, locked.last_error = "active", None
    db.commit()
    return creds


class ShopifySessionIn(BaseModel):
    id_token: str = Field(min_length=20, max_length=4000)


async def _auth_session(shop: str, *, create: bool, info: dict | None = None) -> dict | None:
    """The shop's own Listing Agent account (auth). create=False: None if the shop has none yet."""
    try:
        r = await _http.post(f"{settings.AUTH_URL}/internal/shopify/session", headers=outgoing_headers(settings.INTERNAL_TOKEN),
                             json={"shop": shop, "create": create, **(info or {})})
    except httpx.HTTPError:
        raise HTTPException(503, "Couldn't sign you in right now — reload the app in a moment.")
    if r.status_code == 404 and not create:
        return None
    if r.status_code >= 400:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
        raise HTTPException(503 if r.status_code >= 500 else r.status_code, detail or "Couldn't sign you in.")
    return r.json()


async def _shop_info(shop: str, access_token: str) -> dict:
    """Name, contact email and market for a new account. Best effort: defaults if Shopify is slow."""
    try:
        data = await shopify_api.graphql(_http, shop, access_token, shopify_api.SHOP_INFO)
    except shopify_api.ShopifyError as exc:
        log.warning("shop info for %s unavailable: %s", shop, exc)
        return {}
    s = data.get("shop") or {}
    return {"name": s.get("name"), "email": s.get("contactEmail") or s.get("email"),
            "country": (s.get("billingAddress") or {}).get("countryCodeV2"), "currency": s.get("currencyCode")}


def _save_shop_tokens(db: Session, user_id, name: str, store_url: str, tokens: "shopify_api.Tokens") -> StoreConnection:
    creds = tokens.as_credentials(settings.SHOPIFY_CLIENT_ID)
    try:
        return _save(db, user_id, "shopify", name, store_url, creds, replace=True, via_app=settings.SHOPIFY_CLIENT_ID)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        # The app was opened twice at the same moment and the other request just added the row: update it.
        return _save(db, user_id, "shopify", name, store_url, creds, replace=True, via_app=settings.SHOPIFY_CLIENT_ID)


@router.post("/shopify/session")
async def shopify_session(data: ShopifySessionIn, db: Session = Depends(get_db)):
    """Public (the embedded app calls it with App Bridge's ID token, which proves the shop).
    Always the shop's OWN account: whoever manages the shop in Shopify works in it — never in some
    other Listing Agent account that also connected the shop from our website."""
    if not _shopify_ready():
        raise HTTPException(503, "Shopify isn't set up on this server yet.")
    try:
        claims = shopify_api.verify_id_token(data.id_token, settings.SHOPIFY_CLIENT_ID, settings.SHOPIFY_CLIENT_SECRET)
    except shopify_api.ShopifyError:
        raise HTTPException(401, "Couldn't confirm your Shopify store — reload Listing Agent in Shopify.")
    shop = claims["shop"]
    store_url = f"https://{shop}"
    account = await _auth_session(shop, create=False)
    conn = None
    if account:
        conn = db.query(StoreConnection).filter_by(user_id=uuid.UUID(account["user"]["id"]), platform="shopify",
                                                    store_url=store_url).first()
    creds = decrypt_credentials(conn.credentials_encrypted) if conn else {}
    now = time.time()
    usable = (conn is not None and conn.status == "active" and _is_our_app(conn) and bool(creds.get("access_token"))
              and (not creds.get("refresh_token")
                   or int(creds.get("refresh_expires_at") or 0) - now > SHOPIFY_REEXCHANGE_DAYS * 86400))
    tokens = None
    if not usable:
        try:
            tokens = await shopify_api.token_exchange(_http, shop, settings.SHOPIFY_CLIENT_ID,
                                                      settings.SHOPIFY_CLIENT_SECRET, data.id_token)
        except shopify_api.ShopifyError as exc:
            if exc.retryable:
                raise HTTPException(503, "Shopify isn't answering right now — reload the app in a minute.")
            raise HTTPException(401, "Shopify didn't give access — reload Listing Agent in Shopify.")
    info = {}
    if account is None:            # first install: an account named after the shop
        info = await _shop_info(shop, tokens.access_token)
        account = await _auth_session(shop, create=True, info=info)
    if tokens is not None:
        name = (info.get("name") or (conn.name if conn else "") or shop.split(".")[0])[:80]
        _save_shop_tokens(db, uuid.UUID(account["user"]["id"]), name, store_url, tokens)
    return {"access_token": account["access_token"], "token_type": "bearer", "expires_in": account["expires_in"],
            "user": account["user"], "shop": shop, "created": bool(account.get("created"))}


@router.get("/internal/shopify/billing-token")
async def shopify_billing_token(user_id: uuid.UUID = Query(...), shop: str = Query(...), db: Session = Depends(get_db)):
    """Internal (billing): a working token of OUR app for the user's shop — Shopify Billing charges
    go through the app that asks for them, so a shop connected with its own keys can't be used."""
    shop = shop.lower()
    if not SHOP_RE.match(shop):
        raise HTTPException(422, "Not a Shopify store.")
    store = db.query(StoreConnection).filter_by(user_id=user_id, platform="shopify", store_url=f"https://{shop}").first()
    if not store or store.status == "disconnected" or store.via_app != settings.SHOPIFY_CLIENT_ID:
        raise HTTPException(409, "Open Listing Agent from your Shopify admin to buy credits with Shopify.")
    creds = await _fresh_shopify_credentials(db, store)
    return {"shop": shop, "access_token": creds["access_token"]}


# ---------------------------------------------------------------- Daraz

def _daraz() -> daraz.DarazClient:
    return daraz.DarazClient(settings.DARAZ_APP_KEY, settings.DARAZ_APP_SECRET, api_url=settings.DARAZ_API_URL,
                             auth_url=settings.DARAZ_AUTH_URL, http=_http)


@router.post("/connect/daraz")
def start_daraz(data: StartIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    """Daraz seller login: the seller signs in to Daraz Seller Center and clicks Authorize."""
    if not (settings.DARAZ_APP_KEY and settings.DARAZ_APP_SECRET):
        raise HTTPException(503, "Daraz connections aren't set up on this server yet.")
    req = _new_request(db, user_id, "daraz", (data.name or "").strip() or "Daraz shop", "https://www.daraz.pk")
    url = daraz.authorize_url(settings.DARAZ_APP_KEY, f"{settings.api_base}/api/store/connect/daraz/callback",
                              str(req.id), settings.DARAZ_AUTH_PAGE)
    return {"request_id": str(req.id), "authorize_url": url}


@router.get("/connect/daraz/callback")
async def daraz_callback(request: Request, db: Session = Depends(get_db)):
    """Public (the seller's browser comes back here from Daraz with ?code=…&state=…)."""
    params = dict(request.query_params)
    app_url = settings.APP_URL.rstrip("/")
    req = _open_request(db, params.get("state", ""), "daraz")
    if req is None:
        return RedirectResponse(f"{app_url}/stores?connect_error=expired", status_code=303)
    back = f"{app_url}/stores?connect={req.id}"
    if not params.get("code"):
        _finish(db, req, error="The connection was cancelled in Daraz.")
        return RedirectResponse(back + "&denied=1", status_code=303)
    try:
        tokens = await _daraz().create_token(params["code"])
    except daraz.DarazError as exc:
        _finish(db, req, error=f"Daraz didn't give access: {exc}")
        return RedirectResponse(back, status_code=303)
    creds = tokens.as_credentials()
    # One connection per Daraz seller account.
    store_url = f"https://www.daraz.pk/shop/{tokens.short_code}" if tokens.short_code \
        else f"https://sellercenter.daraz.pk/?seller={tokens.seller_id or tokens.account}"
    name = req.name if req.name != "Daraz shop" else (tokens.account or "Daraz shop")
    try:
        await _test("daraz", store_url, creds)
    except ConnectionFailed as exc:
        _finish(db, req, error=str(exc))
        return RedirectResponse(back, status_code=303)
    _finish(db, req, store=_save(db, req.user_id, "daraz", name, store_url, creds, replace=True))
    return RedirectResponse(back, status_code=303)


DARAZ_RENEW_DAYS = 5


async def _fresh_daraz_credentials(db: Session, store: StoreConnection) -> dict:
    """Daraz access lasts 30 days: renew it (with the 180-day refresh token) when fewer than
    DARAZ_RENEW_DAYS remain, and save the new pair. When the refresh token is gone too, the
    seller has to press Connect again."""
    creds = decrypt_credentials(store.credentials_encrypted)
    now = int(time.time())
    if int(creds.get("expires_at") or 0) - now > DARAZ_RENEW_DAYS * 86400:
        return creds
    if int(creds.get("refresh_expires_at") or 0) <= now:
        store.status, store.last_error = "error", "Daraz access expired — press Connect on Daraz again."
        db.commit()
        raise HTTPException(409, store.last_error)
    try:
        tokens = await _daraz().refresh_token(creds.get("refresh_token", ""))
    except daraz.DarazError as exc:
        if exc.retryable:
            return creds   # still valid for a few days; try again next time
        store.status, store.last_error = "error", "Daraz access expired — press Connect on Daraz again."
        db.commit()
        raise HTTPException(409, store.last_error)
    new = {**creds, **{k: v for k, v in tokens.as_credentials().items() if v}}
    store.credentials_encrypted = encrypt_credentials(new)
    store.status, store.last_error = "active", None
    db.commit()
    log.info("renewed Daraz access for store %s", store.id)
    return new


# ---------------------------------------------------------------- eBay

def _ebay() -> ebay.EbayAuth:
    return ebay.EbayAuth(settings.EBAY_CLIENT_ID, settings.EBAY_CLIENT_SECRET, settings.EBAY_RU_NAME,
                         api=settings.EBAY_API_URL, http=_http)


@router.post("/connect/ebay")
def start_ebay(data: StartIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    """eBay seller login. The seller picks the eBay site and where items ship from, then signs in to eBay."""
    if not (settings.EBAY_CLIENT_ID and settings.EBAY_CLIENT_SECRET and settings.EBAY_RU_NAME):
        raise HTTPException(503, "eBay connections aren't set up on this server yet.")
    m = ebay.MARKETPLACES.get((data.marketplace or "").upper())
    if not m:
        raise HTTPException(422, "Pick your eBay site.")
    city, postal = (data.city or "").strip(), (data.postal_code or "").strip()
    if not city or not postal:
        raise HTTPException(422, "Enter the city and postal code your items ship from — eBay shows it to buyers.")
    req = _new_request(db, user_id, "ebay", (data.name or "").strip() or m.name, m.site)
    req.extra = {"marketplace": m.id, "city": city, "postal_code": postal}
    db.commit()
    return {"request_id": str(req.id),
            "authorize_url": ebay.authorize_url(settings.EBAY_CLIENT_ID, settings.EBAY_RU_NAME, str(req.id),
                                                settings.EBAY_AUTH_URL)}


@router.get("/connect/ebay/callback")
async def ebay_callback(request: Request, db: Session = Depends(get_db)):
    """Public (eBay sends the seller's browser here: the RuName's "accept" and "decline" URLs)."""
    params = dict(request.query_params)
    app_url = settings.APP_URL.rstrip("/")
    req = _open_request(db, params.get("state", ""), "ebay")
    if req is None:
        return RedirectResponse(f"{app_url}/stores?connect_error=expired", status_code=303)
    back = f"{app_url}/stores?connect={req.id}"
    if not params.get("code"):
        _finish(db, req, error="The connection was cancelled in eBay.")
        return RedirectResponse(back + "&denied=1", status_code=303)
    auth = _ebay()
    try:
        tokens = await auth.create_token(params["code"])
        r = await _http.get(f"{settings.EBAY_IDENTITY_URL}/commerce/identity/v1/user/",
                            headers={"Authorization": f"Bearer {tokens.access_token}"})
        who = r.json() if r.status_code == 200 else {}
    except (ebay.EbayError, httpx.HTTPError, ValueError) as exc:
        _finish(db, req, error=f"eBay didn't give access: {exc}")
        return RedirectResponse(back, status_code=303)
    extra = req.extra or {}
    m = ebay.MARKETPLACES[extra.get("marketplace", "EBAY_US")]
    username = str(who.get("username") or "")
    creds = {"access_token": tokens.access_token, "refresh_token": tokens.refresh_token,
             "expires_at": str(tokens.expires_at), "refresh_expires_at": str(tokens.refresh_expires_at),
             "marketplace": m.id, "user_id": str(who.get("userId") or ""), "username": username,
             "city": extra.get("city", ""), "postal_code": extra.get("postal_code", "")}
    store_url = f"{m.site}/usr/{username}" if username else m.site
    name = req.name if req.name != m.name else (f"{username} ({m.name})" if username else m.name)
    try:
        await _test("ebay", store_url, creds)
    except ConnectionFailed as exc:
        _finish(db, req, error=str(exc))
        return RedirectResponse(back, status_code=303)
    _finish(db, req, store=_save(db, req.user_id, "ebay", name, store_url, creds, replace=True))
    return RedirectResponse(back, status_code=303)


async def _fresh_ebay_credentials(db: Session, store: StoreConnection) -> dict:
    """eBay access lasts 2 hours: renew it with the (18-month) refresh token when under 10 minutes remain."""
    creds = decrypt_credentials(store.credentials_encrypted)
    now = int(time.time())
    if int(creds.get("expires_at") or 0) - now > 600:
        return creds
    if int(creds.get("refresh_expires_at") or 0) <= now:
        store.status, store.last_error = "error", "eBay access expired — press Connect on eBay again."
        db.commit()
        raise HTTPException(409, store.last_error)
    try:
        token, expires_at = await _ebay().refresh(creds.get("refresh_token", ""))
    except ebay.EbayError as exc:
        if exc.retryable:
            raise HTTPException(503, "eBay isn't answering right now — try again in a minute.")
        store.status, store.last_error = "error", "eBay access expired — press Connect on eBay again."
        db.commit()
        raise HTTPException(409, store.last_error)
    creds.update(access_token=token, expires_at=str(expires_at))
    store.credentials_encrypted = encrypt_credentials(creds)
    store.status, store.last_error = "active", None
    db.commit()
    return creds


def ebay_challenge_response(challenge_code: str, verification_token: str, endpoint: str) -> str:
    """eBay checks the deletion endpoint with SHA-256(challengeCode + verificationToken + endpoint), hex."""
    return hashlib.sha256((challenge_code + verification_token + endpoint).encode()).hexdigest()


@router.get("/ebay/account-deletion")
def ebay_deletion_check(challenge_code: str = Query(...)):
    """Public. eBay's one-time check that this endpoint belongs to us (set in the developer portal)."""
    if not settings.EBAY_VERIFICATION_TOKEN:
        raise HTTPException(503, "Not configured.")
    endpoint = f"{settings.api_base}/api/store/ebay/account-deletion"
    return {"challengeResponse": ebay_challenge_response(challenge_code, settings.EBAY_VERIFICATION_TOKEN, endpoint)}


@router.post("/ebay/account-deletion")
async def ebay_account_deleted(request: Request, db: Session = Depends(get_db)):
    """Public. An eBay member closed their account: forget their eBay connection(s).
    (Anyone can only ever cause a disconnect here, which the seller can undo by connecting again.)"""
    try:
        body = await request.json()
    except ValueError:
        return {"ok": True}
    data = ((body or {}).get("notification") or {}).get("data") or {}
    username, user_ref = str(data.get("username") or ""), str(data.get("userId") or "")
    removed = 0
    if username or user_ref:
        for store in db.query(StoreConnection).filter_by(platform="ebay").all():
            if (username and store.store_url.endswith(f"/usr/{username}")) or \
                    (user_ref and decrypt_credentials(store.credentials_encrypted).get("user_id") == user_ref):
                db.delete(store)
                removed += 1
        db.commit()
    log.info("eBay account deletion: removed %d connection(s)", removed)
    return {"ok": True}


# ---------------------------------------------------------------- Shopify webhooks
# Configure in the Shopify Dev Dashboard (app version):
#   Compliance webhooks (customers/data_request, customers/redact, shop/redact) and the
#   app/uninstalled subscription, all to  <PUBLIC_BASE_URL>/api/store/shopify/webhooks

def shopify_webhook_ok(raw_body: bytes, header_hmac: str, secret: str) -> bool:
    """Shopify signs webhooks with base64(HMAC-SHA256(raw body, app client secret))."""
    if not (secret and header_hmac):
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, header_hmac.strip())


@router.post("/shopify/webhooks")
async def shopify_webhooks(request: Request, db: Session = Depends(get_db)):
    """Public (Shopify calls it). Listing Agent never requests customer or order data (see
    SHOPIFY_SCOPES), so customer requests have nothing to return or erase; a shop's data is our
    saved connection, which is removed on shop/redact and disconnected on app/uninstalled."""
    raw = await request.body()
    if not shopify_webhook_ok(raw, request.headers.get("x-shopify-hmac-sha256", ""), settings.SHOPIFY_CLIENT_SECRET):
        raise HTTPException(401, "Invalid webhook signature.")
    topic = request.headers.get("x-shopify-topic", "")
    shop = request.headers.get("x-shopify-shop-domain", "").lower()
    if not shop:
        try:
            shop = str((await request.json()).get("shop_domain") or "").lower()
        except ValueError:
            shop = ""
    store_url = f"https://{shop}" if SHOP_RE.match(shop) else None
    conns = db.query(StoreConnection).filter_by(platform="shopify", store_url=store_url).all() if store_url else []

    if topic in ("customers/data_request", "customers/redact"):
        log.info("shopify %s for %s: no customer data is stored", topic, shop)
    elif topic == "shop/redact":
        for c in conns:
            db.delete(c)
        if store_url:
            db.query(ConnectRequest).filter_by(platform="shopify", store_url=store_url).delete()
        db.commit()
        log.info("shopify shop/redact for %s: removed %d connection(s)", shop, len(conns))
    elif topic == "app/uninstalled":
        # Only connections made through Listing Agent's app die with it; a shop connected with its
        # own app's keys (Advanced) keeps working.
        conns = [c for c in conns if _is_our_app(c)]
        for c in conns:
            c.status = "disconnected"
            c.last_error = "Listing Agent was uninstalled in Shopify. Connect again to publish."
            c.credentials_encrypted = encrypt_credentials({})   # the token is dead; don't keep it
        db.commit()
        log.info("shopify app/uninstalled for %s: disconnected %d connection(s)", shop, len(conns))
    else:
        log.info("shopify webhook %s ignored", topic)
    return {"ok": True}


@router.get("/connect/requests/{request_id}")
def connect_status(request_id: uuid.UUID, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    req = db.get(ConnectRequest, request_id)
    if not req or req.user_id != user_id:
        raise HTTPException(404, "Not found.")
    status = req.status
    if status == "pending" and req.created_at < datetime.utcnow() - timedelta(minutes=REQUEST_MINUTES):
        status = "failed"
    return {"id": str(req.id), "platform": req.platform, "status": status, "error": req.error,
            "store_url": req.store_url, "name": req.name}


@router.delete("/stores/{store_id}", status_code=204)
def disconnect_store(store_id: uuid.UUID, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    store = db.get(StoreConnection, store_id)
    if not store or store.user_id != user_id:
        raise HTTPException(404, "Store not found.")
    db.delete(store)  # credentials are gone with the row
    db.commit()


@router.get("/internal/stores/{store_id}/credentials")
async def credentials_for_publisher(store_id: uuid.UUID, user_id: uuid.UUID = Query(...), db: Session = Depends(get_db)):
    """Internal only (gateway never routes /internal). Checks the store belongs to the user."""
    store = db.get(StoreConnection, store_id)
    if not store or store.user_id != user_id:
        raise HTTPException(404, "Store not found.")
    if store.status == "disconnected":
        raise HTTPException(409, "Store is disconnected.")
    if store.platform == "daraz":
        creds = await _fresh_daraz_credentials(db, store)
    elif store.platform == "ebay":
        creds = await _fresh_ebay_credentials(db, store)
    elif store.platform == "shopify":
        # the publisher only needs the access token; the refresh token never leaves this service
        creds = {k: v for k, v in (await _fresh_shopify_credentials(db, store)).items()
                 if k not in ("refresh_token", "refresh_expires_at")}
    else:
        creds = decrypt_credentials(store.credentials_encrypted)
    return {"platform": store.platform, "store_url": store.store_url, "credentials": creds}


app = create_service("store", routers=[router], engine=engine,
                     migrations_dir=Path(__file__).parent.parent / "migrations")
