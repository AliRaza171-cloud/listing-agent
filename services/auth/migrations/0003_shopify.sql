-- accounts created by installing Listing Agent from the Shopify App Store: one per shop.
-- They sign in only through Shopify (no password), so `email` holds the shop domain.
ALTER TABLE users ADD COLUMN IF NOT EXISTS shopify_shop VARCHAR UNIQUE;
