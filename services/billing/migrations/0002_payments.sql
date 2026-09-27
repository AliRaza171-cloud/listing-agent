-- credit pack purchases (Stripe / Safepay)
CREATE TYPE payment_status AS ENUM ('pending', 'paid', 'failed');

CREATE TABLE payments (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL,
    pack_id      VARCHAR NOT NULL,
    credits      INTEGER NOT NULL CHECK (credits > 0),
    provider     VARCHAR NOT NULL,             -- stripe | safepay
    amount       NUMERIC(12, 2) NOT NULL,      -- in `currency`, major units (rupees / dollars)
    currency     VARCHAR(3) NOT NULL,
    status       payment_status NOT NULL DEFAULT 'pending',
    provider_ref VARCHAR,                      -- Stripe checkout session id / Safepay tracker
    error        VARCHAR,
    created_at   TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    paid_at      TIMESTAMP
);
CREATE INDEX ix_payments_user ON payments(user_id, created_at DESC);
CREATE UNIQUE INDEX ux_payments_ref ON payments(provider, provider_ref) WHERE provider_ref IS NOT NULL;
