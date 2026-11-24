-- Permite cantidades negativas en reaprovisionamientos (stock negativo = no disponible en algunas tiendas).
ALTER TABLE stock_schedules DROP CONSTRAINT IF EXISTS stock_schedules_quantity_check;
