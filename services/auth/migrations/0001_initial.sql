-- auth service database
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           VARCHAR NOT NULL UNIQUE,          -- stored lower-cased
    hashed_password VARCHAR,
    full_name       VARCHAR,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    failed_logins   INTEGER NOT NULL DEFAULT 0,
    locked_until    TIMESTAMP,
    created_at      TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
