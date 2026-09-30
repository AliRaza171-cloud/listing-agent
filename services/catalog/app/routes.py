"""Catalog HTTP API (reached through the gateway as /api/catalog/...)."""
import uuid
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from lagent_common.markets import Market
from lagent_common.correlation import outgoing_headers
from lagent_common.internal import current_user_id, require_internal

from app.core import Batch, Listing, ListingJob, Product, ProductImage, bus, get_db, settings
from app.publishing import PublishBlocked, check_publishable, mark_publications, send_publish_events

router = APIRouter(dependencies=[Depends(require_internal)])
_http = httpx.AsyncClient(timeout=15)

LANGUAGES = {"en", "ur"}
PLATFORMS = {"shopify", "woocommerce", "custom"}


class ProductIn(BaseModel):
    image_urls: list[str] = Field(default_factory=list, max_length=8)
    seller_notes: str | None = None
    batch_id: uuid.UUID | None = None
    # The seller's market (from their account). Prices of this product are in this currency.
    country: str | None = Field(default=None, max_length=2)
    currency: str | None = Field(default=None, max_length=3)


class ProductPatch(BaseModel):
    seller_notes: str | None = None
    price: Decimal | None = Field(default=None, gt=0)
    discount_pct: int | None = Field(default=None, ge=1, le=95)
    stock: int | None = Field(default=None, ge=0)
    sku: str | None = None
    free_shipping: bool | None = None
    weight_kg: Decimal | None = Field(default=None, gt=0, le=500)
    length_cm: Decimal | None = Field(default=None, gt=0, le=1000)
    width_cm: Decimal | None = Field(default=None, gt=0, le=1000)
    height_cm: Decimal | None = Field(default=None, gt=0, le=1000)


class AutoPublishIn(BaseModel):
    """Bulk upload only: publish to these stores as soon as the listing is written."""
    store_connection_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    mode: str = Field(default="draft", pattern="^(draft|live)$")
    language: str = Field(default="en", pattern="^(en|ur)$")


class GenerateIn(BaseModel):
    languages: list[str] = ["en"]
    platforms: list[str] = []                 # empty = one generic listing
    research: bool = False
    store_connection_ids: list[uuid.UUID] = []  # stores whose categories the AI should pick from
    auto_publish: AutoPublishIn | None = None


class PublishIn(BaseModel):
    store_connection_ids: list[uuid.UUID] = Field(min_length=1)
    mode: str = Field(default="draft", pattern="^(draft|live)$")
    language: str = "en"


def _own_product(db: Session, product_id: uuid.UUID, user_id: uuid.UUID) -> Product:
    product = db.get(Product, product_id)
    if not product or product.user_id != user_id:
        raise HTTPException(404, "Product not found.")
    return product


def _serialize(p: Product) -> dict:
    current = [l for l in p.listings if l.is_current]
    return {
        "id": str(p.id), "status": p.status, "batch_id": str(p.batch_id) if p.batch_id else None,
        "seller_notes": p.seller_notes, "detected": p.detected, "research": p.research,
        "price": float(p.price) if p.price is not None else None, "discount_pct": p.discount_pct,
        "stock": p.stock, "sku": p.sku, "free_shipping": p.free_shipping, "last_error": p.last_error,
        "country": p.country or "PK", "currency": p.currency or "PKR",
        **{k: (float(getattr(p, k)) if getattr(p, k) is not None else None) for k in ("weight_kg", "length_cm", "width_cm", "height_cm")},
        "images": [i.url for i in p.images],
        "listings": [{
            "id": str(l.id), "language": l.language, "platform": l.platform, "title": l.title,
            "highlights": l.highlights, "description": l.description, "seo_title": l.seo_title,
            "meta_description": l.meta_description, "tags": l.tags,
            "category_suggestion": l.category_suggestion, "version": l.version,
        } for l in current],
        "publications": [{
            "store_connection_id": str(x.store_connection_id), "status": x.status, "mode": x.mode,
            "external_url": x.external_url, "error": x.error,
        } for x in p.publications],
        "auto_publish": p.auto_publish,
        "updated_at": p.updated_at.isoformat(),
    }


class BatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def _batch_summary(db: Session, batch: Batch) -> dict:
    counts = dict(db.query(Product.status, func.count()).filter(Product.batch_id == batch.id)
                  .group_by(Product.status).all())
    return {"id": str(batch.id), "name": batch.name, "created_at": batch.created_at.isoformat(),
            "total": sum(counts.values()), "draft": counts.get("draft", 0),
            "generating": counts.get("generating", 0), "ready": counts.get("ready", 0),
            "failed": counts.get("failed", 0)}


@router.post("/batches", status_code=201)
def create_batch(data: BatchIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    batch = Batch(user_id=user_id, name=data.name.strip())
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return _batch_summary(db, batch)


@router.get("/batches")
def list_batches(user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    rows = db.query(Batch).filter(Batch.user_id == user_id).order_by(Batch.created_at.desc()).limit(50).all()
    return [_batch_summary(db, b) for b in rows]


@router.post("/products", status_code=201)
def create_product(data: ProductIn, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    if data.batch_id is not None:
        batch = db.get(Batch, data.batch_id)
        if not batch or batch.user_id != user_id:
            raise HTTPException(404, "Batch not found.")
    market = Market.of({"country": data.country, "currency": data.currency})
    product = Product(user_id=user_id, seller_notes=data.seller_notes, batch_id=data.batch_id,
                      country=market.country, currency=market.currency)
    product.images = [ProductImage(url=u, position=i) for i, u in enumerate(data.image_urls)]
    db.add(product)
    db.commit()
    db.refresh(product)
    return _serialize(product)


@router.get("/products")
def list_products(user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    rows = db.query(Product).filter(Product.user_id == user_id).order_by(Product.created_at.desc()).limit(200).all()
    return [_serialize(p) for p in rows]


@router.get("/products/{product_id}")
def get_product(product_id: uuid.UUID, user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    return _serialize(_own_product(db, product_id, user_id))


@router.patch("/products/{product_id}")
def update_product(product_id: uuid.UUID, data: ProductPatch,
                   user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    product = _own_product(db, product_id, user_id)
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    db.commit()
    db.refresh(product)
    return _serialize(product)


class ListingPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    highlights: list[str] | None = Field(default=None, max_length=12)
    description: str | None = Field(default=None, min_length=1, max_length=10000)
    tags: list[str] | None = Field(default=None, max_length=30)
    seo_title: str | None = Field(default=None, max_length=200)
    meta_description: str | None = Field(default=None, max_length=400)
    category_suggestion: str | None = Field(default=None, max_length=60)


@router.patch("/products/{product_id}/listings/{listing_id}")
def update_listing(product_id: uuid.UUID, listing_id: uuid.UUID, data: ListingPatch,
                   user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    """The seller's own edits. Saved in place on the current version and marked as edited."""
    product = _own_product(db, product_id, user_id)
    listing = next((l for l in product.listings if l.id == listing_id and l.is_current), None)
    if listing is None:
        raise HTTPException(404, "Listing not found.")
    changes = data.model_dump(exclude_unset=True)
    for field in ("highlights", "tags"):
        if field in changes and changes[field] is not None:
            changes[field] = [s.strip() for s in changes[field] if s and s.strip()]
    category = changes.pop("category_suggestion", "__unset__")
    for field, value in changes.items():
        if value is not None:
            setattr(listing, field, value)
    if category != "__unset__":
        # The category belongs to the product, not one language: apply it to every current listing.
        category = (category or "").strip() or None
        for other in product.listings:
            if other.is_current:
                other.category_suggestion = category
    listing.edited_by_seller = True
    db.commit()
    db.refresh(product)
    return _serialize(product)


@router.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: uuid.UUID, user_id: uuid.UUID = Depends(current_user_id),
                   db: Session = Depends(get_db)):
    product = _own_product(db, product_id, user_id)
    if product.status == "generating":
        raise HTTPException(409, "Wait for the listing to finish before deleting.")
    db.delete(product)
    db.commit()


@router.post("/products/{product_id}/generate", status_code=202)
async def generate(product_id: uuid.UUID, data: GenerateIn,
                   user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    """Saga step 1 (contracts/README.md §4.1): reserve a credit, then hand the work to ai."""
    product = _own_product(db, product_id, user_id)
    if product.status == "generating":
        raise HTTPException(409, "A listing is already being written for this product.")
    if not set(data.languages) <= LANGUAGES or not data.languages:
        raise HTTPException(422, "Unsupported language.")
    # Marketplaces (Daraz, eBay) take the general listing; only these get their own format.
    platforms = [p for p in dict.fromkeys(data.platforms) if p in PLATFORMS]
    if data.auto_publish is not None and product.batch_id is None:
        raise HTTPException(422, "Automatic publishing is for bulk uploads.")
    if not product.images and not product.seller_notes:
        raise HTTPException(422, "Add at least one photo or some details first.")

    job_id = uuid.uuid4()
    res = await _http.post(
        f"{settings.BILLING_URL}/internal/reservations",
        json={"user_id": str(user_id), "amount": settings.CREDITS_PER_LISTING,
              "reason": "listing", "ref_id": str(job_id)},
        headers=outgoing_headers(settings.INTERNAL_TOKEN),
    )
    if res.status_code == 402:
        raise HTTPException(402, "You're out of credits — buy a pack to keep generating.")
    res.raise_for_status()
    reservation_id = res.json()["reservation_id"]

    categories: list[str] = []
    for store_id in data.store_connection_ids:
        r = await _http.get(f"{settings.PUBLISHER_URL}/internal/stores/{store_id}/categories",
                            params={"user_id": str(user_id)}, headers=outgoing_headers(settings.INTERNAL_TOKEN))
        if r.status_code == 200:
            categories += [c["name"] for c in r.json()]

    db.add(ListingJob(id=job_id, product_id=product.id, user_id=user_id, reservation_id=reservation_id,
                      languages=data.languages, platforms=platforms))
    product.status, product.last_error = "generating", None
    # Saved on the product so it survives the seller closing the page; used once, when the listing is ready.
    product.auto_publish = ({**data.auto_publish.model_dump(mode="json"),
                             "store_connection_ids": [str(x) for x in dict.fromkeys(data.auto_publish.store_connection_ids)]}
                            if data.auto_publish else None)
    db.commit()

    await bus.publish("listing.requested", {
        "job_id": str(job_id), "product_id": str(product.id), "user_id": str(user_id),
        "reservation_id": reservation_id, "image_urls": [i.url for i in product.images],
        "seller_notes": product.seller_notes, "languages": data.languages, "platforms": platforms,
        "research": data.research, "categories": sorted(set(categories)),
        "market": {"country": product.country or "PK", "currency": product.currency or "PKR"},
    })
    return {"job_id": str(job_id), "status": "generating"}


@router.post("/products/{product_id}/publish", status_code=202)
async def publish(product_id: uuid.UUID, data: PublishIn,
                  user_id: uuid.UUID = Depends(current_user_id), db: Session = Depends(get_db)):
    """Saga 4.2: one publish.requested per store. Seller-only facts must be filled in first."""
    product = _own_product(db, product_id, user_id)
    try:
        listing = check_publishable(product, data.language)
    except PublishBlocked as exc:
        raise HTTPException(exc.status, str(exc))
    product.auto_publish = None      # the seller took over
    jobs = mark_publications(product, list(dict.fromkeys(data.store_connection_ids)), data.mode)
    db.commit()
    await send_publish_events(product, listing, jobs, data.mode)
    return {"publishing_to": [str(s) for _, s in jobs]}
