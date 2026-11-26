"""Documentación de la API.

- `enrich(schema)`: ordena el OpenAPI (etiquetas por grupo y títulos en español) para quien lo lea, humano o agente.
- `plain_page(schema)`: `/docs`, HTML semántico sin estilos ni JavaScript, pensado para leer con agentes de IA.
- `PAGE_BODY`: `/api-docs`, la documentación para el equipo (secciones, buscador, ejemplos y pruebas). Se pinta en el
  navegador a partir de `/openapi.json`, así que nunca queda desfasada respecto a las rutas reales.
"""
import html, json

SKU = 'Emparejar SKU MX↔ES'

# (etiqueta, descripción, coincidencia por prefijo de ruta). El primer grupo que coincide gana.
GROUPS = [
    (SKU, 'Vincula cada variante de MX (SKU y EAN propios) con su SKU de ES para que MCP, Business Central y n8n hablen el mismo código. Propuestas, revisión humana y consumo del mapa.', ()),
    ('Estado del servicio', 'Comprobación de salud.', ('/health',)),
    ('Glosario', 'Términos preferidos de traducción y sus candidatos a revisar.', ('/api/glossary',)),
    ('Tiendas y perfiles', 'Tiendas Shopify conectadas, idiomas y perfiles de traducción.', ('/api/stores', '/api/translation-profiles')),
    ('Shopify y traducción', 'Lectura y escritura directa en Shopify, contexto de traducción y traducción de celdas.', ('/api/shopify', '/api/translate-cell', '/api/translation-context')),
    ('Lotes de traducción', 'Preparar, revisar, aprobar y aplicar traducciones por lotes.', ('/api/translation-batches',)),
    ('Campañas de traducción', 'Traducciones programadas sobre muchos SKU, con reintentos.', ('/api/translation-campaigns',)),
    ('Cola de trabajos y productos', 'Cola de traducción, control del proceso y ficha de producto.', ('/api/jobs', '/api/process', '/api/products')),
    ('Control de stock', 'Consulta y ajuste de inventario, con programaciones.', ('/api/stock',)),
    ('Etiquetado masivo', 'Búsqueda de productos y aplicación de etiquetas en bloque.', ('/api/tags',)),
    ('Colecciones', 'Búsqueda y traducción de colecciones.', ('/api/collections',)),
    ('Categorías homólogas', 'Vínculos de categorías maestras con las colecciones de cada tienda.', ('/api/admin/categories',)),
    ('Administración masiva', 'Edición de productos, variantes, colecciones y metacampos en varias tiendas, con vista previa y registro de operaciones.', ('/api/admin',)),
    ('Pantallas web', 'Páginas HTML del hub. No son API; se listan para saber qué ruta abre cada pantalla.', ()),
]
TAGS = [{'name': n, 'description': d} for n, d, _ in GROUPS]

