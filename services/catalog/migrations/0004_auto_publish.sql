-- bulk upload: publish to these stores as soon as the listing is written (then cleared)
ALTER TABLE products ADD COLUMN IF NOT EXISTS auto_publish JSONB;
