-- where the seller sells: prices, AI price research and listings use this country and currency
ALTER TABLE users ADD COLUMN IF NOT EXISTS country  VARCHAR(2) NOT NULL DEFAULT 'PK';
ALTER TABLE users ADD COLUMN IF NOT EXISTS currency VARCHAR(3) NOT NULL DEFAULT 'PKR';
