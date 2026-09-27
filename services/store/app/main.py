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
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import Column, DateTime, Enum, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, declarative_base

from lagent_common.correlation import outgoing_headers
from lagent_common.db import make_db
from lagent_common.internal import current_user_id, require_internal
from lagent_common.service import create_service

from app.crypto import decrypt_credentials, encrypt_credentials


class Settings(BaseSettings):
    DATABASE_URL: str
    INTERNAL_TOKEN: str
    PUBLISHER_URL: str = "http://publisher:8000"
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
    "shopify": {"access_token"},
}


class StoreConnection(Base):
    __tablename__ = "store_connections"
    __table_args__ = (UniqueConstraint("user_id", "platform", "store_url"),)
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    platform = Column(Enum("custom", "woocommerce", "shopify", name="store_platform", create_type=False), nullable=False)
    name = Column(String, nullable=False)
    store_url = Column(String, nullable=False)
    credentials_encrypted = Column(String, nullable=False)
    status = Column(Enum("active", "error", "disconnected", name="connection_status", create_type=False),
                    default="active", nullable=False)
    last_error = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class ConnectRequest(Base):
    __tablename__ = "connect_requests"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    platform = Column(Enum("custom", "woocommerce", "shopify", name="store_platform", create_type=False), nullable=False)
    name = Column(String, nullable=False)
    store_url = Column(String, nullable=False)
    status = Column(String, default="pending", nullable=False)
    error = Column(String)
    store_id = Column(UUID(as_uuid=True))
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
          replace: bool) -> StoreConnection:
    """Creates the connection, or (replace=True, used by one-click reconnects) refreshes its keys."""
    existing = db.query(StoreConnection).filter_by(user_id=user_id, platform=platform, store_url=store_url).first()
    if existing and not replace and existing.status != "disconnected":
        raise HTTPException(409, "This store is already connected.")
    store = existing or StoreConnection(user_id=user_id, platform=platform, store_url=store_url)
    store.name = name
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
    missing = REQUIRED_CREDENTIALS[data.platform] - set(data.credentials)
    if missing:
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
    store: str = Field(min_length=3, max_length=300)   # WooCommerce site address, or Shopify store name
    name: str | None = Field(default=None, max_length=80)


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


def _shop_domain(raw: str) -> str:
    shop = re.sub(r"^https?://", "", raw.strip().lower()).split("/")[0]
    if "." not in shop:
        shop += ".myshopify.com"
    if not SHOP_RE.match(shop):
        raise HTTPException(422, "Enter your Shopify store name, e.g. yourstore (from yourstore.myshopify.com)")
    return shop


@router.get("/connect/options")
def connect_options():
    return {"shopify": bool(settings.SHOPIFY_CLIENT_ID and settings.SHOPIFY_CLIENT_SECRET), "woocommerce": True}


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


@router.post("/connect/shopify")
def start_shopify(data: StartIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    """Shopify OAuth: the seller approves Listing Agent's app in their Shopify admin."""
    if not (settings.SHOPIFY_CLIENT_ID and settings.SHOPIFY_CLIENT_SECRET):
        raise HTTPException(503, "Shopify connections aren't set up on this server yet.")
    shop = _shop_domain(data.store)
    req = _new_request(db, user_id, "shopify", (data.name or "").strip() or shop.split(".")[0], f"https://{shop}")
    query = urlencode({"client_id": settings.SHOPIFY_CLIENT_ID, "scope": settings.SHOPIFY_SCOPES,
                       "redirect_uri": f"{settings.api_base}/api/store/connect/shopify/callback", "state": str(req.id)})
    return {"request_id": str(req.id), "authorize_url": f"https://{shop}/admin/oauth/authorize?{query}"}


def shopify_hmac_ok(params: dict[str, str], secret: str) -> bool:
    given = params.get("hmac", "")
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k not in ("hmac", "signature"))
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return bool(given) and hmac.compare_digest(expected, given)


@router.get("/connect/shopify/callback")
async def shopify_callback(request: Request, db: Session = Depends(get_db)):
    """Public (the seller's browser comes back here from Shopify)."""
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
    try:
        r = await _http.post(f"https://{shop}/admin/oauth/access_token", json={
            "client_id": settings.SHOPIFY_CLIENT_ID, "client_secret": settings.SHOPIFY_CLIENT_SECRET,
            "code": params["code"]})
        token = r.json().get("access_token") if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        token = None
    if not token:
        _finish(db, req, error="Shopify didn't give access. Try connecting again.")
        return RedirectResponse(back, status_code=303)
    creds = {"access_token": token}
    try:
        await _test("shopify", req.store_url, creds)
    except ConnectionFailed as exc:
        _finish(db, req, error=str(exc))
        return RedirectResponse(back, status_code=303)
    _finish(db, req, store=_save(db, req.user_id, "shopify", req.name, req.store_url, creds, replace=True))
    return RedirectResponse(back, status_code=303)


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
def credentials_for_publisher(store_id: uuid.UUID, user_id: uuid.UUID = Query(...), db: Session = Depends(get_db)):
    """Internal only (gateway never routes /internal). Checks the store belongs to the user."""
    store = db.get(StoreConnection, store_id)
    if not store or store.user_id != user_id:
        raise HTTPException(404, "Store not found.")
    if store.status == "disconnected":
        raise HTTPException(409, "Store is disconnected.")
    return {"platform": store.platform, "store_url": store.store_url,
            "credentials": decrypt_credentials(store.credentials_encrypted)}


app = create_service("store", routers=[router], engine=engine,
                     migrations_dir=Path(__file__).parent.parent / "migrations")
