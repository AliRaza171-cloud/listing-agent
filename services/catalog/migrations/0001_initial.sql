-- catalog service database. user_id / store_connection_id are plain UUIDs owned by other services.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE product_status   AS ENUM ('draft', 'generating', 'ready', 'failed');
CREATE TYPE listing_language AS ENUM ('en', 'ur');
CREATE TYPE store_platform   AS ENUM ('custom', 'woocommerce', 'shopify');
CREATE TYPE job_status       AS ENUM ('queued', 'done', 'failed');
CREATE TYPE publication_status AS ENUM ('publishing', 'published', 'failed');

CREATE TABLE processed_events (
    event_id     UUID PRIMARY KEY,
    event_type   VARCHAR NOT NULL,
    processed_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE batches (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL,
    name       VARCHAR NOT NULL,
    completed_notified BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX ix_batches_user ON batches(user_id);

CREATE TABLE products (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL,
    batch_id      UUID REFERENCES batches(id) ON DELETE SET NULL,
    status        product_status NOT NULL DEFAULT 'draft',
    seller_notes  TEXT,
    detected      JSONB,
    research      JSONB,
    price         NUMERIC(10, 2) CHECK (price IS NULL OR price > 0),
    discount_pct  INTEGER CHECK (discount_pct IS NULL OR (discount_pct BETWEEN 1 AND 95)),
    stock         INTEGER CHECK (stock IS NULL OR stock >= 0),
    sku           VARCHAR,
    free_shipping BOOLEAN NOT NULL DEFAULT false,
    last_error    VARCHAR,
    created_at    TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at    TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX ix_products_user ON products(user_id, created_at DESC);
CREATE INDEX ix_products_batch ON products(batch_id);

CREATE TABLE product_images (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    url        VARCHAR NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_product_images_product ON product_images(product_id, position);

-- one row per "Generate" click; ties the saga together (reservation in billing, work in ai)
CREATE TABLE listing_jobs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id     UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    user_id        UUID NOT NULL,
    reservation_id UUID NOT NULL,
    status         job_status NOT NULL DEFAULT 'queued',
    languages      TEXT[] NOT NULL,
    platforms      TEXT[] NOT NULL,
    error          VARCHAR,
    created_at     TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    finished_at    TIMESTAMP
);
CREATE INDEX ix_listing_jobs_product ON listing_jobs(product_id, created_at DESC);

CREATE TABLE listings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    job_id              UUID REFERENCES listing_jobs(id) ON DELETE SET NULL,
    language            listing_language NOT NULL DEFAULT 'en',
    platform            store_platform,
    title               VARCHAR NOT NULL,
    highlights          TEXT[] NOT NULL DEFAULT '{}',
    description         TEXT NOT NULL,
    seo_title           VARCHAR,
    meta_description    VARCHAR,
    tags                TEXT[] NOT NULL DEFAULT '{}',
    category_suggestion VARCHAR,
    version             INTEGER NOT NULL DEFAULT 1,
    is_current          BOOLEAN NOT NULL DEFAULT true,
    edited_by_seller    BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE UNIQUE INDEX ux_listings_current_generic
    ON listings (product_id, language) WHERE is_current AND platform IS NULL;
CREATE UNIQUE INDEX ux_listings_current_platform
    ON listings (product_id, language, platform) WHERE is_current AND platform IS NOT NULL;

-- catalog's read-model of publish status, kept up to date from publisher's events
CREATE TABLE product_publications (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id          UUID NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    store_connection_id UUID NOT NULL,
    publish_job_id      UUID NOT NULL,
    status              publication_status NOT NULL DEFAULT 'publishing',
    mode                VARCHAR NOT NULL DEFAULT 'draft',
    external_url        VARCHAR,
    error               VARCHAR,
    updated_at          TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    UNIQUE (product_id, store_connection_id)
);
