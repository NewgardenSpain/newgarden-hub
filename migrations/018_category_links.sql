-- Vinculación permanente de colecciones homólogas entre tiendas (maestra = ES).
-- Una fila por (colección maestra, tienda). La ausencia de fila significa "pendiente de vincular";
-- NONE confirma de forma explícita que ese mercado no tiene colección equivalente.
CREATE TABLE IF NOT EXISTS category_links (
    master_gid text NOT NULL,
    store_code text NOT NULL,
    collection_gid text,
    status text NOT NULL CHECK (status IN ('CONFIRMED','NONE')),
    linked_by text NOT NULL DEFAULT 'local',
    linked_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (master_gid, store_code),
    CHECK ((status = 'CONFIRMED') = (collection_gid IS NOT NULL))
);
-- Una colección de una tienda solo puede ser homóloga de una maestra.
CREATE UNIQUE INDEX IF NOT EXISTS category_links_target_idx
    ON category_links (store_code, collection_gid) WHERE collection_gid IS NOT NULL;
