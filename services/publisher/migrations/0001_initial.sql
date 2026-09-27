-- publisher service database
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE publish_status AS ENUM ('publishing', 'published', 'failed');

CREATE TABLE processed_events (
    event_id     UUID PRIMARY KEY,
    event_type   VARCHAR NOT NULL,
    processed_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE publish_jobs (
    id                  UUID PRIMARY KEY,               -- publish_job_id from catalog
    product_id          UUID NOT NULL,
    user_id             UUID NOT NULL,
    store_connection_id UUID NOT NULL,
    mode                VARCHAR NOT NULL DEFAULT 'draft',
    status              publish_status NOT NULL DEFAULT 'publishing',
    external_id         VARCHAR,     -- the store's product id: re-publishing updates it instead of duplicating
    external_url        VARCHAR,
    error               VARCHAR,
    attempts            INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at          TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX ix_publish_jobs_product_store ON publish_jobs(product_id, store_connection_id, created_at DESC);
