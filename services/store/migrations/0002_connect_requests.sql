-- one-click connections: a pending "Connect" click, matched when the store sends the seller back
CREATE TABLE connect_requests (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),   -- also the one-time `state` we send to the store
    user_id     UUID NOT NULL,
    platform    store_platform NOT NULL,
    name        VARCHAR NOT NULL,
    store_url   VARCHAR NOT NULL,
    status      VARCHAR NOT NULL DEFAULT 'pending',          -- pending | connected | failed
    error       VARCHAR,
    store_id    UUID,
    created_at  TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX ix_connect_requests_user ON connect_requests(user_id, created_at DESC);
