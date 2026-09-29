-- Shopify Billing: which shop a charge belongs to (payments.provider = 'shopify';
-- provider_ref = the AppPurchaseOneTime id, e.g. gid://shopify/AppPurchaseOneTime/123)
ALTER TABLE payments ADD COLUMN IF NOT EXISTS shop VARCHAR;
