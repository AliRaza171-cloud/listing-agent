-- notification service database
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE processed_events (
    event_id     UUID PRIMARY KEY,
    event_type   VARCHAR NOT NULL,
    processed_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

-- local copy of what we need about users, kept in sync from user.registered
CREATE TABLE user_contacts (
    user_id   UUID PRIMARY KEY,
    email     VARCHAR NOT NULL,
    full_name VARCHAR
);

-- in-app notifications (the bell) + email delivery log
CREATE TABLE notifications (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL,
    kind       VARCHAR NOT NULL,
    title      VARCHAR NOT NULL,
    body       VARCHAR NOT NULL,
    link       VARCHAR,
    is_read    BOOLEAN NOT NULL DEFAULT false,
    emailed    BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX ix_notifications_user ON notifications(user_id, created_at DESC);
