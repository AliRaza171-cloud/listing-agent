-- each product is sold in one market: its price is in this currency, the AI writes/researches for this country
ALTER TABLE products ADD COLUMN IF NOT EXISTS country  VARCHAR(2) NOT NULL DEFAULT 'PK';
ALTER TABLE products ADD COLUMN IF NOT EXISTS currency VARCHAR(3) NOT NULL DEFAULT 'PKR';
