import uuid
from datetime import datetime

from pydantic_settings import BaseSettings
from sqlalchemy import Boolean, Column, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import declarative_base, relationship

from lagent_common.bus import EventBus
from lagent_common.db import make_db


class Settings(BaseSettings):
    DATABASE_URL: str
    REDIS_URL: str = "redis://redis:6379/0"
    INTERNAL_TOKEN: str
    BILLING_URL: str = "http://billing:8000"
    PUBLISHER_URL: str = "http://publisher:8000"
    CREDITS_PER_LISTING: int = 1
    MEDIA_DIR: str = "/data/media"
    MAX_UPLOAD_MB: int = 8


settings = Settings()
engine, SessionLocal, get_db = make_db(settings.DATABASE_URL)
bus = EventBus(settings.REDIS_URL, "catalog")
Base = declarative_base()


def pg_enum(*values, name):
    return Enum(*values, name=name, create_type=False)


class Batch(Base):
    __tablename__ = "batches"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    name = Column(String, nullable=False)
    completed_notified = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Product(Base):
    __tablename__ = "products"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    batch_id = Column(UUID(as_uuid=True), ForeignKey("batches.id", ondelete="SET NULL"))
    status = Column(pg_enum("draft", "generating", "ready", "failed", name="product_status"), default="draft", nullable=False)
    seller_notes = Column(Text)
    detected = Column(JSONB)
    research = Column(JSONB)
    price = Column(Numeric(10, 2))
    discount_pct = Column(Integer)
    stock = Column(Integer)
    sku = Column(String)
    free_shipping = Column(Boolean, default=False, nullable=False)
    country = Column(String(2), default="PK", nullable=False)     # market: prices in `currency`,
    currency = Column(String(3), default="PKR", nullable=False)   # AI writes/researches for `country`
    weight_kg = Column(Numeric(8, 3))
    length_cm = Column(Numeric(8, 1))
    width_cm = Column(Numeric(8, 1))
    height_cm = Column(Numeric(8, 1))
    last_error = Column(String)
    # bulk upload: {"store_connection_ids": [...], "mode": "draft"|"live", "language": "en"|"ur"} — publish
    # as soon as the listing is ready, then cleared
    auto_publish = Column(JSONB)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    images = relationship("ProductImage", cascade="all, delete-orphan", order_by="ProductImage.position")
    listings = relationship("Listing", cascade="all, delete-orphan")
    publications = relationship("ProductPublication", cascade="all, delete-orphan")


class ProductImage(Base):
    __tablename__ = "product_images"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    url = Column(String, nullable=False)
    position = Column(Integer, default=0, nullable=False)


class ListingJob(Base):
    __tablename__ = "listing_jobs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), nullable=False)
    reservation_id = Column(UUID(as_uuid=True), nullable=False)
    status = Column(pg_enum("queued", "done", "failed", name="job_status"), default="queued", nullable=False)
    languages = Column(ARRAY(Text), nullable=False)
    platforms = Column(ARRAY(Text), nullable=False)
    error = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime)


class Listing(Base):
    __tablename__ = "listings"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    job_id = Column(UUID(as_uuid=True), ForeignKey("listing_jobs.id", ondelete="SET NULL"))
    language = Column(pg_enum("en", "ur", name="listing_language"), default="en", nullable=False)
    platform = Column(pg_enum("custom", "woocommerce", "shopify", name="store_platform"))
    title = Column(String, nullable=False)
    highlights = Column(ARRAY(Text), default=list, nullable=False)
    description = Column(Text, nullable=False)
    seo_title = Column(String)
    meta_description = Column(String)
    tags = Column(ARRAY(Text), default=list, nullable=False)
    category_suggestion = Column(String)
    version = Column(Integer, default=1, nullable=False)
    is_current = Column(Boolean, default=True, nullable=False)
    edited_by_seller = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ProductPublication(Base):
    __tablename__ = "product_publications"
    __table_args__ = (UniqueConstraint("product_id", "store_connection_id"),)
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    store_connection_id = Column(UUID(as_uuid=True), nullable=False)
    publish_job_id = Column(UUID(as_uuid=True), nullable=False)
    status = Column(pg_enum("publishing", "published", "failed", name="publication_status"), default="publishing", nullable=False)
    mode = Column(String, default="draft", nullable=False)
    external_url = Column(String)
    error = Column(String)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
