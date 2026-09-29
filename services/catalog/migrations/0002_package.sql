-- package for delivery (Daraz requires it; WooCommerce uses it for shipping rates)
ALTER TABLE products ADD COLUMN IF NOT EXISTS weight_kg NUMERIC(8, 3);
ALTER TABLE products ADD COLUMN IF NOT EXISTS length_cm NUMERIC(8, 1);
ALTER TABLE products ADD COLUMN IF NOT EXISTS width_cm  NUMERIC(8, 1);
ALTER TABLE products ADD COLUMN IF NOT EXISTS height_cm NUMERIC(8, 1);
