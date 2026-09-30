"""Starting a publish (saga 4.2) — shared by the Publish button and bulk auto-publish."""
import uuid

from app.core import Product, ProductPublication, bus


class PublishBlocked(Exception):
    """The product can't be published yet; the message is shown to the seller."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def check_publishable(product: Product, language: str):
    """-> the listing to publish, or PublishBlocked with what the seller must do first."""
    if product.status != "ready":
        raise PublishBlocked("Generate the listing first.", 409)
    if product.price is None:
        raise PublishBlocked("Set a price before publishing.")
    listings = [l for l in product.listings if l.is_current and l.language == language]
    if not listings:
        raise PublishBlocked(f"No {language} listing to publish.")
    return next((l for l in listings if l.platform is None), listings[0])


def mark_publications(product: Product, store_ids: list[uuid.UUID], mode: str, *, status: str = "publishing",
                      error: str | None = None) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Create/refresh one publication row per store. -> [(publish_job_id, store_id)]."""
    jobs = []
    for store_id in store_ids:
        publish_job_id = uuid.uuid4()
        pub = next((x for x in product.publications if x.store_connection_id == store_id), None)
        if pub is None:
            pub = ProductPublication(product_id=product.id, store_connection_id=store_id, publish_job_id=publish_job_id)
            product.publications.append(pub)
        pub.publish_job_id, pub.status, pub.mode, pub.error = publish_job_id, status, mode, error
        jobs.append((publish_job_id, store_id))
    return jobs


async def send_publish_events(product: Product, listing, jobs, mode: str) -> None:
    """Call after the publication rows are committed."""
    for publish_job_id, store_id in jobs:
        await bus.publish("publish.requested", {
            "publish_job_id": str(publish_job_id), "product_id": str(product.id), "user_id": str(product.user_id),
            "store_connection_id": str(store_id), "mode": mode,
            "product": {
                "title": listing.title, "description": listing.description, "highlights": listing.highlights,
                "price": float(product.price), "discount_pct": product.discount_pct, "stock": product.stock,
                "sku": product.sku, "free_shipping": product.free_shipping,
                "image_urls": [i.url for i in product.images], "tags": listing.tags, "category_id": None,
                "category_name": listing.category_suggestion,
                "seo_title": listing.seo_title, "meta_description": listing.meta_description,
                "brand": (product.detected or {}).get("brand"),
                "currency": product.currency or "PKR",
                "attributes": (product.detected or {}).get("attributes") or {},
                **{k: (float(getattr(product, k)) if getattr(product, k) is not None else None)
                   for k in ("weight_kg", "length_cm", "width_cm", "height_cm")},
            },
        })
