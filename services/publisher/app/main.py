"""Publisher service — the only service that talks to outside stores.

- consumes publish.requested -> creates/updates the product in the store
  -> emits publish.succeeded / publish.failed
- temporary store errors are retried (up to MAX_ATTEMPTS, via the bus's redelivery)
- /internal/connectors/test and /internal/stores/{id}/categories for store & catalog
"""
import dataclasses
import logging
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from sqlalchemy import Column, DateTime, Enum, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base

from lagent_common.bus import EventBus
from lagent_common.correlation import outgoing_headers
from lagent_common.db import make_db
from lagent_common.internal import require_internal
from lagent_common.service import create_service

from app.connectors import ConnectorError, ConnectorNotBuilt, get_connector
from app.connectors.base import ImageFile, ProductPayload

log = logging.getLogger("lagent.publisher")


class Settings(BaseSettings):
    DATABASE_URL: str
    REDIS_URL: str = "redis://redis:6379/0"
    INTERNAL_TOKEN: str
    STORE_URL: str = "http://store:8000"
    CATALOG_URL: str = "http://catalog:8000"
    MAX_ATTEMPTS: int = 3


settings = Settings()
engine, SessionLocal, get_db = make_db(settings.DATABASE_URL)
bus = EventBus(settings.REDIS_URL, "publisher")
Base = declarative_base()
_http = httpx.AsyncClient(timeout=15)


class PublishJob(Base):
    __tablename__ = "publish_jobs"
    id = Column(UUID(as_uuid=True), primary_key=True)
    product_id = Column(UUID(as_uuid=True), nullable=False)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    store_connection_id = Column(UUID(as_uuid=True), nullable=False)
    mode = Column(String, default="draft", nullable=False)
    status = Column(Enum("publishing", "published", "failed", name="publish_status", create_type=False),
                    default="publishing", nullable=False)
    external_id = Column(String)
    external_url = Column(String)
    error = Column(String)
    attempts = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


async def _store_credentials(store_id: str, user_id: str) -> dict:
    r = await _http.get(f"{settings.STORE_URL}/internal/stores/{store_id}/credentials",
                        params={"user_id": user_id}, headers=outgoing_headers(settings.INTERNAL_TOKEN))
    if r.status_code in (404, 409):
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        raise ConnectorError(detail if r.status_code == 409 and isinstance(detail, str) else "This store is no longer connected.")
    r.raise_for_status()
    return r.json()


async def _load_images(urls: list[str]) -> list[ImageFile]:
    """Product photos live in catalog (/api/catalog/media/<name>). Stores can't reach that,
    so publisher fetches the bytes over the internal network and uploads them."""
    files = []
    for url in urls:
        if url.startswith("/api/catalog/"):
            target, headers = f"{settings.CATALOG_URL}/{url[len('/api/catalog/'):]}", outgoing_headers(settings.INTERNAL_TOKEN)
        else:
            target, headers = url, {}
        r = await _http.get(target, headers=headers)
        if r.status_code == 404:
            continue  # photo was removed; publish the rest
        r.raise_for_status()
        name = url.rsplit("/", 1)[-1] or "photo.jpg"
        files.append(ImageFile(filename=name, data=r.content,
                               content_type=r.headers.get("content-type", "image/jpeg").split(";")[0]))
    return files


async def on_publish_requested(event: dict) -> None:
    """Idempotent by publish_job_id: the job row *is* the processed marker, and a
    finished job is never re-run. A retryable failure raises so the bus redelivers."""
    data = event["data"]
    ids = {k: data[k] for k in ("publish_job_id", "product_id", "user_id", "store_connection_id")}
    job_id = uuid.UUID(data["publish_job_id"])

    with SessionLocal() as db:
        job = db.get(PublishJob, job_id)
        if job is None:
            job = PublishJob(id=job_id, product_id=uuid.UUID(data["product_id"]), user_id=uuid.UUID(data["user_id"]),
                             store_connection_id=uuid.UUID(data["store_connection_id"]), mode=data["mode"],
                             status="publishing", attempts=0)  # column defaults only apply on INSERT
            db.add(job)
        if job.status != "publishing":
            return  # already finished (duplicate delivery)
        previous = (db.query(PublishJob)
                    .filter(PublishJob.product_id == job.product_id,
                            PublishJob.store_connection_id == job.store_connection_id,
                            PublishJob.status == "published", PublishJob.external_id.isnot(None))
                    .order_by(PublishJob.created_at.desc()).first())
        job.attempts += 1
        db.commit()
        attempts, existing_id = job.attempts, previous.external_id if previous else None

    try:
        store = await _store_credentials(data["store_connection_id"], data["user_id"])
        connector = get_connector(store["platform"], store["store_url"], store["credentials"])
        known = {f.name for f in dataclasses.fields(ProductPayload)}
        payload = ProductPayload(**{k: v for k, v in data["product"].items() if k in known},
                                 publish_live=data["mode"] == "live")
        payload.image_files = await _load_images(payload.image_urls)
        result = (await connector.update_product(existing_id, payload) if existing_id
                  else await connector.create_product(payload))
    except (ConnectorError, ConnectorNotBuilt, httpx.HTTPError) as exc:
        retryable = isinstance(exc, httpx.HTTPError) or getattr(exc, "retryable", False)
        if retryable and attempts < settings.MAX_ATTEMPTS:
            raise  # bus redelivers after its idle timeout
        reason = str(exc) if isinstance(exc, (ConnectorError, ConnectorNotBuilt)) else "The store didn't respond — try again later."
        with SessionLocal() as db:
            job = db.get(PublishJob, job_id)
            job.status, job.error = "failed", reason
            db.commit()
        await bus.publish("publish.failed", {**ids, "reason": reason, "retryable": retryable})
        return

    with SessionLocal() as db:
        job = db.get(PublishJob, job_id)
        job.status, job.external_id, job.external_url, job.error = "published", result.external_id, result.external_url, None
        db.commit()
    await bus.publish("publish.succeeded", {**ids, "external_id": result.external_id, "external_url": result.external_url})


bus.subscribe("publish.requested", on_publish_requested)


class TestIn(BaseModel):
    platform: str
    store_url: str
    credentials: dict[str, str]


router = APIRouter(dependencies=[Depends(require_internal)])


@router.post("/internal/connectors/test", status_code=204)
async def test_connection(data: TestIn):
    try:
        await get_connector(data.platform, data.store_url, data.credentials).test_connection()
    except ConnectorNotBuilt as exc:
        raise HTTPException(501, str(exc))
    except ConnectorError as exc:
        raise HTTPException(422, str(exc))


@router.get("/internal/stores/{store_id}/categories")
async def store_categories(store_id: uuid.UUID, user_id: uuid.UUID = Query(...)):
    try:
        store = await _store_credentials(str(store_id), str(user_id))
        cats = await get_connector(store["platform"], store["store_url"], store["credentials"]).list_categories()
    except ConnectorNotBuilt:
        return []
    except ConnectorError as exc:
        raise HTTPException(422, str(exc))
    return [{"id": c.id, "name": c.name} for c in cats]


app = create_service("publisher", routers=[router], bus=bus, engine=engine,
                     migrations_dir=Path(__file__).parent.parent / "migrations")
