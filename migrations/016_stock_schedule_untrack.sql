-- Un reaprovisionamiento programado puede fijar una cantidad (track=true) o volver el SKU a sin seguimiento (track=false).
-- Un mismo SKU puede estar en varias variantes de una tienda: cada programación apunta a un artículo de inventario.
ALTER TABLE stock_schedules ADD COLUMN IF NOT EXISTS track boolean NOT NULL DEFAULT true;
ALTER TABLE stock_schedules ADD COLUMN IF NOT EXISTS inventory_item_id text;
ALTER TABLE stock_schedules ALTER COLUMN quantity DROP NOT NULL;
ALTER TABLE stock_schedules ADD CONSTRAINT stock_schedules_track_quantity CHECK (NOT track OR quantity IS NOT NULL);
