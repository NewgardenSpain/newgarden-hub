-- Emparejamiento de variantes MX (SKU/EAN propios por motivos legales) con su SKU de ES (maestra).
-- Candidatos en staging (como glossary_candidates): nada pasa a sku_links sin revisión humana.
CREATE TABLE IF NOT EXISTS sku_link_candidates (
    id bigserial PRIMARY KEY,
    mx_variant_gid text NOT NULL UNIQUE,
    mx_product_gid text NOT NULL,
    mx_sku text,
    mx_barcode text,
    mx_title text NOT NULL DEFAULT '',
    mx_variant_title text NOT NULL DEFAULT '',
    mx_status text NOT NULL DEFAULT '',
    kind text NOT NULL DEFAULT 'single' CHECK (kind IN ('single','pack','bundle','refurb','none')),
    es_sku text,
    es_variant_gid text,
    es_title text NOT NULL DEFAULT '',
    components jsonb NOT NULL DEFAULT '[]',
    score real NOT NULL DEFAULT 0,
    reasons jsonb NOT NULL DEFAULT '[]',
    alternatives jsonb NOT NULL DEFAULT '[]',
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sku_link_candidates_status_idx ON sku_link_candidates (status, score DESC);

-- Vínculo permanente: una fila por variante MX. NONE = esa variante MX no tiene equivalente en ES.
CREATE TABLE IF NOT EXISTS sku_links (
    mx_variant_gid text PRIMARY KEY,
    mx_sku text,
    mx_barcode text,
    es_sku text,
    es_variant_gid text,
    kind text NOT NULL DEFAULT 'single' CHECK (kind IN ('single','pack','bundle','refurb','none')),
    components jsonb NOT NULL DEFAULT '[]',
    status text NOT NULL CHECK (status IN ('CONFIRMED','NONE')),
    linked_by text NOT NULL DEFAULT 'local',
    linked_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'CONFIRMED') = (es_sku IS NOT NULL OR jsonb_array_length(components) > 0))
);
CREATE INDEX IF NOT EXISTS sku_links_mx_sku_idx ON sku_links (mx_sku);
CREATE INDEX IF NOT EXISTS sku_links_es_sku_idx ON sku_links (es_sku);
