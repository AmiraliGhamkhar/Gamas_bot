-- Retain the catalog's list price separately from the final sale price.
-- Existing custom/standard rows start at their previous live price; startup
-- catalog sync supplies the canonical original and sale prices for current
-- standard plans. Historical payment snapshots remain unchanged.
ALTER TABLE plans ADD COLUMN original_price INTEGER NOT NULL DEFAULT 0;
UPDATE plans SET original_price = price WHERE original_price <= 0;
