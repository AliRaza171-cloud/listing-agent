-- Which Shopify app a connection's token belongs to. Set only by the server (never taken from what a
-- seller submits), so billing and the uninstall webhook can tell Listing Agent's app from a shop's own keys.
ALTER TABLE store_connections ADD COLUMN IF NOT EXISTS via_app VARCHAR;
-- Shopify connections made with the Connect button before this column existed: '*' = our app (id not recorded).
UPDATE store_connections SET via_app = '*'
 WHERE platform = 'shopify' AND via_app IS NULL
   AND id IN (SELECT store_id FROM connect_requests
               WHERE platform = 'shopify' AND status = 'connected' AND store_id IS NOT NULL);