SUMMARIES = {
    'GET /health': 'Comprobar que el servicio y la base de datos responden',
    'GET /': 'Pantalla: resumen', 'GET /stores': 'Pantalla: tiendas', 'GET /glossary': 'Pantalla: glosario',
    'GET /glossary/review': 'Pantalla: revisar candidatos del glosario', 'GET /products': 'Pantalla: gestión de productos',
    'GET /products/batch': 'Pantalla: traducción en lote', 'GET /translation-batches': 'Pantalla: revisión de lotes',
    'GET /translation-campaigns': 'Pantalla: campañas', 'GET /stock': 'Pantalla: control de stock', 'GET /tags': 'Pantalla: etiquetado masivo',
    'GET /collections': 'Pantalla: traducir colecciones', 'GET /admin': 'Pantalla: administración',
    'GET /api/glossary-candidates': 'Listar candidatos del glosario',
    'POST /api/glossary-candidates/{candidate_id}/approve': 'Aprobar un candidato del glosario',
    'POST /api/glossary-candidates/apply-ai': 'Aplicar en bloque las sugerencias de IA',
    'POST /api/glossary-candidates/{candidate_id}/reject': 'Rechazar un candidato del glosario',
    'POST /api/glossary-candidates/{candidate_id}/reopen': 'Reabrir un candidato del glosario',
    'GET /api/glossary': 'Listar términos del glosario', 'POST /api/glossary': 'Añadir un término al glosario',
    'DELETE /api/glossary/{term_id}': 'Borrar un término del glosario',
    'GET /api/stores': 'Listar tiendas', 'PUT /api/stores': 'Crear o actualizar una tienda',
    'GET /api/translation-profiles': 'Listar perfiles de traducción',
    'GET /api/stores/{code}/shop-locales': 'Idiomas publicados de una tienda',
    'GET /api/shopify/metafield-definitions': 'Definiciones de metacampos de texto de ES',
    'GET /api/shopify/products/search': 'Buscar productos en Shopify',
    'GET /api/shopify/products/by-sku': 'Obtener un producto por SKU',
    'POST /api/shopify/graphql': 'Ejecutar una consulta GraphQL de Shopify Admin',
    'GET /api/shopify/products/compare': 'Comparar un producto de origen con su traducción',
    'POST /api/translate-cell': 'Traducir un texto suelto', 'GET /api/translation-context': 'Contexto para que un agente traduzca por su cuenta',
    'POST /api/shopify/translations': 'Guardar una traducción en Shopify',
    'POST /api/translation-batches/resolve': 'Resolver SKU de un lote a productos',
    'POST /api/translation-batches/parse-skus': 'Leer SKU de un archivo xlsx, csv o txt',
    'GET /api/translation-batches': 'Listar lotes', 'POST /api/translation-batches': 'Crear un lote',
    'GET /api/translation-batches/search': 'Buscar lotes por SKU', 'GET /api/translation-batches/{batch_id}': 'Detalle de un lote',
    'PATCH /api/translation-batches/{batch_id}/items/{item_id}': 'Editar un elemento del lote',
    'POST /api/translation-batches/{batch_id}/approve': 'Aprobar elementos del lote',
    'POST /api/translation-batches/{batch_id}/reopen': 'Reabrir elementos del lote',
    'POST /api/translation-batches/{batch_id}/reject': 'Rechazar el lote',
    'POST /api/translation-batches/{batch_id}/apply': 'Aplicar el lote en Shopify',
    'POST /api/products/import': 'Importar un producto', 'GET /api/products/{product_key}': 'Detalle de un producto',
    'GET /api/translation-campaigns': 'Listar campañas', 'POST /api/translation-campaigns': 'Crear una campaña',
    'GET /api/translation-campaigns/{campaign_id}': 'Detalle de una campaña',
    'POST /api/translation-campaigns/{campaign_id}/cancel': 'Cancelar una campaña',
    'POST /api/translation-campaigns/{campaign_id}/resume': 'Reanudar una campaña',
    'POST /api/translation-campaigns/{campaign_id}/retry-failed': 'Reintentar los SKU fallidos de una campaña',
    'POST /api/jobs/enqueue': 'Encolar trabajos de traducción', 'GET /api/jobs': 'Listar trabajos',
    'GET /api/jobs/{job_id}/results': 'Resultados de un trabajo', 'GET /api/jobs/{job_id}/publications': 'Publicaciones de un trabajo',
    'POST /api/jobs/{job_id}/approve': 'Aprobar un trabajo', 'POST /api/jobs/{job_id}/publish': 'Publicar un trabajo',
    'POST /api/process/start': 'Iniciar el proceso de traducción', 'POST /api/process/continue': 'Reanudar el proceso de traducción',
    'POST /api/process/stop': 'Detener el proceso de traducción', 'GET /api/process': 'Estado del proceso de traducción',
    'GET /api/stock/lookup': 'Consultar el stock de un SKU', 'POST /api/stock/apply': 'Aplicar o programar un ajuste de stock',
    'POST /api/stock/schedules/{schedule_id}/cancel': 'Cancelar un ajuste programado',
    'GET /api/tags/collections': 'Colecciones disponibles para etiquetar', 'GET /api/tags/search': 'Buscar productos para etiquetar',
    'POST /api/tags/apply': 'Aplicar etiquetas en bloque',
    'GET /api/collections/search': 'Buscar colecciones', 'GET /api/collections/plan': 'Plan de traducción de una colección',
    'POST /api/collections/translate': 'Traducir una colección',
    'GET /api/admin/stores': 'Tiendas disponibles para administrar', 'GET /api/admin/products': 'Listar productos de una tienda',
    'GET /api/admin/product': 'Detalle de un producto', 'GET /api/admin/collections': 'Listar colecciones de una tienda',
    'GET /api/admin/collection': 'Detalle de una colección',
    'GET /api/admin/metafield-definitions': 'Definiciones de metacampos por tienda',
    'POST /api/admin/product/update': 'Actualizar un producto', 'POST /api/admin/collection/update': 'Actualizar una colección',
    'POST /api/admin/variant/update': 'Actualizar una variante', 'POST /api/admin/metafield/set': 'Escribir un metacampo',
    'POST /api/admin/metafield/delete': 'Vaciar un metacampo',
    'GET /api/admin/operations': 'Listar operaciones registradas', 'GET /api/admin/operations/summary': 'Resumen de operaciones',
    'GET /api/admin/operations/{op_id}': 'Detalle de una operación',
    'POST /api/admin/operations/{op_id}/retry-failed': 'Reintentar lo que falló en una operación',
    'POST /api/admin/global/preview': 'Edición global: vista previa', 'POST /api/admin/global/apply': 'Edición global: aplicar',
    'POST /api/admin/global/sku-check': 'Edición global: comprobar SKU', 'POST /api/admin/global/find': 'Edición global: buscar por nombre',
    'POST /api/admin/duplicates/search': 'Duplicados: buscar', 'POST /api/admin/duplicates/apply': 'Duplicados: aplicar',
    'POST /api/admin/grid/search': 'Cuadrícula: buscar', 'POST /api/admin/grid/apply': 'Cuadrícula: aplicar',
    'POST /api/admin/prices/find': 'Precios: buscar producto', 'POST /api/admin/prices/search': 'Precios: buscar variantes',
    'POST /api/admin/prices/apply': 'Precios: aplicar',
    'POST /api/admin/definitions/create': 'Copiar una definición de metacampo a otras tiendas',
    'GET /api/admin/translation-locales': 'Idiomas de traducción activos por tienda',
    'GET /api/admin/values/page': 'Valores de un metacampo por variante (página de 250)',
    'POST /api/admin/values/apply': 'Asignar un valor a una lista de variantes',
    'GET /api/admin/collection/product-ids': 'Identificadores de producto de una colección',
    'GET /api/admin/categories/masters': 'Categorías maestras y estado de sus vínculos',
    'GET /api/admin/categories/group': 'Una categoría maestra con su homóloga por tienda',
    'GET /api/admin/categories/suggest': 'Sugerir categoría homóloga',
    'GET /api/admin/categories/skus': 'Comparar los SKU de una categoría entre tiendas',
    'GET /api/admin/categories/skus.csv': 'Descargar en CSV las incidencias de SKU',
    'POST /api/admin/categories/edit/preview': 'Edición de categoría: vista previa',
    'POST /api/admin/categories/edit/apply': 'Edición de categoría: aplicar',
    'POST /api/admin/categories/link': 'Guardar, cambiar o quitar un vínculo de categoría',
}


