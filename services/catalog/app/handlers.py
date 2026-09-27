"""Catalog's event handlers. Each runs in one transaction with mark_processed, so a
redelivered event (at-least-once) changes nothing the second time."""
import logging
import uuid
from datetime import datetime

from sqlalchemy import func

from lagent_common.db import mark_processed

from app.core import Batch, Listing, ListingJob, Product, ProductPublication, SessionLocal, bus

log = logging.getLogger("lagent.catalog.handlers")


async def _maybe_complete_batch(db, product: Product) -> None:
    if not product.batch_id:
        return
    batch = db.get(Batch, product.batch_id)
    if not batch or batch.completed_notified:
        return
    counts = dict(
        db.query(Product.status, func.count()).filter(Product.batch_id == batch.id).group_by(Product.status).all()
    )
    if counts.get("generating") or counts.get("draft"):
        return
    batch.completed_notified = True
    db.commit()
    await bus.publish("batch.completed", {
        "batch_id": str(batch.id), "user_id": str(batch.user_id),
        "succeeded": counts.get("ready", 0), "failed": counts.get("failed", 0),
    })


async def on_listing_generated(event: dict) -> None:
    data = event["data"]
    with SessionLocal() as db:
        if not mark_processed(db, event):
            return
        job = db.get(ListingJob, uuid.UUID(data["job_id"]))
        product = db.get(Product, uuid.UUID(data["product_id"]))
        if not job or not product or job.status != "queued":
            db.commit()  # keep the processed mark; nothing else to do
            return
        for item in data["listings"]:
            previous = (db.query(Listing)
                        .filter(Listing.product_id == product.id, Listing.language == item["language"],
                                Listing.platform.is_(None) if item.get("platform") is None
                                else Listing.platform == item["platform"],
                                Listing.is_current.is_(True))
                        .first())
            version = 1
            if previous:
                previous.is_current = False
                version = previous.version + 1
                db.flush()  # retire the old one before inserting (partial unique index)
            db.add(Listing(
                product_id=product.id, job_id=job.id, language=item["language"], platform=item.get("platform"),
                title=item["title"], highlights=item.get("highlights", []), description=item["description"],
                seo_title=item.get("seo_title"), meta_description=item.get("meta_description"),
                tags=item.get("tags", []), category_suggestion=item.get("category_suggestion"), version=version,
            ))
        product.detected = data.get("facts")
        product.research = data.get("research")
        product.status = "ready"
        job.status, job.finished_at = "done", datetime.utcnow()
        db.commit()
        await _maybe_complete_batch(db, product)


async def on_listing_failed(event: dict) -> None:
    data = event["data"]
    with SessionLocal() as db:
        if not mark_processed(db, event):
            return
        job = db.get(ListingJob, uuid.UUID(data["job_id"]))
        product = db.get(Product, uuid.UUID(data["product_id"]))
        if job and job.status == "queued":
            job.status, job.error, job.finished_at = "failed", data["reason"], datetime.utcnow()
        if product and product.status == "generating":
            has_listing = db.query(Listing).filter(Listing.product_id == product.id, Listing.is_current.is_(True)).first()
            product.status = "ready" if has_listing else "failed"  # a failed *re*-generation keeps the old text
            product.last_error = data["reason"]
        db.commit()
        if product:
            await _maybe_complete_batch(db, product)


def _publication(db, data) -> ProductPublication | None:
    return (db.query(ProductPublication)
            .filter(ProductPublication.product_id == uuid.UUID(data["product_id"]),
                    ProductPublication.store_connection_id == uuid.UUID(data["store_connection_id"]))
            .first())


async def on_publish_succeeded(event: dict) -> None:
    data = event["data"]
    with SessionLocal() as db:
        if not mark_processed(db, event):
            return
        pub = _publication(db, data)
        if pub and str(pub.publish_job_id) == data["publish_job_id"]:  # ignore results of superseded attempts
            pub.status, pub.external_url, pub.error = "published", data.get("external_url"), None
        db.commit()


async def on_publish_failed(event: dict) -> None:
    data = event["data"]
    with SessionLocal() as db:
        if not mark_processed(db, event):
            return
        pub = _publication(db, data)
        if pub and str(pub.publish_job_id) == data["publish_job_id"]:
            pub.status, pub.error = "failed", data["reason"]
        db.commit()


def register(bus_) -> None:
    bus_.subscribe("listing.generated", on_listing_generated)
    bus_.subscribe("listing.failed", on_listing_failed)
    bus_.subscribe("publish.succeeded", on_publish_succeeded)
    bus_.subscribe("publish.failed", on_publish_failed)
