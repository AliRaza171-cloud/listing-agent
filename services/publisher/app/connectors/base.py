"""Store connectors — how a finished listing gets into a seller's store.

Every platform implements the same small interface, so the publish flow is
identical for Smart Click-style custom stores, WooCommerce and Shopify.
Adding a platform later = one new class, registered in __init__.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class StoreCategory:
    id: str
    name: str


@dataclass
class ImageFile:
    filename: str
    data: bytes
    content_type: str


@dataclass
class ProductPayload:
    """Everything a store needs to create/update one product. Built from Product + current Listing."""
    title: str
    description: str               # HTML-safe text; connectors format per platform
    highlights: list[str]
    price: float
    discount_pct: int | None
    stock: int | None
    sku: str | None
    free_shipping: bool
    image_urls: list[str]
    tags: list[str] = field(default_factory=list)
    category_id: str | None = None
    category_name: str | None = None   # the AI's pick, from the store's own category list
    seo_title: str | None = None
    meta_description: str | None = None
    publish_live: bool = False     # False = create as draft (safe default)
    brand: str | None = None       # from what the AI saw in the photos
    attributes: dict = field(default_factory=dict)   # e.g. {"color": "black", "power": "2200W"} (AI-detected)
    # Package for delivery (Daraz requires these; WooCommerce uses them for shipping rates)
    weight_kg: float | None = None
    length_cm: float | None = None
    width_cm: float | None = None
    height_cm: float | None = None
    image_files: list[ImageFile] = field(default_factory=list)  # photo bytes, loaded by publisher


@dataclass
class PublishResult:
    external_id: str
    external_url: str | None = None


class ConnectorError(Exception):
    """Raised with a seller-friendly message, e.g. 'WooCommerce rejected the API keys'.
    retryable=True for temporary problems (timeouts, 429, 5xx) — publisher retries those."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class StoreConnector(Protocol):
    def __init__(self, store_url: str, credentials: dict): ...

    async def test_connection(self) -> None:
        """Raise ConnectorError if the URL/credentials don't work. Called when connecting a store."""

    async def list_categories(self) -> list[StoreCategory]:
        """The store's real categories, so the AI picks from them."""

    async def create_product(self, payload: ProductPayload) -> PublishResult: ...

    async def update_product(self, external_id: str, payload: ProductPayload) -> PublishResult:
        """Used on re-publish so the store doesn't get duplicates."""