def group_for(path, tagged=None):
    if tagged: return tagged
    for name, _, prefixes in GROUPS:
        if any(path == p or path.startswith(p + '/') or path.startswith(p + '-') for p in prefixes): return name
    return 'Pantallas web' if not path.startswith('/api') else 'Otros'


def enrich(schema):
    """Etiqueta por grupo y título en español para las rutas que no los tenían. No toca lo ya escrito a mano."""
    schema.setdefault('info', {})['description'] = (
        'API del Content Hub de Newgarden. Documentación para el equipo en `/api-docs`; esta especificación es la fuente de ambas. '
        'Sin login: acceso solo desde la red local. Las rutas de escritura actúan directamente sobre Shopify o sobre la base del hub.')
    present = set()
    for path, ops in schema.get('paths', {}).items():
        for method, op in ops.items():
            if not isinstance(op, dict): continue
            tag = group_for(path, (op.get('tags') or [None])[0])
            op['tags'] = [tag]; present.add(tag)
            title = SUMMARIES.get(f'{method.upper()} {path}')
            if title and not (op.get('tags') and tag == SKU): op['summary'] = title
    schema['tags'] = [t for t in TAGS if t['name'] in present] + ([{'name': 'Otros', 'description': 'Sin clasificar.'}] if 'Otros' in present else [])
    return schema


# ---------------------------------------------------------------- /docs (texto plano, para agentes)
RESP = {'Successful Response': 'Correcto', 'Validation Error': 'Parámetros no válidos'}


def _resolve(schema, node):
    while isinstance(node, dict) and '$ref' in node:
        cur = schema
        for part in node['$ref'].lstrip('#/').split('/'): cur = cur[part]
        node = cur
    return node


def _type(schema, node):
    node = _resolve(schema, node) or {}
    if 'anyOf' in node or 'oneOf' in node:
        return ' | '.join(sorted({_type(schema, n) for n in node.get('anyOf', node.get('oneOf'))}))
    t = node.get('type', 'object' if 'properties' in node else 'any')
    if t == 'array': return f"list[{_type(schema, node.get('items', {}))}]"
    if node.get('enum'): return t + ' (' + '|'.join(map(str, node['enum'])) + ')'
    return t


def _fields(schema, node):
    node = _resolve(schema, node) or {}
    req = set(node.get('required', []))
    return [(k, _type(schema, v), k in req, (_resolve(schema, v) or {}).get('description', '')) for k, v in (node.get('properties') or {}).items()]


