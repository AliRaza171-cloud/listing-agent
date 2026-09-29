"""Notification service: turns events into in-app notifications and emails.

Knows nothing about products or stores beyond what events tell it; keeps its own
copy of each user's email (from user.registered) so it never calls auth.
"""
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic_settings import BaseSettings
from sqlalchemy import text
from sqlalchemy.orm import Session

from lagent_common.bus import EventBus
from lagent_common.db import make_db, mark_processed
from lagent_common.internal import current_user_id, require_internal
from lagent_common.service import create_service

log = logging.getLogger("lagent.notification")


class Settings(BaseSettings):
    DATABASE_URL: str
    REDIS_URL: str = "redis://redis:6379/0"
    EMAIL_PROVIDER: str = "log"   # "log" = print instead of sending (development)
    APP_URL: str = "http://localhost:3000"


settings = Settings()
engine, SessionLocal, get_db = make_db(settings.DATABASE_URL)
bus = EventBus(settings.REDIS_URL, "notification")


async def send_email(to: str, subject: str, body: str) -> None:
    if settings.EMAIL_PROVIDER == "log":
        log.info("EMAIL to=%s subject=%r", to, subject)
        return
    raise RuntimeError(f"Email provider '{settings.EMAIL_PROVIDER}' is not implemented yet.")


def _notify(db: Session, user_id: str, kind: str, title: str, body: str, link: str | None, email: bool) -> dict | None:
    db.execute(text("INSERT INTO notifications (user_id, kind, title, body, link, emailed) "
                    "VALUES (:u, :k, :t, :b, :l, :e)"),
               {"u": user_id, "k": kind, "t": title, "b": body, "l": link, "e": email})
    if not email:
        return None
    contact = db.execute(text("SELECT email FROM user_contacts WHERE user_id = :u"), {"u": user_id}).first()
    return {"to": contact.email, "subject": title, "body": body} if contact and contact.email else None


def _handler(build):
    """Wraps a builder(data) -> (kind, title, body, link, email?) into an idempotent handler."""
    async def handle(event: dict) -> None:
        data = event["data"]
        with SessionLocal() as db:
            if not mark_processed(db, event):
                return
            kind, title, body, link, email = build(data)
            outgoing = _notify(db, data["user_id"], kind, title, body, link, email)
            db.commit()
        if outgoing:
            await send_email(**outgoing)
    return handle


async def on_user_registered(event: dict) -> None:
    data = event["data"]
    with SessionLocal() as db:
        if not mark_processed(db, event):
            return
        if "@" not in (data.get("email") or ""):   # e.g. a Shopify shop that shares no contact email
            db.commit()
            return
        db.execute(text("INSERT INTO user_contacts (user_id, email, full_name) VALUES (:u, :e, :n) "
                        "ON CONFLICT (user_id) DO UPDATE SET email = EXCLUDED.email, full_name = EXCLUDED.full_name"),
                   {"u": data["user_id"], "e": data["email"], "n": data.get("full_name")})
        db.commit()
    await send_email(data["email"], "Welcome to Listing Agent",
                     "Your 10 free listings are ready. Upload a product photo to start.")


bus.subscribe("user.registered", on_user_registered)
bus.subscribe("listing.generated", _handler(lambda d: (
    "listing_ready", "Your listing is ready", "Review it and publish when you're happy.",
    f"{settings.APP_URL}/products/{d['product_id']}", False)))
bus.subscribe("listing.failed", _handler(lambda d: (
    "listing_failed", "We couldn't write that listing", f"{d['reason']} Your credit was returned.",
    f"{settings.APP_URL}/products/{d['product_id']}", False)))
bus.subscribe("publish.succeeded", _handler(lambda d: (
    "published", "Published to your store", "Your product is in your store.", d.get("external_url"), False)))
bus.subscribe("publish.failed", _handler(lambda d: (
    "publish_failed", "Publishing failed", d["reason"], f"{settings.APP_URL}/products/{d['product_id']}", True)))
bus.subscribe("batch.completed", _handler(lambda d: (
    "batch_done", "Your batch is finished",
    f"{d['succeeded']} listing(s) ready" + (f", {d['failed']} need another try." if d["failed"] else "."),
    f"{settings.APP_URL}/products?batch={d['batch_id']}", True)))
bus.subscribe("credits.purchased", _handler(lambda d: (
    "credits", "Credits added", f"{d['amount']} credits added — your balance is {d['balance']}.", None, True)))


router = APIRouter(dependencies=[Depends(require_internal)])


@router.get("/notifications")
def my_notifications(user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    rows = db.execute(text("SELECT id, kind, title, body, link, is_read, created_at FROM notifications "
                           "WHERE user_id = :u ORDER BY created_at DESC LIMIT 50"), {"u": str(user_id)})
    return [{"id": str(r.id), "kind": r.kind, "title": r.title, "body": r.body, "link": r.link,
             "is_read": r.is_read, "created_at": r.created_at.isoformat()} for r in rows]


@router.post("/notifications/{notification_id}/read", status_code=204)
def mark_read(notification_id: uuid.UUID, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    updated = db.execute(text("UPDATE notifications SET is_read = true WHERE id = :id AND user_id = :u"),
                         {"id": str(notification_id), "u": str(user_id)})
    if not updated.rowcount:
        raise HTTPException(404, "Notification not found.")
    db.commit()


app = create_service("notification", routers=[router], bus=bus, engine=engine,
                     migrations_dir=Path(__file__).parent.parent / "migrations")
