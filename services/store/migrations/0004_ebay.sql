-- eBay: sellers connect with eBay's own login; a connect request carries the eBay site and item location
ALTER TYPE store_platform ADD VALUE IF NOT EXISTS 'ebay';
ALTER TABLE connect_requests ADD COLUMN IF NOT EXISTS extra JSONB;