def plain_page(schema):
    e = html.escape
    out = ['<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           '<title>Newgarden Content Hub · API (texto plano)</title></head><body><main>',
           '<h1>Newgarden Content Hub · API</h1>',
           '<p>Versión de texto plano pensada para agentes y scripts. La documentación visual para el equipo está en <a href="/api-docs">/api-docs</a>; '
           'la especificación completa en <a href="/openapi.json">/openapi.json</a>.</p>',
           '<p>Sin autenticación (red local). Las respuestas son JSON salvo que se indique otra cosa. '
           'Las rutas POST, PUT, PATCH y DELETE escriben: en Shopify o en la base del hub.</p>']
    tags = {t['name']: t.get('description', '') for t in schema.get('tags', [])}
    by = {}
    for path, ops in schema['paths'].items():
        for method, op in ops.items():
            if isinstance(op, dict): by.setdefault(op['tags'][0], []).append((path, method.upper(), op))
    out.append('<nav><h2>Índice</h2><ul>' + ''.join(f'<li><a href="#g{i}">{e(n)}</a> ({len(by[n])})</li>' for i, n in enumerate(tags) if n in by) + '</ul></nav>')
    for i, n in enumerate(n for n in tags if n in by):
        out.append(f'<section><h2 id="g{i}">{e(n)}</h2><p>{e(tags[n])}</p>')
        for path, method, op in by[n]:
            out.append(f'<h3>{method} {e(path)}</h3><p>{e(op.get("summary", ""))}</p>')
            if op.get('description'): out.append(f'<pre>{e(op["description"].strip())}</pre>')
            params = op.get('parameters', [])
            if params:
                out.append('<p>Parámetros:</p><ul>' + ''.join(
                    f'<li>{e(p["name"])} ({e(p["in"])}, {e(_type(schema, p.get("schema", {})))}, {"obligatorio" if p.get("required") else "opcional"})'
                    f'{" — " + e(p["description"]) if p.get("description") else ""}</li>' for p in params) + '</ul>')
            body = (op.get('requestBody') or {}).get('content', {})
            for ctype, spec in body.items():
                fields = _fields(schema, spec.get('schema', {}))
                out.append(f'<p>Cuerpo ({e(ctype)}):</p>')
                out.append('<ul>' + ''.join(f'<li>{e(k)} ({e(t)}, {"obligatorio" if r else "opcional"}){" — " + e(d) if d else ""}</li>' for k, t, r, d in fields) + '</ul>' if fields else '<p>Objeto libre.</p>')
            out.append('<p>Respuestas:</p><ul>' + ''.join(f'<li>{e(code)} — {e(RESP.get(r.get("description", ""), r.get("description", "")))}</li>' for code, r in op.get('responses', {}).items()) + '</ul>')
        out.append('</section>')
    out.append('</main></body></html>')
    return ''.join(out)


# ---------------------------------------------------------------- /api-docs (para el equipo)
GUIDE = r"""
<section class="card guide" id="guia">
<h2>Guía: emparejar SKU de MX con ES</h2>
<p>MX usa SKU y EAN propios por motivos legales, así que no hay una clave común con las tiendas europeas. El hub guarda, variante a variante, qué SKU de ES equivale a cada SKU de MX. Esa tabla es la que consumen MCP, Business Central y n8n.</p>
<ol class="steps">
<li><strong>Proponer.</strong> <code>POST /api/sku-candidates/recompute</code> descarga ES y MX (solo lectura) y calcula propuestas con una puntuación. Se consulta el avance en <code>recompute-status</code>.</li>
<li><strong>Revisar.</strong> En <a href="/sku-links">Emparejar SKU</a> una persona aprueba, corrige, rechaza o marca «sin equivalente». Solo lo aprobado entra en la tabla permanente.</li>
<li><strong>Consumir.</strong> <code>resolve</code> traduce un SKU suelto; <code>export</code> devuelve el mapa completo en CSV o JSON.</li>
</ol>
<h3>Sufijos organizativos</h3>
<p>En ES un SKU puede llevar <code>XMAS</code> (Navidad), <code>IND</code> (uso interior), <code>ACC</code> (accesorio) o <code>NG1</code>, <code>NG2</code>, <code>NG3</code> (genéricos). Son una organización de Shopify: <strong>no existen en Business Central</strong>. Por eso cada respuesta distingue dos campos:</p>
<table class="doc-table"><thead><tr><th>Campo</th><th>Qué es</th><th>Quién lo usa</th></tr></thead><tbody>
<tr><td><code>es_sku</code> / <code>bc_sku</code></td><td>Código sin sufijo</td><td>Business Central</td></tr>
<tr><td><code>es_shopify_sku</code></td><td>SKU exacto de la variante en Shopify, con sufijo</td><td>Shopify, MCP, localizar la variante</td></tr></tbody></table>
<p>Ejemplo: la variante MX <code>LUMAM040ICNW</code> se vincula a <code>LUMAM040ICNWIND</code> en Shopify, y a BC llega <code>LUMAM040ICNW</code>.</p>
<h3>Ejemplos</h3>
<div class="proto"><div class="proto-head"><span>Resolver un SKU de MX</span></div><pre class="code" data-copy>curl -s "<span data-origin></span>/api/sku-links/resolve?store=MX&amp;sku=LUMAM040ICNW"</pre></div>
<div class="proto"><div class="proto-head"><span>Mapa completo para BC o n8n</span></div><pre class="code" data-copy>curl -s "<span data-origin></span>/api/sku-links/export" -o mapa-sku.csv</pre>
<pre class="code">mx_sku,mx_ean,es_sku,es_shopify_sku,qty,kind,status
LUMAM040ICNW,,LUMAM040ICNW,LUMAM040ICNWIND,1,single,CONFIRMED</pre></div>
<p class="muted">Un SKU de MX puede equivaler a varios de ES (packs y kits): el export trae una fila por componente, con su <code>qty</code>. Si es <code>NONE</code> no hay equivalente en ES.</p>
</section>
"""

