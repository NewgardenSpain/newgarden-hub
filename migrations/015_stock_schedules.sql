-- Reaprovisionamientos programados del control de stock multitienda.
-- Al llegar `run_at` se activa el seguimiento de inventario y se fija la cantidad disponible.
CREATE TABLE IF NOT EXISTS stock_schedules (
  id bigserial PRIMARY KEY,
  sku text NOT NULL,
  store_code text NOT NULL REFERENCES stores(code),
  location_id text,
  quantity integer NOT NULL CHECK (quantity >= 0),
  run_at timestamptz NOT NULL,
  state text NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','RUNNING','DONE','FAILED','CANCELLED')),
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  applied_at timestamptz
);
CREATE INDEX IF NOT EXISTS stock_schedules_due_idx ON stock_schedules(run_at) WHERE state='PENDING';
CREATE INDEX IF NOT EXISTS stock_schedules_sku_idx ON stock_schedules(lower(sku));
