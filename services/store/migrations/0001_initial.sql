-- store service database
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE store_platform    AS ENUM ('custom', 'woocommerce', 'shopify');
CREATE TYPE connection_status AS ENUM ('active', 'error', 'disconnected');

CREATE TABLE store_connections (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id               UUID NOT NULL,
    platform              store_platform NOT NULL,
    name                  VARCHAR NOT NULL,
    store_url             VARCHAR NOT NULL,
    credentials_encrypted TEXT NOT NULL,      -- Fernet; only ever decrypted for publisher's internal call
    status                connection_status NOT NULL DEFAULT 'active',
    last_error            VARCHAR,
    created_at            TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    updated_at            TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    UNIQUE (user_id, platform, store_url)
);
CREATE INDEX ix_store_connections_user ON store_connections(user_id);
