#!/usr/bin/env python3
"""MCP stdio bridge for the Newgarden Content Hub HTTP API."""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


BASE_URL = os.environ.get("CONTENT_HUB_URL", "http://127.0.0.1:8787").rstrip("/")

MCP_INSTRUCTIONS = (
    "For Shopify product translation or localization requests, the agent is the translator. "
    "Do not delegate translation to a server-side provider. Use get_product_by_sku to read the source, "
    "list_stores/list_shop_locales to find destinations, and get_translation_context for each target "
    "store and locale. Translate with your own language model while preserving the returned glossary terms. "
    "Write existing product fields with edit_translation; use shopify_admin_graphql for product creation, "
    "variants, metafields, or other Admin API mutations. Execute requested writes directly and report results "
    "per store and locale."
)


TOOLS = [
    {"name": "list_stores", "description": "Lista tiendas y locales configurados en Content Hub.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_shop_locales", "description": "Consulta en Shopify los locales habilitados para una tienda y señala cuáles están publicados y cuál es el principal.", "inputSchema": {"type": "object", "properties": {"store": {"type": "string", "minLength": 2}, "published_only": {"type": "boolean"}}, "required": ["store"]}},
    {"name": "get_translation_context", "description": "Devuelve el locale publicado, perfil de tono y términos de glosario aplicables para que el agente haga la traducción con su propio modelo. No llama a DeepL ni a otro traductor.", "inputSchema": {"type": "object", "properties": {"store": {"type": "string", "minLength": 2}, "locale": {"type": "string", "minLength": 2}, "source_store": {"type": "string", "minLength": 2}, "source_locale": {"type": "string", "minLength": 2}}, "required": ["store", "locale"]}},
    {"name": "search_products", "description": "Busca productos Shopify por SKU, texto o GID.", "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "minLength": 2}}, "required": ["query"]}},
    {"name": "get_product_by_sku", "description": "Lee en una tienda el producto que contiene el SKU exacto, con variantes, imágenes, SEO, metacampos y traducciones configuradas. Usa esta herramienta para obtener el texto que el agente traducirá.", "inputSchema": {"type": "object", "properties": {"store": {"type": "string", "minLength": 2}, "sku": {"type": "string", "minLength": 1}}, "required": ["store", "sku"]}},
    {"name": "search_shop_products", "description": "Busca productos dentro de una tienda concreta por texto, SKU o sintaxis de búsqueda Shopify. Devuelve variantes/SKU y cursor de paginación.", "inputSchema": {"type": "object", "properties": {"store": {"type": "string", "minLength": 2}, "search": {"type": "string", "minLength": 1}, "first": {"type": "integer", "minimum": 1, "maximum": 50}, "after": {"type": "string"}}, "required": ["store", "search"]}},
    {"name": "search_orders", "description": "Busca pedidos de una tienda concreta usando sintaxis de búsqueda Shopify (por ejemplo name:#1001, created_at o fulfillment_status). Incluye estados, total y líneas, sin datos personales del cliente.", "inputSchema": {"type": "object", "properties": {"store": {"type": "string", "minLength": 2}, "search": {"type": "string"}, "first": {"type": "integer", "minimum": 1, "maximum": 50}, "after": {"type": "string"}}, "required": ["store"]}},
    {"name": "get_inventory_levels", "description": "Consulta inventario por InventoryItem GID o ID numérico en una tienda, por ubicación y estado de cantidad. Devuelve cursor de paginación.", "inputSchema": {"type": "object", "properties": {"store": {"type": "string", "minLength": 2}, "inventory_item_id": {"type": "string", "minLength": 1}, "first": {"type": "integer", "minimum": 1, "maximum": 100}, "after": {"type": "string"}}, "required": ["store", "inventory_item_id"]}},
    {"name": "shopify_admin_graphql", "description": "Ejecuta una operación GraphQL Admin (query o mutation) en la tienda indicada usando su conexión ya configurada. Usa variables GraphQL para valores dinámicos y operation_name si el documento incluye varias operaciones. Devuelve data, errors y extensions de Shopify, incluidos userErrors de mutaciones. La versión API es la configurada por Content Hub.", "inputSchema": {"type": "object", "properties": {"store": {"type": "string", "minLength": 2}, "query": {"type": "string", "minLength": 1, "maxLength": 100000}, "variables": {"type": "object"}, "operation_name": {"type": "string", "maxLength": 255}}, "required": ["store", "query"]}},
    {"name": "compare_product", "description": "Lee y compara un producto de ES con productos/locales de destino. source_id es un GID Shopify.", "inputSchema": {"type": "object", "properties": {"source_id": {"type": "string"}, "target_store": {"type": "string"}, "target_locale": {"type": "string"}}, "required": ["source_id"]}},
    {"name": "get_translation_batch", "description": "Consulta estado, propuestas y errores de un lote.", "inputSchema": {"type": "object", "properties": {"batch_id": {"type": "integer"}}, "required": ["batch_id"]}},
    {"name": "edit_translation", "description": "Guarda inmediatamente un valor en Shopify para el locale indicado. resource_id/owner_id son GID; key puede ser title, body_html, handle, seo_title, seo_description, vendor, product_type o value para metacampo.", "inputSchema": {"type": "object", "properties": {"store": {"type": "string"}, "resource_id": {"type": "string"}, "owner_id": {"type": "string"}, "locale": {"type": "string"}, "key": {"type": "string"}, "value": {"type": "string"}, "namespace": {"type": "string"}, "metafield_key": {"type": "string"}, "metafield_type": {"type": "string"}, "compare_digest": {"type": "string"}, "source_value": {"type": "string"}, "source_product_gid": {"type": "string"}}, "required": ["store", "resource_id", "owner_id", "locale", "key", "value"]}},
]


SEARCH_SHOP_PRODUCTS_QUERY = """query SearchShopProducts($search: String!, $first: Int!, $after: String) {
  products(first: $first, query: $search, after: $after) {
    nodes { id title handle status vendor productType variants(first: 100) { nodes { sku } } }
    pageInfo { hasNextPage endCursor }
  }
}"""

SEARCH_ORDERS_QUERY = """query SearchOrders($search: String, $first: Int!, $after: String) {
  orders(first: $first, query: $search, after: $after, reverse: true) {
    nodes {
      id name createdAt updatedAt displayFinancialStatus displayFulfillmentStatus
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 50) { nodes { title quantity sku } }
    }
    pageInfo { hasNextPage endCursor }
  }
}"""

GET_INVENTORY_LEVELS_QUERY = """query GetInventoryLevels($id: ID!, $first: Int!, $after: String) {
  inventoryItem(id: $id) {
    id sku tracked
    variants(first: 1) { nodes { id title product { id title } } }
    inventoryLevels(first: $first, after: $after) {
      nodes {
        id location { id name }
        quantities(names: ["available", "committed", "incoming", "on_hand", "reserved"]) { name quantity }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""

SHOP_LOCALES_QUERY = """query StoreLocales($published: Boolean) {
  shopLocales(published: $published) { locale name primary published }
}"""


def api(method, path, data=None):
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE_URL + path, data=body, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail", str(exc))
        except Exception:
            detail = str(exc)
        raise RuntimeError(f"Content Hub HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"No se pudo conectar con Content Hub en {BASE_URL}: {exc}")
    return json.loads(raw) if raw else {}


def shopify_graphql(store, query, variables=None, operation_name=None):
    return api("POST", "/api/shopify/graphql", {
        "store": store,
        "query": query,
        "variables": variables or {},
        "operation_name": operation_name,
    })


def has_shopify_errors(value):
    if isinstance(value, dict):
        if value.get("errors"):
            return True
        user_errors = value.get("userErrors")
        if isinstance(user_errors, list) and user_errors:
            return True
        return any(has_shopify_errors(item) for item in value.values())
    if isinstance(value, list):
        return any(has_shopify_errors(item) for item in value)
    return False


def call_tool(name, args):
    if name == "list_stores": return api("GET", "/api/stores")
    if name == "list_shop_locales":
        data=shopify_graphql(args["store"],SHOP_LOCALES_QUERY,{"published":True if args.get("published_only") else None})
        return data
    if name == "get_translation_context":
        params={k:args[k] for k in ("store","locale","source_store","source_locale") if args.get(k)}
        return api("GET", "/api/translation-context?"+urllib.parse.urlencode(params))
    if name == "get_product_by_sku":
        return api("GET", "/api/shopify/products/by-sku?"+urllib.parse.urlencode({"store":args["store"],"sku":args["sku"]}))
    if name == "search_products": return api("GET", "/api/shopify/products/search?q=" + urllib.parse.quote(args["query"]))
    if name == "shopify_admin_graphql": return shopify_graphql(args["store"], args["query"], args.get("variables"), args.get("operation_name"))
    if name == "search_shop_products":
        data=shopify_graphql(args["store"],SEARCH_SHOP_PRODUCTS_QUERY,{
            "search":args["search"],"first":max(1,min(int(args.get("first",20)),50)),"after":args.get("after")
        })
        return data
    if name == "search_orders":
        data=shopify_graphql(args["store"],SEARCH_ORDERS_QUERY,{
            "search":args.get("search") or None,"first":max(1,min(int(args.get("first",20)),50)),"after":args.get("after")
        })
        return data
    if name == "get_inventory_levels":
        inventory_id=args["inventory_item_id"].strip()
        if inventory_id.isdecimal(): inventory_id=f"gid://shopify/InventoryItem/{inventory_id}"
        data=shopify_graphql(args["store"],GET_INVENTORY_LEVELS_QUERY,{
            "id":inventory_id,"first":max(1,min(int(args.get("first",50)),100)),"after":args.get("after")
        })
        return data
    if name == "compare_product":
        query = {k: args[k] for k in ("target_store", "target_locale") if args.get(k)}
        return api("GET", "/api/shopify/products/compare?" + urllib.parse.urlencode({"source_id": args["source_id"], **query}))
    if name == "get_translation_batch": return api("GET", f"/api/translation-batches/{int(args['batch_id'])}")
    if name == "edit_translation": return api("POST", "/api/shopify/translations", args)
    raise ValueError(f"Herramienta desconocida: {name}")


def send(message):
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def read_message():
    line = sys.stdin.buffer.readline()
    return json.loads(line) if line else None


def handle(msg):
    method, params = msg.get("method"), msg.get("params", {})
    ident = msg.get("id")
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "newgarden-content-hub", "version": "1.1.0"}, "instructions": MCP_INSTRUCTIONS}
    if method == "ping": return {}
    if method == "tools/list": return {"tools": TOOLS}
    if method == "tools/call":
        try:
            result = call_tool(params.get("name"), params.get("arguments") or {})
            response={"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}], "structuredContent": result}
            if has_shopify_errors(result): response["isError"]=True
            return response
        except Exception as exc:
            return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    if method == "notifications/initialized" or method == "notifications/cancelled": return None
    if ident is None: return None
    return {"error": {"code": -32601, "message": f"Método no soportado: {method}"}}


def main():
    while True:
        msg = read_message()
        if msg is None: break
        result = handle(msg)
        if "id" in msg and result is not None:
            if "error" in result: send({"jsonrpc": "2.0", "id": msg["id"], **result})
            else: send({"jsonrpc": "2.0", "id": msg["id"], "result": result})


if __name__ == "__main__":
    main()