PAGE_BODY = r"""<style>
.ad{display:grid;grid-template-columns:260px minmax(0,1fr);gap:32px;align-items:start}
.ad-nav{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto;padding-right:6px}
.ad-nav input{width:100%;margin:0 0 12px}
.ad-nav ul{list-style:none;margin:0;padding:0}
.ad-nav a{display:flex;justify-content:space-between;gap:8px;padding:7px 10px;border-radius:4px;color:var(--ink);text-decoration:none;font-size:13.5px;line-height:1.4}
.ad-nav a:hover{background:var(--surface)}
.ad-nav a span{color:var(--muted);font:11.5px/1.6 var(--mono)}
.ad-main{min-width:0}
.ad-group{margin:0 0 40px}
.ad-group>h2{margin:0 0 6px}
.ad-group>p{margin:0 0 16px;color:var(--muted);max-width:75ch}
.op{border:1px solid var(--line);border-radius:6px;background:var(--card);margin:0 0 10px;overflow:hidden}
.op>summary{display:grid;grid-template-columns:64px minmax(0,1.4fr) minmax(0,1fr);gap:14px;align-items:center;padding:12px 16px;cursor:pointer;list-style:none}
.op>summary::-webkit-details-marker{display:none}
.op>summary:hover{background:var(--paper)}
.op[open]>summary{border-bottom:1px solid var(--line)}
.op .path{font:600 13px/1.5 var(--mono);overflow-wrap:anywhere}
.op .sum{font-size:13.5px;color:var(--muted)}
.m{display:inline-block;min-width:54px;text-align:center;padding:3px 0;border-radius:3px;font:700 11px/1.6 var(--mono);color:#fff;background:#626665}
.m.GET{background:var(--green)}.m.POST{background:#1f5f8b}.m.PUT,.m.PATCH{background:var(--orange)}.m.DELETE{background:var(--accent)}
.op-body{padding:18px 20px 22px;display:grid;gap:18px}
.op-body h4{margin:0 0 8px;font:600 12px/1.5 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.op-desc p{margin:0 0 10px;max-width:80ch}.op-desc ul{margin:0 0 10px;padding-left:20px}
.doc-table{width:100%;border-collapse:collapse;font-size:13.5px}
.doc-table th,.doc-table td{padding:8px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
.doc-table th{font:600 11.5px/1.5 var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--muted);white-space:nowrap}
.doc-table td:first-child{white-space:nowrap}
.req{color:var(--accent);font:11px/1.5 var(--mono)}
.proto{border:1px solid var(--line);border-radius:6px;overflow:hidden;background:var(--paper)}
.proto-head{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:6px 8px 6px 12px;border-bottom:1px solid var(--line);font:12px/1.5 var(--mono);color:var(--muted)}
.tabs{display:flex;gap:4px;flex-wrap:wrap}
.tabs button{min-height:30px;padding:3px 10px;font-size:12px;color:var(--ink);background:transparent;border:1px solid transparent;border-radius:3px}
.tabs button[aria-selected=true]{background:#fff;border-color:var(--line)}
.copy{min-height:30px;padding:3px 10px;font-size:12px}
pre.code{margin:0;padding:14px 16px;overflow:auto;font:12.5px/1.6 var(--mono);white-space:pre;max-height:380px;background:var(--paper)}
.try{display:grid;gap:10px}
.try .fields{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:4px 14px}
.try .field{margin:4px 0}
.try .out{margin:0;padding:14px 16px;border:1px solid var(--line);border-radius:6px;background:#fff;font:12.5px/1.6 var(--mono);white-space:pre-wrap;overflow:auto;max-height:420px}
.try-note{font-size:12.5px;color:var(--muted)}
.guide .steps{padding-left:20px}.guide .steps li{margin:0 0 8px}
.guide h3{margin:22px 0 8px}
.ad-empty{padding:24px;color:var(--muted)}
@media (max-width:980px){.ad{grid-template-columns:1fr}.ad-nav{position:static;max-height:none}.op>summary{grid-template-columns:60px minmax(0,1fr)}.op .sum{grid-column:1/-1}}
</style>
<main class="wrap"><h1>Documentación de la API</h1>
<p class="lede">Todas las rutas del hub, organizadas por área, con ejemplos listos para copiar y una consola para probar las consultas. Se genera desde la especificación real, así que no se desfasa. Para agentes de IA hay una <a href="/docs">versión en texto plano</a> y el <a href="/openapi.json">OpenAPI en JSON</a>.</p>
<div class="ad"><nav class="ad-nav" aria-label="Secciones de la API"><label class="muted" for="ad-q">Buscar una ruta</label><input id="ad-q" type="search" placeholder="sku, stock, /api/glossary…" autocomplete="off"><ul id="ad-index"></ul></nav>
<div class="ad-main" id="ad-main"><p class="ad-empty">Cargando especificación…</p></div></div></main>
<script>
const GUIDE=__GUIDE__;
const RESP={'Successful Response':'Correcto','Validation Error':'Parámetros no válidos'};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const origin=location.origin;
let spec=null;
const ref=n=>{while(n&&n.$ref){n=n.$ref.slice(2).split('/').reduce((a,k)=>a[k],spec)}return n||{}};
const typ=n=>{n=ref(n);if(n.anyOf||n.oneOf)return [...new Set((n.anyOf||n.oneOf).map(typ))].join(' | ');if(n.type==='array')return 'list['+typ(n.items||{})+']';if(n.enum)return (n.type||'')+' ('+n.enum.join('|')+')';return n.type||(n.properties?'object':'any')};
function sample(n,d=0){n=ref(n);if(n.example!==undefined)return n.example;if(n.examples&&n.examples.length)return n.examples[0];if(n.default!==undefined&&n.default!==null)return n.default;if(n.anyOf||n.oneOf){const f=(n.anyOf||n.oneOf).find(x=>ref(x).type!=='null');return sample(f||{},d)}
 if(n.enum)return n.enum[0];if(n.type==='array')return d>2?[]:[sample(n.items||{},d+1)];
 if(n.type==='object'||n.properties){const o={};if(d<3)for(const [k,v] of Object.entries(n.properties||{}))o[k]=sample(v,d+1);return o}
 return {string:'texto',integer:1,number:1,boolean:true}[n.type]??null}
function md(t){t=(t||'').trim();if(!t)return '';const inl=s=>esc(s).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
 return t.split(/\n\s*\n/).map(b=>{const l=b.split('\n').map(x=>x.trim());if(l.every(x=>/^[-*] /.test(x)))return '<ul>'+l.map(x=>'<li>'+inl(x.slice(2))+'</li>').join('')+'</ul>';
 const i=l.findIndex(x=>/^[-*] /.test(x));if(i>0)return '<p>'+inl(l.slice(0,i).join(' '))+'</p><ul>'+l.slice(i).map(x=>'<li>'+inl(x.replace(/^[-*] /,''))+'</li>').join('')+'</ul>';return '<p>'+inl(l.join(' '))+'</p>'}).join('')}
function opParts(path,m,op){const params=(op.parameters||[]).map(p=>({name:p.name,in:p.in,type:typ(p.schema||{}),required:!!p.required,desc:p.description||'',def:(ref(p.schema||{})).default,ex:((ref(p.schema||{})).examples||[])[0]}));
 const bc=(op.requestBody||{}).content||{};const ct=Object.keys(bc)[0];const bs=ct?ref(bc[ct].schema||{}):null;
 const fields=bs&&bs.properties?Object.entries(bs.properties).map(([k,v])=>({name:k,type:typ(v),required:(bs.required||[]).includes(k),desc:ref(v).description||''})):[];
 const body=ct&&ct.includes('json')?sample(bc[ct].schema||{}):null;return {params,ct,fields,body}}
function urlFor(path,params,vals){let u=path;const q=[];params.forEach(p=>{const v=vals?(vals[p.name]??''):(p.required||p.in==='path'?(p.ex??'<'+p.name+'>'):'');if(p.in==='path')u=u.replace('{'+p.name+'}',v||(p.ex??'<'+p.name+'>'));else if(p.in==='query'&&(vals?v!=='':p.required))q.push(encodeURIComponent(p.name)+'='+(vals?encodeURIComponent(v):v))});return u+(q.length?'?'+q.join('&'):'')}
function protos(path,m,op){const {params,ct,body}=opParts(path,m,op);const u=origin+urlFor(path,params);const hasBody=ct&&ct.includes('json');const js=hasBody?JSON.stringify(body,null,2):null;
 const curl='curl -s'+(m==='GET'?'':' -X '+m)+' "'+u+'"'+(hasBody?" \\\n  -H 'Content-Type: application/json' \\\n  -d '"+JSON.stringify(body).replace(/'/g,"'\\''")+"'":'');
 const py='import requests\n\nr = requests.'+m.toLowerCase()+'(\n    "'+u+'",'+(hasBody?'\n    json='+JSON.stringify(body,null,4).replace(/\n/g,'\n    ').replace(/\btrue\b/g,'True').replace(/\bfalse\b/g,'False').replace(/\bnull\b/g,'None')+',':'')+'\n    timeout=60,\n)\nr.raise_for_status()\nprint(r.json())';
 const n8n=JSON.stringify({method:m,url:u,sendBody:!!hasBody,...(hasBody?{specifyBody:'json',jsonBody:JSON.stringify(body)}:{}),options:{}},null,2);
 return [['curl',curl],['Python',py],['n8n · HTTP Request',n8n]]}
function protoHtml(path,m,op){const ps=protos(path,m,op);return '<div class="proto"><div class="proto-head"><div class="tabs" role="tablist">'+ps.map((p,i)=>'<button type="button" role="tab" aria-selected="'+(i===0)+'" data-t="'+i+'">'+esc(p[0])+'</button>').join('')+'</div><button type="button" class="secondary copy">Copiar</button></div>'+ps.map((p,i)=>'<pre class="code" role="tabpanel" data-p="'+i+'"'+(i?' hidden':'')+'>'+esc(p[1])+'</pre>').join('')+'</div>'}
function tryHtml(path,m,op,id){const {params}=opParts(path,m,op);
 if(m!=='GET')return '<p class="try-note">Las rutas que escriben no se pueden lanzar desde aquí. Usa los ejemplos de arriba.</p>';
 return '<div class="try"><div class="fields">'+params.map(p=>'<div class="field"><label for="'+id+p.name+'">'+esc(p.name)+(p.required?' <span class="req">obligatorio</span>':'')+'</label><input id="'+id+p.name+'" data-name="'+esc(p.name)+'" placeholder="'+esc(p.type)+'" value="'+esc(p.def??p.ex??'')+'" autocomplete="off"></div>').join('')+'</div><div><button type="button" class="run">Probar petición</button> <span class="status" role="status"></span></div><pre class="out" hidden></pre></div>'}
function opHtml(path,m,op,i){const {params,ct,fields,body}=opParts(path,m,op);const id='o'+i+'_';
 let h='<div class="op-body">';
 if(op.description)h+='<div class="op-desc">'+md(op.description)+'</div>';
 if(params.length)h+='<div><h4>Parámetros</h4><table class="doc-table"><thead><tr><th>Nombre</th><th>En</th><th>Tipo</th><th>Descripción</th></tr></thead><tbody>'+params.map(p=>'<tr><td><code>'+esc(p.name)+'</code>'+(p.required?' <span class="req">obligatorio</span>':'')+'</td><td>'+esc(p.in)+'</td><td>'+esc(p.type)+'</td><td>'+esc(p.desc)+(p.def!==undefined&&p.def!==null?' <span class="muted">Por defecto: '+esc(p.def)+'</span>':'')+'</td></tr>').join('')+'</tbody></table></div>';
 if(ct)h+='<div><h4>Cuerpo · '+esc(ct)+'</h4>'+(fields.length?'<table class="doc-table"><thead><tr><th>Campo</th><th>Tipo</th><th>Descripción</th></tr></thead><tbody>'+fields.map(f=>'<tr><td><code>'+esc(f.name)+'</code>'+(f.required?' <span class="req">obligatorio</span>':'')+'</td><td>'+esc(f.type)+'</td><td>'+esc(f.desc)+'</td></tr>').join('')+'</tbody></table>':'<p class="muted">Objeto libre.</p>')+'</div>';
 h+='<div><h4>Respuestas</h4><table class="doc-table"><tbody>'+Object.entries(op.responses||{}).map(([c,r])=>'<tr><td><code>'+esc(c)+'</code></td><td>'+esc(RESP[r.description]||r.description||'')+'</td></tr>').join('')+'</tbody></table></div>';
 h+='<div><h4>Ejemplos de petición</h4>'+protoHtml(path,m,op)+'</div><div><h4>Probar</h4>'+tryHtml(path,m,op,id)+'</div></div>';return h}
function render(){const q=document.querySelector('#ad-q').value.trim().toLowerCase();const groups={};let n=0;
 for(const [path,ops] of Object.entries(spec.paths))for(const [m,op] of Object.entries(ops)){if(typeof op!=='object')continue;const hay=(path+' '+(op.summary||'')+' '+m).toLowerCase();if(q&&!hay.includes(q))continue;(groups[op.tags[0]]=groups[op.tags[0]]||[]).push([path,m.toUpperCase(),op,n++])}
 const tags=spec.tags.filter(t=>groups[t.name]);
 document.querySelector('#ad-index').innerHTML=(q?'':'<li><a href="#guia">Guía de SKU<span>guía</span></a></li>')+tags.map((t,i)=>'<li><a href="#g'+i+'">'+esc(t.name)+'<span>'+groups[t.name].length+'</span></a></li>').join('');
 document.querySelector('#ad-main').innerHTML=(q?'':GUIDE)+(tags.length?tags.map((t,i)=>'<section class="ad-group" id="g'+i+'"><h2>'+esc(t.name)+'</h2><p>'+esc(t.description||'')+'</p>'+groups[t.name].map(([path,m,op,k])=>'<details class="op" data-k="'+k+'"><summary><span class="m '+m+'">'+m+'</span><span class="path">'+esc(path)+'</span><span class="sum">'+esc(op.summary||'')+'</span></summary></details>').join('')+'</section>').join(''):'<p class="ad-empty">Ninguna ruta coincide con «'+esc(q)+'».</p>');
 document.querySelectorAll('[data-origin]').forEach(e=>e.textContent=origin);
 document.querySelectorAll('.op').forEach(d=>{const [path,m,op,k]=Object.values(groups).flat().find(x=>x[3]==+d.dataset.k);d.addEventListener('toggle',()=>{if(d.open&&!d.querySelector('.op-body'))d.insertAdjacentHTML('beforeend',opHtml(path,m,op,k))},{once:false});d._info=[path,m,op]});
 if(q)document.querySelectorAll('.op').forEach((d,i)=>{if(i<3)d.open=true})}
document.addEventListener('click',async e=>{
 const t=e.target.closest('.tabs button');if(t){const box=t.closest('.proto');box.querySelectorAll('.tabs button').forEach(b=>b.setAttribute('aria-selected',b===t));box.querySelectorAll('pre[data-p]').forEach(p=>p.hidden=p.dataset.p!==t.dataset.t);return}
 const c=e.target.closest('.copy');if(c){const p=c.closest('.proto').querySelector('pre:not([hidden])');try{await navigator.clipboard.writeText(p.textContent);c.textContent='Copiado'}catch{c.textContent='Selecciona y copia'}setTimeout(()=>c.textContent='Copiar',1500);return}
 const r=e.target.closest('.run');if(r){const d=r.closest('.op');const [path,m,op]=d._info;const {params}=opParts(path,m,op);const vals={};d.querySelectorAll('[data-name]').forEach(i=>vals[i.dataset.name]=i.value.trim());
  const miss=params.find(p=>p.required&&!vals[p.name]);const st=d.querySelector('.try .status'),out=d.querySelector('.try .out');if(miss){st.textContent='Falta «'+miss.name+'».';return}
  st.textContent='Consultando…';r.disabled=true;try{const res=await fetch(urlFor(path,params,vals));const txt=await res.text();let shown=txt;try{shown=JSON.stringify(JSON.parse(txt),null,2)}catch{}
   out.hidden=false;out.textContent=(shown.length>20000?shown.slice(0,20000)+'\n… (recortado)':shown);st.textContent='HTTP '+res.status+' · '+txt.length+' bytes';st.style.color=res.ok?'':'var(--accent)'}catch(err){st.textContent=err.message}finally{r.disabled=false}}});
document.querySelector('#ad-q').addEventListener('input',()=>render());
fetch('/openapi.json').then(r=>r.json()).then(s=>{spec=s;render()}).catch(e=>document.querySelector('#ad-main').innerHTML='<p class="ad-empty">No se pudo cargar la especificación: '+esc(e.message)+'</p>');
</script>"""


def page_body():
    return PAGE_BODY.replace('__GUIDE__', json.dumps(GUIDE))
