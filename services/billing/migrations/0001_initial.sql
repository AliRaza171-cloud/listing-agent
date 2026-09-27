-- billing service database
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE reservation_status AS ENUM ('reserved', 'spent', 'released');

CREATE TABLE processed_events (
    event_id     UUID PRIMARY KEY,
    event_type   VARCHAR NOT NULL,
    processed_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

CREATE TABLE credit_accounts (
    user_id    UUID PRIMARY KEY,
    balance    INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),   -- can never go negative
    updated_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);

-- credits held while a listing is being written (saga 4.1)
CREATE TABLE credit_reservations (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL,
    amount     INTEGER NOT NULL CHECK (amount > 0),
    reason     VARCHAR NOT NULL,
    ref_id     UUID NOT NULL UNIQUE,          -- catalog's job id: one reservation per job, even if asked twice
    status     reservation_status NOT NULL DEFAULT 'reserved',
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    settled_at TIMESTAMP
);
CREATE INDEX ix_reservations_open ON credit_reservations(expires_at) WHERE status = 'reserved';

-- every balance change, forever
CREATE TABLE credit_transactions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL,
    delta      INTEGER NOT NULL,
    reason     VARCHAR NOT NULL,              -- signup_bonus | reserved | released | purchase
    ref_id     UUID,
    created_at TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX ix_credit_transactions_user ON credit_transactions(user_id, created_at DESC);
