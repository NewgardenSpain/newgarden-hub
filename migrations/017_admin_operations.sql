-- Auditoría de operaciones del panel de administración multitienda.
CREATE TABLE IF NOT EXISTS admin_operations (
    id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    operator text NOT NULL DEFAULT 'local',
    kind text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('single','global')),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS admin_operation_items (
    id bigserial PRIMARY KEY,
    op_id bigint NOT NULL REFERENCES admin_operations(id) ON DELETE CASCADE,
    store_code text NOT NULL,
    entity_gid text,
    status text NOT NULL CHECK (status IN ('OK','FAILED','SKIPPED')),
    error text,
    before jsonb,
    after jsonb
);
CREATE INDEX IF NOT EXISTS admin_operation_items_op_idx ON admin_operation_items(op_id);
