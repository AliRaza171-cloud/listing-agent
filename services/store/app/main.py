"""Store service: a seller's connected stores and their encrypted credentials.

It never talks to Shopify/WooCommerce itself — connection tests go to publisher
(contracts/README.md §2.2). Credentials leave this service only through
GET /internal/stores/{id}/credentials, which the gateway never routes.
"""
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
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


settings = Settings()
engine, SessionLocal, get_db = make_db(settings.DATABASE_URL)
Base = declarative_base()
_http = httpx.AsyncClient(timeout=30)

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


@router.post("/stores", status_code=201)
async def connect_store(data: ConnectIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    if data.store_url.lower().startswith("http://") and not settings.ALLOW_HTTP_STORES:
        raise HTTPException(422, "The store address must start with https://")
    missing = REQUIRED_CREDENTIALS[data.platform] - set(data.credentials)
    if missing:
        raise HTTPException(422, f"Missing: {', '.join(sorted(missing))}")
    store_url = data.store_url.rstrip("/")

    test = await _http.post(
        f"{settings.PUBLISHER_URL}/internal/connectors/test",
        json={"platform": data.platform, "store_url": store_url, "credentials": data.credentials},
        headers=outgoing_headers(settings.INTERNAL_TOKEN),
    )
    if test.status_code == 422:
        raise HTTPException(422, test.json().get("detail", "The store rejected these details."))
    if test.status_code == 501:
        raise HTTPException(422, f"Publishing to {data.platform} isn't available yet.")
    test.raise_for_status()

    store = StoreConnection(user_id=user_id, platform=data.platform, name=data.name, store_url=store_url,
                            credentials_encrypted=encrypt_credentials(data.credentials))
    db.add(store)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This store is already connected.")
    db.refresh(store)
    return _public(store)


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
