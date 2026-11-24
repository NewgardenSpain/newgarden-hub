from decimal import Decimal
from typing import Any
import hashlib, json, logging, os, html, re, threading, time, uuid
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, field_validator
import psycopg.errors
from psycopg.types.json import Jsonb
from .db import pool, initialize
from .skufile import extract_skus
from .quality import assess_translation, strip_added_quotes
from .profiles import active_profiles, list_active_profiles, target_profile

def operator():
    # This service is intended to be reachable only on the trusted local network.
    return 'local'

@asynccontextmanager
async def lifespan(app):
    initialize()
    # Recover abandoned RUNNING work: only a complete persisted result can finish it.
    with pool.connection() as c:
        c.execute("UPDATE localization_jobs j SET state=CASE WHEN EXISTS(SELECT 1 FROM localization_results r WHERE r.job_id=j.id AND r.output IS NOT NULL AND r.validation->>'ok'='true') THEN 'READY_FOR_REVIEW' ELSE 'PENDING' END, updated_at=now() WHERE state='RUNNING'")
        c.execute("UPDATE process_control SET state='STOPPED',updated_at=now() WHERE singleton AND state='RUNNING'")
        # A product that was being prepared when the service stopped goes back to the queue.
        c.execute("UPDATE translation_campaign_items SET state='PENDING',started_at=NULL WHERE state='RUNNING'")
        c.execute("UPDATE stock_schedules SET state='PENDING' WHERE state='RUNNING'")
        c.execute("UPDATE translation_batches SET state='READY_WITH_ERRORS',error='Interrumpido por un reinicio del servicio',updated_at=now() WHERE state='PREPARING'")
    threading.Thread(target=_campaign_runner,name='campaign-runner',daemon=True).start()
    threading.Thread(target=_stock_runner,name='stock-runner',daemon=True).start()
    yield

app=FastAPI(title='Newgarden Content Hub',version='0.1.0',lifespan=lifespan)
class ImportProduct(BaseModel):
    product_key:str=Field(min_length=1,max_length=120)
    source:dict
    mappings:dict[str,dict]=Field(default_factory=dict)
class Enqueue(BaseModel): product_key:str; destinations:list[str]
class Fields(BaseModel): fields:list[str]
class CellTranslation(BaseModel):
    store:str=Field(pattern=r'^[A-Z0-9_-]{2,12}$')
    resource_id:str=Field(min_length=8,max_length=255)
    owner_id:str=Field(min_length=8,max_length=255)
    locale:str=Field(min_length=2,max_length=20)
    key:str=Field(min_length=1,max_length=255)
    value:str=Field(max_length=200000)
    namespace:str|None=Field(default=None,max_length=255)
    metafield_key:str|None=Field(default=None,max_length=255)
    metafield_type:str|None=Field(default=None,max_length=100)
    compare_digest:str|None=Field(default=None,max_length=255)
    source_value:str|None=Field(default=None,max_length=200000)
    source_product_gid:str|None=Field(default=None,max_length=255)
class GlossaryTerm(BaseModel):
    source_store:str=Field(min_length=2,max_length=12)
    source_locale:str=Field(default='es-ES',min_length=2,max_length=20)
    target_store:str=Field(min_length=2,max_length=12)
    target_locale:str=Field(min_length=2,max_length=20)
    source_term:str=Field(min_length=1,max_length=500)
    target_term:str=Field(min_length=1,max_length=500)
    enabled:bool=True
class AIApplyRequest(BaseModel):
    store:str|None=None
    locale:str|None=None
    verdicts:list[str]=Field(default_factory=list)
class CandidateDecision(BaseModel):
    target_term:str=Field(min_length=1,max_length=500)
class CellTranslateRequest(BaseModel):
    source_text:str=Field(min_length=1,max_length=120000)
    source_store:str=Field(default='ES',min_length=2,max_length=12)
    source_locale:str=Field(default='es-ES',min_length=2,max_length=20)
    target_store:str=Field(min_length=2,max_length=12)
    target_locale:str=Field(min_length=2,max_length=20)
    html:bool=False
    field_key:str=Field(min_length=1,max_length=255)
    metafield_type:str|None=None
BATCH_BASE_FIELDS={'title','handle','descriptionHtml','seo.title','seo.description'}
class TranslationBatchRequest(BaseModel):
    sku:str=Field(min_length=1,max_length=120)
    # Optional campaign targeting. Without `fields` the batch keeps its original
    # scope (description, SEO and every metafield). Accepted values: the base keys
    # above, `metafields` (all of them) or `metafield:<namespace>.<key>`.
    fields:list[str]|None=Field(default=None,min_length=1,max_length=100)
    store:str|None=Field(default=None,pattern=r'^[A-Z0-9_-]{2,12}$')
    locale:str|None=Field(default=None,min_length=2,max_length=20)
    stores:list[str]|None=Field(default=None,max_length=100)
    locales:list[str]|None=Field(default=None,max_length=100)
    # Other SKUs of the same product and whether variant metafields are limited
    # to the variants of `sku` and `also_skus` instead of every variant.
    also_skus:list[str]=Field(default_factory=list,max_length=50)
    variants_of_skus_only:bool=False
    # Traduce y escribe directamente en Shopify, sin pasar por la revisión del hub.
    auto_apply:bool=False
    # Tiendas donde, si el producto no existe (por SKU), se prepara su creación como
    # producto ARCHIVADO. Se crea al preparar el lote, sin aprobación previa: el estado
    # archivado es la revisión humana (nada queda publicado).
    create_stores:list[str]=Field(default_factory=list,max_length=100)

    @field_validator('create_stores')
    @classmethod
    def check_create_stores(cls,value):
        for item in value:
            if not re.fullmatch(r'[A-Z0-9_-]{2,12}',item) or item=='ES': raise ValueError(f'Tienda no válida para crear productos: {item}')
        return list(dict.fromkeys(value))

    @field_validator('fields')
    @classmethod
    def check_fields(cls,value):
        if value is None: return value
        cleaned=[]
        for item in value:
            item=item.strip()
            if item not in BATCH_BASE_FIELDS and item!='metafields' and not re.fullmatch(r'metafield:[^\s.:]{1,255}\.[^\s:]{1,255}',item):
                raise ValueError(f'Campo no válido: {item}')
            if item not in cleaned: cleaned.append(item)
        return cleaned

def batch_field_selected(key:str,specs:list[str]|None)->bool:
    if specs is None: return key in BATCH_BASE_FIELDS or key.startswith('metafield.')
    if key in specs: return True
    if not key.startswith('metafield.'): return False
    if 'metafields' in specs: return True
    for spec in specs:
        if not spec.startswith('metafield:'): continue
        ns_key=spec[len('metafield:'):]
        if key==f'metafield.product.{ns_key}' or (key.startswith('metafield.variant.') and key.endswith('.'+ns_key)): return True
    return False
class TranslationBatchResolve(BaseModel):
    skus:list[str]=Field(min_length=1,max_length=50)
class TranslationBatchItemIds(BaseModel):
    item_ids:list[int]=Field(min_length=1,max_length=500)
class TranslationBatchItemEdit(BaseModel):
    proposed_text:str=Field(max_length=200000)
class StoreConfig(BaseModel):
    code:str=Field(pattern=r'^[A-Z0-9_-]{2,12}$')
    domain:str=Field(min_length=3,max_length=255)
    token:str=Field(default='',max_length=500)
    locales:list[str]=Field(default_factory=list)
class ShopifyGraphQLRequest(BaseModel):
    store:str=Field(pattern=r'^[A-Z0-9_-]{2,12}$')
    query:str=Field(min_length=1,max_length=100000)
    variables:dict=Field(default_factory=dict)
    operation_name:str|None=Field(default=None,max_length=255)

    @field_validator('variables')
    @classmethod
    def check_variables_size(cls,value):
        if len(json.dumps(value,ensure_ascii=False).encode('utf-8'))>1_000_000:
            raise ValueError('Las variables GraphQL superan el límite de 1 MB')
        return value

# Shared presentation assets are bundled by Docker's COPY hub ./hub.
from pathlib import Path

STYLE = '<style>' + Path(__file__).with_name('ui.css').read_text(encoding='utf-8') + '</style>'

SHOPIFY_API_VERSION=os.getenv('SHOPIFY_API_VERSION','2026-07')

def shopify_graphql_payload(domain, token, query, variables=None, operation_name=None):
    url=f'https://{domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json'
    body={'query':query,'variables':variables or {}}
    if operation_name: body['operationName']=operation_name
    req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={'X-Shopify-Access-Token':token,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=25) as response: data=json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            payload=json.loads(exc.read())
            detail=payload.get('errors') or payload.get('error') or payload.get('message') or f'HTTP {exc.code}'
        except Exception:
            detail=f'HTTP {exc.code}'
        if isinstance(detail,(dict,list)): detail=json.dumps(detail,ensure_ascii=False)
        prefix='Shopify rechazó el token o los permisos de la tienda' if exc.code in (401,403) else f'Error HTTP {exc.code} al consultar Shopify'
        raise HTTPException(502,(prefix+': '+str(detail))[:700])
    except (urllib.error.URLError,TimeoutError): raise HTTPException(502,'No se pudo conectar con Shopify')
    if not isinstance(data,dict): raise HTTPException(502,'Shopify devolvió una respuesta GraphQL no válida')
    return data

def shopify_graphql(domain, token, query, variables=None):
    data=shopify_graphql_payload(domain,token,query,variables)
    if data.get('errors'): raise HTTPException(502,'Shopify GraphQL: '+str(data['errors'][0].get('message','error'))[:250])
    return data.get('data',{})

PRODUCT_SELECTION='''id title descriptionHtml handle vendor productType status tags createdAt updatedAt seo { title description } featuredMedia { preview { image { url altText } } } images(first:50) { nodes { id url altText } } variants(first:100) { nodes { id title sku barcode position selectedOptions { name value } } pageInfo { hasNextPage endCursor } } metafields(first:100) { nodes { id namespace key type value compareDigest description } pageInfo { hasNextPage endCursor } }'''

_TOKEN_CACHE={}
_TOKEN_LOCK=threading.Lock()

def env_credentials(config):
    """(client_id, secret) de una tienda con `credentials_env` en su configuración, o None."""
    prefix=(config or {}).get('credentials_env')
    if not prefix: return None
    client_id=os.getenv(f'{prefix}_CLIENT_ID','').strip(); secret=os.getenv(f'{prefix}_CLIENT_SECRET','').strip()
    return (client_id,secret) if client_id and secret else None

def store_token(domain,config):
    """Token de Admin API de una tienda. Si tiene `credentials_env`, se pide con client credentials y se
    renueva solo antes de caducar (duran 24 h); si no hay credenciales o falla, se usa `admin_token`."""
    static=(config or {}).get('admin_token') or ''
    credentials=env_credentials(config)
    if not credentials or not domain: return static
    with _TOKEN_LOCK:
        cached=_TOKEN_CACHE.get(domain)
        if cached and cached[1]>time.time(): return cached[0]
        try:
            request=urllib.request.Request(f'https://{domain}/admin/oauth/access_token',data=json.dumps({'client_id':credentials[0],'client_secret':credentials[1],'grant_type':'client_credentials'}).encode(),headers={'Content-Type':'application/json'},method='POST')
            with urllib.request.urlopen(request,timeout=20) as response: data=json.loads(response.read())
            token=data['access_token']
            # Se renueva 10 minutos antes de que caduque.
            _TOKEN_CACHE[domain]=(token,time.time()+max(int(data.get('expires_in',86400))-600,60))
            return token
        except Exception as exc:
            logging.getLogger('hub.shopify').warning('No se pudo renovar el token de %s: %s',domain,type(exc).__name__)
            return static

def shopify_stores():
    with pool.connection() as c:
        rows=c.execute("SELECT s.code,s.domain,s.config,COALESCE(array_agg(l.locale) FILTER (WHERE l.locale IS NOT NULL),'{}') FROM stores s LEFT JOIN store_locales l ON l.store_code=s.code WHERE s.active GROUP BY s.code ORDER BY s.code").fetchall()
    return [{'code':r[0],'domain':r[1],'token':store_token(r[1],r[2]) or '', 'locales':r[3]} for r in rows]

def shopify_store_by_code(code):
    with pool.connection() as c:
        row=c.execute("SELECT s.code,s.domain,s.config,COALESCE(array_agg(l.locale) FILTER (WHERE l.locale IS NOT NULL),'{}') FROM stores s LEFT JOIN store_locales l ON l.store_code=s.code WHERE s.code=%s AND s.active GROUP BY s.code",(code,)).fetchone()
    if not row: return None
    return {'code':row[0],'domain':row[1],'token':store_token(row[1],row[2]) or '', 'locales':row[3]}

def shopify_search(store, term, limit=12):
    if not store['domain'] or not store['token']: return []
    # Run separate Shopify searches: its search grammar does not support arbitrary
    # substring wildcards consistently, and combining filters can hide valid hits.
    safe=term.replace('\\','\\\\').replace('"','\\"')
    q=f'''query ProductSearch($query:String!) {{ products(first:{limit},query:$query) {{ nodes {{ id title handle vendor productType status variants(first:100) {{ nodes {{ sku }} }} }} }} }}'''
    found={}
    # Plain text invokes Shopify's broad product search and covers product names.
    queries=[term]
    # Explicit SKU filter improves exact SKU lookup without constraining text search.
    queries.append(f'sku:"{safe}"')
    numeric=term.removeprefix('gid://shopify/Product/')
    if numeric.isdecimal(): queries.append(f'id:{numeric}')
    errors=[]
    for search in queries:
        try:
            nodes=shopify_graphql(store['domain'],store['token'],q,{'query':search}).get('products',{}).get('nodes',[])
            found.update((node['id'],node) for node in nodes)
        except HTTPException as exc:
            errors.append(exc)
    if not found and errors: raise errors[0]
    return list(found.values())

def shopify_find_by_id(store, identifier):
    raw=identifier.removeprefix('gid://shopify/Product/')
    if not raw.isdecimal(): return None
    gid=f'gid://shopify/Product/{raw}'
    q='''query ProductById($id:ID!) { product(id:$id) { id title handle vendor productType status variants(first:20) { nodes { sku } } } }'''
    return shopify_graphql(store['domain'],store['token'],q,{'id':gid}).get('product')

def shopify_product(store, gid, locales):
    tr=' '.join(f't{n}:translations(locale:{json.dumps(locale)}) {{ key value outdated }}' for n,locale in enumerate(locales))
    mf_tr=' '.join(f'mt{n}:translations(locale:{json.dumps(locale)}) {{ key value outdated }}' for n,locale in enumerate(locales))
    selection=PRODUCT_SELECTION.replace('metafields(first:100) { nodes { id namespace key type value compareDigest description } }',f'metafields(first:100) {{ nodes {{ id namespace key type value compareDigest description {mf_tr} }} }}')
    q=f'''query ProductRead($id:ID!) {{ product(id:$id) {{ {selection} {tr} }} }}'''
    translation_error=None
    try: data=shopify_graphql(store['domain'],store['token'],q,{'id':gid}).get('product')
    except HTTPException as exc:
        if not locales: raise
        translation_error=exc.detail
        selection=PRODUCT_SELECTION
        q=f'''query ProductRead($id:ID!) {{ product(id:$id) {{ {selection} }} }}'''
        data=shopify_graphql(store['domain'],store['token'],q,{'id':gid}).get('product')
    if data:
        data['translation_error']=translation_error
        data['translations_by_locale']={locale:data.pop(f't{n}',[]) for n,locale in enumerate(locales)}
        def mf_selection():
            if translation_error: return ''
            return ' '.join(f'mt{n}:translations(locale:{json.dumps(locale)}) {{ key value outdated }}' for n,locale in enumerate(locales))
        def collect_connection(owner_id, field, initial):
            nodes=list(initial.get('nodes',[])); page=initial.get('pageInfo',{})
            while page.get('hasNextPage'):
                query=f'''query MetafieldPage($id:ID!,$after:String) {{ node(id:$id) {{ ... on {"Product" if field=="metafields" and owner_id==gid else "ProductVariant"} {{ {field}(first:100,after:$after) {{ nodes {{ id namespace key type value compareDigest description {mf_selection()} }} pageInfo {{ hasNextPage endCursor }} }} }} }} }}'''
                owner=shopify_graphql(store['domain'],store['token'],query,{'id':owner_id,'after':page.get('endCursor')}).get('node') or {}
                connection=owner.get(field,{})
                nodes.extend(connection.get('nodes',[])); page=connection.get('pageInfo',{})
                if not page.get('endCursor') and page.get('hasNextPage'): raise HTTPException(502,'Shopify no devolvió el cursor de metacampos')
            return nodes
        data['metafields']['nodes']=collect_connection(gid,'metafields',data.get('metafields',{}))
        variants=data.get('variants',{}).get('nodes',[])
        variant_page=data.get('variants',{}).get('pageInfo',{})
        while variant_page.get('hasNextPage'):
            page_query='''query VariantPage($id:ID!,$after:String) { product(id:$id) { variants(first:100,after:$after) { nodes { id title sku barcode position selectedOptions { name value } } pageInfo { hasNextPage endCursor } } } }'''
            page_data=shopify_graphql(store['domain'],store['token'],page_query,{'id':gid,'after':variant_page.get('endCursor')}).get('product') or {}
            connection=page_data.get('variants',{}); variants.extend(connection.get('nodes',[])); variant_page=connection.get('pageInfo',{})
            if not variant_page.get('endCursor') and variant_page.get('hasNextPage'): raise HTTPException(502,'Shopify no devolvió el cursor de variantes')
        data['variants']['nodes']=variants
        for variant in variants:
            variant['metafields']={'nodes':collect_connection(variant['id'],'metafields',{'nodes':[],'pageInfo':{'hasNextPage':True,'endCursor':None}})}
        for mf in data.get('metafields',{}).get('nodes',[]):
            mf['translations_by_locale']={locale:mf.pop(f'mt{n}',[]) for n,locale in enumerate(locales)}
        for variant in variants:
            for mf in variant.get('metafields',{}).get('nodes',[]):
                mf['translations_by_locale']={locale:mf.pop(f'mt{n}',[]) for n,locale in enumerate(locales)}
    return data

def shopify_locale_code(locale, store_code=''):
    # Shopify requires the locale tag enabled on that exact shop. Never infer
    # it from a country/store code (for example, es-MX may be es or es-419).
    return locale

def is_nl_pt_locale(locale):
    return locale.replace('_','-').split('-')[0].casefold() in ('nl','pt')

def shopify_shop_locales(store):
    query='''query EnabledShopLocales { shopLocales { locale name primary published } }'''
    return shopify_graphql(store['domain'],store['token'],query).get('shopLocales',[])

def product_field_value(product, key):
    if not product: return ''
    if key=='title': return product.get('title','')
    if key=='descriptionHtml': return product.get('descriptionHtml','')
    if key=='handle': return product.get('handle','')
    if key=='vendor': return product.get('vendor','')
    if key=='productType': return product.get('productType','')
    if key=='status': return product.get('status','')
    if key=='tags': return ', '.join(product.get('tags') or [])
    if key=='createdAt': return product.get('createdAt','')
    if key=='updatedAt': return product.get('updatedAt','')
    if key=='seo.title': return (product.get('seo') or {}).get('title','')
    if key=='seo.description': return (product.get('seo') or {}).get('description','')
    variants=product.get('variants',{}).get('nodes',[])
    if key=='variants.sku': return ', '.join(v.get('sku','') for v in variants if v.get('sku'))
    if key=='variants.barcode': return ', '.join(v.get('barcode','') for v in variants if v.get('barcode'))
    if key=='variants.title': return ', '.join(v.get('title','') for v in variants)
    if key=='images.altText': return ', '.join(i.get('altText') or '' for i in product.get('images',{}).get('nodes',[]))
    return ''

def shell(title, body, active=''):
    icons = {
        'home': '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
        'products': '<path d="m12 3 9 5v8l-9 5-9-5V8l9-5Z"/><path d="m3 8 9 5 9-5M12 13v8M7.5 5.5l9 5"/>',
        'gallery': '<path d="M14 3h7v7M21 3l-9 9M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/>',
        'batch': '<rect x="7" y="7" width="14" height="14" rx="2"/><path d="M17 7V3H3v14h4M11 12h6M11 16h6"/>',
        'campaigns': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
        'review': '<path d="M4 4h16v16H4zM8 9h8M8 13h5"/><path d="m14 17 2 2 4-4"/>',
        'glossary': '<path d="M12 5v16M12 5C8 2 4 3 2 4v15c3-1 6-1 10 2 4-3 7-3 10-2V4c-3-1-6-2-10 1Z"/>',
        'stores': '<path d="M3 10v11h18V10M2 10l2-7h16l2 7M2 10h20M9 21v-7h6v7"/>',
        'stock': '<path d="M3 7h18v13H3zM3 7l3-4h12l3 4M9 12h6"/>',
        'tags': '<path d="M3 12V3h9l9 9-9 9-9-9Z"/><circle cx="7.5" cy="7.5" r="1.5"/>',        'collections': '<rect x="3" y="3" width="13" height="13" rx="2"/><path d="M8 21h11a2 2 0 0 0 2-2V8"/>',
        'admin': '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M8 4v16"/>',
    }
    items = [('home','/','Resumen'),('admin','/admin','Administración'),('products','/products','Gestión de productos'),
             ('gallery','http://192.168.1.202:5678/webhook/panel-galeria','Panel de galería'),
             ('batch','/products/batch','Traducción en lote'),('campaigns','/translation-campaigns','Campañas'),('review','/translation-batches','Revisión de lotes'),('stock','/stock','Control de stock'),('tags','/tags','Etiquetado masivo'),('collections','/collections','Traducir colecciones'),('glossary','/glossary','Glosario'),('stores','/stores','Tiendas')]
    links = ''.join(f'<a class="nav-link {"active" if key==active else ""}" href="{url}" {"aria-current=page" if key==active else ""}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{icons[key]}</svg>{label}</a>' for key,url,label in items)
    mark = '<span class="dotmark" aria-hidden="true">' + '<i></i>' * 9 + '</span>'
    body = body.replace('<main class="wrap">', '<main class="wrap" id="main-content" tabindex="-1">', 1)
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#202121"><link rel="icon" href="/favicon.svg" type="image/svg+xml"><title>{html.escape(title)} · Newgarden Hub</title>{STYLE}</head><body><a class="skip-link" href="#main-content">Saltar al contenido</a><aside class="sidebar"><a class="brand" href="/" aria-label="Newgarden Content Hub, inicio">{mark}<span><strong translate="no">newgarden</strong><small>CONTENT HUB</small></span></a><nav aria-label="Navegación principal">{links}</nav><div class="sidebar-foot"><p>Contenido global.<br>Control en cada palabra.</p><span>NEWGARDEN / WORKSPACE</span></div></aside><div class="workspace"><header class="workspace-bar"><div class="breadcrumb"><span>Content Hub</span><span aria-hidden="true">/</span>{html.escape(title)}</div><span class="workspace-label">Espacio de trabajo interno</span></header>{body}<footer class="workspace-footer"><span>NEWGARDEN · CONTENT OPERATIONS</span><a href="/docs">Documentación API ↗</a></footer></div></body></html>'''

@app.get('/health')
def health():
    with pool.connection() as c: c.execute('SELECT 1')
    return {'status':'ok'}

FAVICON_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#202121"/><g fill="#f4f4f0" transform="translate(5 5) scale(.82)"><circle cx="10" cy="10" r="4"/><circle cx="32" cy="10" r="4"/><circle cx="54" cy="10" r="4"/><circle cx="10" cy="32" r="4"/><circle cx="32" cy="32" r="5"/><circle cx="54" cy="32" r="4"/><circle cx="10" cy="54" r="4"/><circle cx="32" cy="54" r="4"/><circle cx="54" cy="54" r="4"/></g></svg>'

@app.get('/favicon.svg',include_in_schema=False)
@app.get('/favicon.ico',include_in_schema=False)
def favicon():
    return Response(FAVICON_SVG, media_type='image/svg+xml', headers={'Cache-Control':'public, max-age=86400'})

@app.get('/',response_class=HTMLResponse)
def home():
    with pool.connection() as c:
        glossary_count=c.execute('SELECT count(*) FROM translation_glossary WHERE enabled').fetchone()[0]
        memory_count=c.execute('SELECT count(*) FROM translation_memory').fetchone()[0]
        store_count=c.execute('SELECT count(*) FROM stores WHERE active').fetchone()[0]
    deepl_ready=bool(os.getenv('DEEPL_AUTH_KEY','').strip())
    provider_status='Configurado' if deepl_ready else 'Falta configurar la clave'
    provider_class='ready' if deepl_ready else 'needs-setup'
    body=f'''<main class="wrap"><section class="overview-intro"><div><span class="section-label">Catálogo / Traducción / Revisión</span><h1>Tu catálogo.<br>En cada idioma.</h1><p class="lede">Un espacio para traducir, comparar y cuidar cada palabra de tu catálogo. Con DeepL y el criterio de tu equipo.</p><div class="actions"><a class="button primary" href="/products">Explorar productos <span aria-hidden="true">↗</span></a><a class="button secondary" href="/products/batch">Traducir en lote</a></div></div><section class="workflow" aria-label="Flujo de traducción"><div class="workflow-head"><span>DE ORIGEN A DESTINO</span><span class="dotmark" aria-hidden="true">{'<i></i>' * 9}</span></div><ol><li><div><strong>Encuentra el producto</strong><small>Consulta tu catálogo en las tiendas conectadas.</small></div></li><li><div><strong>Traduce con contexto</strong><small>Aplica tu glosario y las correcciones guardadas.</small></div></li><li><div><strong>Revisa y guarda</strong><small>Decide qué campos llegan a Shopify.</small></div></li></ol></section></section><section class="overview-section" aria-labelledby="system-title"><div class="section-heading"><h2 id="system-title">Tu espacio de trabajo</h2><span class="muted">Configuración y conocimiento compartido</span></div><div class="system-grid"><article class="system-item"><h3>Motor de traducción</h3><strong class="{provider_class}">DeepL · {provider_status}</strong><p class="muted">La clave permanece en el servidor.</p></article><article class="system-item"><h3>Memoria y glosario</h3><strong>{memory_count} textos · {glossary_count} términos</strong><p class="muted">Correcciones que ayudan en la siguiente traducción.</p></article><article class="system-item"><h3>Catálogo conectado</h3><strong>{store_count} tiendas activas</strong><p class="muted">Conexiones disponibles para consultar el catálogo.</p></article></div></section><div class="resource-links"><a class="resource-link" href="/glossary"><strong>Una voz, en todos los idiomas</strong><span class="muted">Gestiona tus términos preferidos en el glosario.</span><span aria-hidden="true">↗</span></a><a class="resource-link" href="/stores"><strong>Conecta tus mercados</strong><span class="muted">Configura las tiendas y sus idiomas disponibles.</span><span aria-hidden="true">↗</span></a></div></main>'''
    return shell('Resumen',body,'home')

@app.get('/stores',response_class=HTMLResponse)
def stores_page():
    body='''<main class="wrap"><h1>Tiendas Shopify</h1><p class="lede">Configura las conexiones y los idiomas disponibles para cada tienda.</p><div id="stores" class="grid"><div class="card">Cargando tiendas…</div></div><section class="card"><h2>Añadir tienda</h2><form id="store-form"><div class="grid"><div class="field"><label for="code">Código</label><input id="code" placeholder="FR" pattern="[A-Z0-9_-]{2,12}" required></div><div class="field"><label for="domain">Dominio Shopify</label><input id="domain" placeholder="mi-tienda.myshopify.com" required></div><div class="field"><label for="token">Token de acceso Admin API</label><input id="token" type="password" autocomplete="new-password" placeholder="shpat_…"></div><div class="field"><label for="locales">Locales (separados por coma)</label><input id="locales" placeholder="fr-FR, en-GB"></div></div><div class="actions"><button>Guardar tienda</button><span id="message" class="status" role="status"></span></div></form><p class="muted">Los tokens se guardan en la base de datos de esta instancia. El token vacío conserva el actual al editar.</p></section></main>
<script>
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function loadStores(){const r=await fetch('/api/stores');const data=await r.json();document.querySelector('#stores').innerHTML=data.map(s=>`<article class="card"><span class="badge">${s.active?'ACTIVA':'INACTIVA'}</span><h2>${esc(s.code)}</h2><p>${esc(s.domain||'Dominio sin configurar')}</p><p class="muted">Token: ${s.has_token?'•••••••• configurado':'sin configurar'} · Locales configurados: ${esc(s.locales.join(', ')||'ninguno')}</p><button class="secondary" data-code="${esc(s.code)}">Editar configuración</button> <button class="secondary" data-fetch-locales="${esc(s.code)}" ${!s.active||!s.has_token?'disabled':''}>Consultar locales Shopify</button><div class="muted" id="shop-locales-${esc(s.code)}"></div></article>`).join('')||'<div class="card">No hay tiendas configuradas.</div>';document.querySelectorAll('[data-code]').forEach(b=>b.addEventListener('click',()=>editStore(data.find(s=>s.code===b.dataset.code))));document.querySelectorAll('[data-fetch-locales]').forEach(button=>button.addEventListener('click',async()=>{const s=data.find(item=>item.code===button.dataset.fetchLocales),panel=document.querySelector('#shop-locales-'+s.code);panel.textContent='Consultando Shopify…';const response=await fetch('/api/stores/'+encodeURIComponent(s.code)+'/shop-locales'),result=await response.json();if(!response.ok){panel.textContent=result.detail||'No se pudieron consultar los locales';return}const selected=result.filter(item=>item.published);panel.innerHTML='<p><strong>Locales habilitados en Shopify:</strong></p><ul>'+result.map(item=>`<li><code>${esc(item.locale)}</code> · ${esc(item.name)}${item.primary?' · principal':''}${item.published?' · publicado':' · no publicado'}</li>`).join('')+'</ul><button type="button" class="secondary use-shop-locales">Cargar todos los locales publicados</button>';panel.querySelector('.use-shop-locales').addEventListener('click',()=>{editStore(s);document.querySelector('#locales').value=selected.map(item=>item.locale).join(', ')})}))}
function editStore(s){document.querySelector('#code').value=s.code;document.querySelector('#code').readOnly=true;document.querySelector('#domain').value=s.domain||'';document.querySelector('#token').value='';document.querySelector('#locales').value=s.locales.join(', ');document.querySelector('#store-form').scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});document.querySelector('#domain').focus({preventScroll:true})}
document.querySelector('#store-form').addEventListener('submit',async e=>{e.preventDefault();const payload={code:document.querySelector('#code').value.trim().toUpperCase(),domain:document.querySelector('#domain').value.trim(),token:document.querySelector('#token').value,locales:document.querySelector('#locales').value.split(',').map(x=>x.trim()).filter(Boolean)};const r=await fetch('/api/stores',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const m=document.querySelector('#message');if(r.ok){m.textContent='Configuración guardada';document.querySelector('#store-form').reset();document.querySelector('#code').readOnly=false;loadStores()}else{const x=await r.json();m.textContent=x.detail||'No se pudo guardar'}});loadStores();
</script>'''
    return shell('Tiendas',body,'stores')

@app.get('/glossary',response_class=HTMLResponse)
def glossary_page():
    body='''<main class="wrap"><h1>Glosario y aprendizaje</h1><p class="lede">Las reglas se limitan a una tienda origen, su locale y una tienda destino. El locale principal de Shopify queda seleccionado por defecto; puedes elegir otro locale publicado.</p><p><a class="button secondary" href="/glossary/review">Revisar candidatos del catálogo</a></p><section class="card"><h2>Añadir término</h2><form id="term-form"><div class="grid"><div class="field"><label for="source-store">Tienda origen</label><select id="source-store" name="source_store" required></select></div><div class="field"><label for="source-locale">Idioma origen</label><select id="source-locale" name="source_locale" required disabled><option value="">Elige una tienda</option></select></div><div class="field"><label for="target-store">Tienda destino</label><select id="target-store" name="target_store" required></select></div><div class="field"><label for="target-locale">Idioma destino</label><select id="target-locale" name="target_locale" required disabled><option value="">Elige una tienda</option></select></div><div class="field"><label for="source-term">Término origen</label><input id="source-term" name="source_term" required></div><div class="field"><label for="target-term">Traducción preferida</label><input id="target-term" name="target_term" required></div></div><p id="locale-status" class="muted" role="status"></p><button id="save-term">Guardar término</button> <span id="term-status" class="status" role="status"></span></form></section><section class="card"><h2>Términos guardados</h2><form id="term-search" class="grid"><div class="field"><label for="search-store">Tienda</label><select id="search-store"><option value="">Todas las tiendas</option></select></div><div class="field"><label for="search-q">Buscar término</label><input id="search-q" type="search" placeholder="Origen o traducción" autocomplete="off"></div></form><p id="terms-count" class="muted" role="status"></p><div id="terms" tabindex="0" role="region" aria-label="Términos guardados">Cargando…</div></section></main><script>
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));let stores=[];
const sourceStore=document.querySelector('#source-store'),targetStore=document.querySelector('#target-store'),sourceLocale=document.querySelector('#source-locale'),targetLocale=document.querySelector('#target-locale'),localeStatus=document.querySelector('#locale-status');
async function loadStores(){const r=await fetch('/api/stores'),data=await r.json();stores=data.filter(s=>s.active);const options=stores.map(s=>`<option value="${esc(s.code)}">${esc(s.code)} · ${esc(s.domain)}</option>`).join('');sourceStore.innerHTML=options;targetStore.innerHTML=options;document.querySelector('#search-store').innerHTML='<option value="">Todas las tiendas</option>'+stores.map(s=>`<option value="${esc(s.code)}">${esc(s.code)}</option>`).join('');await Promise.all([loadLocales(sourceStore.value,sourceLocale),loadLocales(targetStore.value,targetLocale)]);}
async function loadLocales(code,select){select.disabled=true;select.innerHTML='<option value="">Consultando Shopify…</option>';try{const r=await fetch('/api/stores/'+encodeURIComponent(code)+'/shop-locales'),data=await r.json();if(!r.ok)throw new Error(data.detail||'Shopify no devolvió los idiomas');const shopLocales=data.filter(x=>x.published);const configured=stores.find(s=>s.code===code)?.locales||[];const choices=shopLocales.filter(x=>configured.includes(x.locale));if(!choices.length)throw new Error('No hay locales publicados y configurados a la vez. Configúralos en Tiendas.');select.innerHTML=choices.map(x=>`<option value="${esc(x.locale)}">${esc(x.locale)}${x.primary?' · principal':''}</option>`).join('');const primary=choices.find(x=>x.primary);select.value=(primary||choices[0]).locale;select.disabled=false;localeStatus.textContent='Locales publicados en Shopify; el principal está marcado y seleccionado por defecto.';return true}catch(error){select.innerHTML='<option value="">No se pudieron validar los locales</option>';select.disabled=true;localeStatus.textContent=error.message+' · Revisa la conexión y los permisos read_locales en Tiendas.';return false}}
sourceStore.addEventListener('change',()=>loadLocales(sourceStore.value,sourceLocale));targetStore.addEventListener('change',()=>loadLocales(targetStore.value,targetLocale));
async function loadTerms(){const qs=new URLSearchParams();const ss=document.querySelector('#search-store').value,sq=document.querySelector('#search-q').value.trim();if(ss)qs.set('store',ss);if(sq)qs.set('q',sq);const r=await fetch('/api/glossary?'+qs);let d;try{d=await r.json()}catch{document.querySelector('#terms').textContent='La API devolvió una respuesta no válida';return}if(!r.ok){document.querySelector('#terms').textContent=d.detail||'No se pudo cargar el glosario';return}document.querySelector('#terms').innerHTML=d.terms.length?'<table><tr><th>Tienda origen</th><th>Idioma</th><th>Tienda destino</th><th>Idioma</th><th>Término</th><th>Traducción</th><th>Acciones</th></tr>'+d.terms.map(t=>`<tr><td>${esc(t.source_store||'Todas (regla antigua)')}</td><td>${esc(t.source_locale)}</td><td>${esc(t.target_store||'Todas (regla antigua)')}</td><td>${esc(t.target_locale)}</td><td>${esc(t.source_term)}</td><td>${esc(t.target_term)}</td><td><button class="secondary" onclick="removeTerm(${t.id})">Eliminar</button></td></tr>`).join('')+'</table>':'<p class="muted">'+(ss||sq?'Ningún término coincide con la búsqueda.':'Aún no hay términos.')+'</p>';document.querySelector('#terms-count').textContent=d.terms.length+' términos'}
async function removeTerm(id){const r=await fetch('/api/glossary/'+id,{method:'DELETE'});if(r.ok)loadTerms()}
document.querySelector('#term-form').addEventListener('submit',async e=>{e.preventDefault();const save=document.querySelector('#save-term');save.disabled=true;const data=Object.fromEntries(new FormData(e.target));const r=await fetch('/api/glossary',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});let result={};try{result=await r.json()}catch{}document.querySelector('#term-status').textContent=r.ok?'Guardado':(result.detail||'No se pudo guardar');save.disabled=false;if(r.ok){document.querySelector('#source-term').value='';document.querySelector('#target-term').value='';loadTerms()}});let tTimer;document.querySelector('#term-search').addEventListener('submit',e=>e.preventDefault());document.querySelector('#search-store').addEventListener('change',loadTerms);document.querySelector('#search-q').addEventListener('input',()=>{clearTimeout(tTimer);tTimer=setTimeout(loadTerms,300)});loadStores();loadTerms();
</script>'''
    return shell('Glosario',body,'glossary')

@app.get('/glossary/review',response_class=HTMLResponse)
def glossary_review_page():
    body="""<main class="wrap"><h1>Revisión de candidatos de glosario</h1><p class="lede">Términos propuestos a partir del catálogo ya traducido. Elige el valor canónico de cada tienda (puedes escribir otro o tomar una alternativa) y apruébalo para que se aplique en las traducciones. Nada se aplica hasta aprobarlo.</p><section class="card"><form id="filters"><div class="grid"><div class="field"><label for="f-store">Tienda · idioma destino</label><select id="f-store"></select></div><div class="field"><label for="f-q">Buscar término</label><input id="f-q" type="search" placeholder="Término ES, traducción o alternativa" autocomplete="off"></div><div class="field"><label for="f-status">Estado</label><select id="f-status"><option value="pending">Pendientes</option><option value="approved">Aprobados</option><option value="rejected">Rechazados</option><option value="">Todos</option></select></div><div class="field"><label for="f-kind">Tipo</label><select id="f-kind"><option value="">Todos</option><option value="term">Traducciones</option><option value="invariant">No traducir (nombres, modelos)</option></select></div><div class="field"><label for="f-conflicts">Inconsistencias</label><select id="f-conflicts"><option value="">Todas</option><option value="1">Solo con alternativas</option></select></div><div class="field"><label for="f-ai">Sugerencia</label><select id="f-ai"><option value="">Todas</option><option value="none">Sin sugerencia</option><option value="approve">Aprobar</option><option value="reject">Rechazar</option><option value="review">Revisar a mano</option></select></div></div></form><p id="summary" class="muted" role="status"></p><div class="toolbar"><button type="button" id="ai-apply">Aplicar sugerencias</button> <span id="ai-status" class="status" role="status"></span></div></section><section class="card"><div id="candidates" tabindex="0" role="region" aria-label="Candidatos de glosario">Cargando…</div></section></main><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const box=document.querySelector('#candidates'),summary=document.querySelector('#summary'),ids=['#f-store','#f-status','#f-kind','#f-conflicts','#f-ai'];
const badge={pending:'Pendiente',approved:'Aprobado',rejected:'Rechazado'};
const aiLabel={approve:'Sugerido: aprobar',reject:'Sugerido: rechazar',review:'Sugerido: revisar'};
const aiCell=c=>c.ai_verdict?`<span class="badge">${aiLabel[c.ai_verdict]}</span>${c.ai_verdict==='approve'&&c.ai_term&&c.ai_term!==c.target_term?`<br><button type="button" class="secondary pick" data-term="${esc(c.ai_term)}">${esc(c.ai_term)}</button>`:''}${c.ai_reason?`<br><small class="muted">${esc(c.ai_reason)}</small>`:''}`:'<small class="muted">Sin sugerencia</small>';
const post=async(url,body)=>{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'La petición falló');return d};
const scope=()=>{const [store,locale]=document.querySelector('#f-store').value.split('|');return {store:store||null,locale:locale||null}};
let busy=false;
document.querySelector('#ai-apply').addEventListener('click',async()=>{if(busy)return;const st=document.querySelector('#ai-status');const sc=scope();const label=sc.store?sc.store+' · '+sc.locale:'todas las tiendas';const verdicts=[];if(confirm('¿Aprobar en el glosario los pendientes que se sugiere aprobar ('+label+')? Se usará el valor sugerido.'))verdicts.push('approve');if(confirm('¿Rechazar también los pendientes que se sugiere rechazar ('+label+')?'))verdicts.push('reject');if(!verdicts.length)return;busy=true;try{const d=await post('/api/glossary-candidates/apply-ai',{...sc,verdicts});st.textContent='Aprobados: '+d.approved+' · rechazados: '+d.rejected}catch(err){st.textContent=err.message}busy=false;load()});
let qTimer;document.querySelector('#f-q').addEventListener('input',()=>{clearTimeout(qTimer);qTimer=setTimeout(load,300)});
async function load(){const q=new URLSearchParams();const v=id=>document.querySelector(id).value;if(v('#f-store')){const [st,lo]=v('#f-store').split('|');q.set('store',st);q.set('locale',lo)}if(v('#f-q').trim())q.set('q',v('#f-q').trim());if(v('#f-ai'))q.set('ai',v('#f-ai'));if(v('#f-status'))q.set('status',v('#f-status'));if(v('#f-kind'))q.set('kind',v('#f-kind'));if(v('#f-conflicts'))q.set('conflicts','1');
 let d;try{const r=await fetch('/api/glossary-candidates?'+q);d=await r.json();if(!r.ok)throw new Error(d.detail||'No se pudieron cargar los candidatos')}catch(e){box.textContent=e.message;return}
 const sel=document.querySelector('#f-store');if(sel.dataset.ready!=='1'){sel.innerHTML='<option value="">Todas</option>'+d.targets.map(t=>`<option value="${esc(t.store+'|'+t.locale)}">${esc(t.store)} · ${esc(t.locale)}</option>`).join('');sel.dataset.ready='1'}
 summary.textContent=d.candidates.length+' candidatos · '+Object.entries(d.counts).map(([k,n])=>(badge[k]||k)+': '+n).join(' · ');
 if(!d.candidates.length){box.innerHTML='<p class="muted">No hay candidatos con estos filtros.'+(v('#f-store')&&!Object.keys(d.counts).length?' Este idioma aún no tiene candidatos: se generan con <code>python -m hub.mine</code> a partir de traducciones históricas.':'')+'</p>';return}
 box.innerHTML='<table><tr><th>Tienda · idioma</th><th>Término ES</th><th>Valor canónico</th><th>Alternativas detectadas</th><th>Evidencia</th><th>Sugerencia</th><th>Estado</th><th>Acciones</th></tr>'+d.candidates.map(c=>{
  const alts=[{term:c.original_term,support:c.support,dice:c.dice,main:true},...c.alternatives.map(a=>({term:a.term,support:a.support,dice:a.dice}))];
  const locked=c.status!=='pending';
  return `<tr data-id="${c.id}"><td>${esc(c.target_store)} · ${esc(c.target_locale)}</td><td><strong>${esc(c.source_term)}</strong>${c.kind==='invariant'?'<br><span class="badge">No traducir</span>':''}</td><td><input class="canon" value="${esc(c.target_term)}" aria-label="Valor canónico de ${esc(c.source_term)}" ${locked?'disabled':''}>${c.existing&&c.existing!==c.target_term?`<br><small class="muted">En glosario: ${esc(c.existing)}</small>`:''}</td><td>${locked?'':alts.map(a=>`<button type="button" class="secondary pick" data-term="${esc(a.term)}" title="${a.support} productos · afinidad ${a.dice}">${esc(a.term)} <small>(${a.support})</small></button>`).join(' ')}</td><td>${c.support} de ${c.source_products} productos<br><small class="muted">afinidad ${c.dice}</small></td><td>${aiCell(c)}</td><td><span class="badge">${esc(badge[c.status]||c.status)}</span></td><td class="actions">${c.status==='pending'?'<button type="button" class="approve">Aprobar</button> <button type="button" class="secondary reject">Rechazar</button>':'<button type="button" class="secondary reopen">Reabrir</button>'}<span class="status" role="status"></span></td></tr>`}).join('')+'</table>'}
box.addEventListener('click',async e=>{const row=e.target.closest('tr[data-id]');if(!row)return;const status=row.querySelector('.status'),input=row.querySelector('.canon');
 const pick=e.target.closest('.pick');if(pick){input.value=pick.dataset.term;return}
 const action=['approve','reject','reopen'].find(a=>e.target.classList.contains(a));if(!action)return;
 const button=e.target;button.disabled=true;status.textContent='';
 try{const r=await fetch(`/api/glossary-candidates/${row.dataset.id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_term:input.value.trim()||'-'})});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.detail||'No se pudo guardar');load()}catch(err){status.textContent=err.message;button.disabled=false}});
document.querySelector('#filters').addEventListener('submit',e=>e.preventDefault());for(const id of ids)document.querySelector(id).addEventListener('change',load);load();
</script>"""
    return shell('Revisión de glosario',body,'glossary')

@app.get('/api/glossary-candidates')
def list_glossary_candidates(store:str|None=None,locale:str|None=None,status:str|None=None,kind:str|None=None,conflicts:str|None=None,q:str|None=None,ai:str|None=None,user=Depends(operator)):
    where=[]; params=[]
    if store: where.append('c.target_store=%s'); params.append(store)
    if locale: where.append('lower(c.target_locale)=lower(%s)'); params.append(locale)
    if status: where.append('c.status=%s'); params.append(status)
    if q and q.strip():
        where.append("(c.source_term ILIKE %s OR c.target_term ILIKE %s OR c.alternatives::text ILIKE %s)"); like='%'+q.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'; params+= [like,like,like]
    if ai=='none': where.append('c.ai_verdict IS NULL')
    elif ai in ('approve','reject','review'): where.append('c.ai_verdict=%s'); params.append(ai)
    if kind: where.append('c.kind=%s'); params.append(kind)
    if conflicts: where.append("jsonb_array_length(c.alternatives)>0")
    clause=('WHERE '+' AND '.join(where)) if where else ''
    with pool.connection() as c:
        rows=c.execute(f"""SELECT c.id,c.target_store,c.target_locale,c.source_term,c.target_term,c.kind,c.support,c.source_products,c.dice,c.alternatives,c.status,
              (SELECT g.target_term FROM translation_glossary g WHERE g.source_store='ES' AND g.target_store=c.target_store AND lower(g.target_locale)=lower(c.target_locale)
                 AND lower(g.source_term)=lower(c.source_term) AND g.enabled LIMIT 1),
              c.ai_verdict,c.ai_term,c.ai_reason
            FROM glossary_candidates c {clause}
            ORDER BY (jsonb_array_length(c.alternatives)>0) DESC,c.support DESC,c.id LIMIT 500""",params).fetchall()
        # Destinos = locales traducibles de los perfiles (incluye los de ES: nl, pt-PT…) + los que ya tienen candidatos.
        targets=[{'store':r[0],'locale':r[1]} for r in c.execute("""SELECT store_code,shopify_locale FROM translation_profiles WHERE active AND deepl_target IS NOT NULL
              UNION SELECT target_store,target_locale FROM glossary_candidates ORDER BY 1,2""").fetchall()]
        cwhere=[]; cparams=[]
        if store: cwhere.append('target_store=%s'); cparams.append(store)
        if locale: cwhere.append('lower(target_locale)=lower(%s)'); cparams.append(locale)
        counts=dict(c.execute('SELECT status,count(*) FROM glossary_candidates'+((' WHERE '+' AND '.join(cwhere)) if cwhere else '')+' GROUP BY status',cparams).fetchall())
    keys=('id','target_store','target_locale','source_term','target_term','kind','support','source_products','dice','alternatives','status','existing','ai_verdict','ai_term','ai_reason')
    out=[dict(zip(keys,r)) for r in rows]
    for item in out: item['original_term']=item['target_term']
    return {'candidates':out,'targets':targets,'counts':counts}

def _candidate_or_404(c,candidate_id):
    row=c.execute('SELECT id,target_store,target_locale,source_term,target_term,status FROM glossary_candidates WHERE id=%s FOR UPDATE',(candidate_id,)).fetchone()
    if not row: raise HTTPException(404,'Candidato no encontrado')
    return row

def _approve_candidate(c,candidate_id,store,locale,source_term,term):
    c.execute("""INSERT INTO translation_glossary(source_store,source_locale,target_store,target_locale,source_term,target_term,enabled,origin)
      VALUES('ES','es-ES',%s,%s,%s,%s,true,'historical_review')
      ON CONFLICT (source_store,source_locale,target_store,target_locale,(lower(source_term)))
      WHERE source_store IS NOT NULL AND target_store IS NOT NULL
      DO UPDATE SET target_term=EXCLUDED.target_term,enabled=true,origin=EXCLUDED.origin,updated_at=now()""",(store,locale,source_term,term))
    c.execute("UPDATE glossary_candidates SET status='approved',target_term=%s WHERE id=%s",(term,candidate_id))

@app.post('/api/glossary-candidates/{candidate_id}/approve')
def approve_glossary_candidate(candidate_id:int,x:CandidateDecision,user=Depends(operator)):
    term=x.target_term.strip()
    if not term: raise HTTPException(400,'Indica el valor canónico')
    with pool.connection() as c:
        _,store,locale,source_term,_,status=_candidate_or_404(c,candidate_id)
        _approve_candidate(c,candidate_id,store,locale,source_term,term)
    return {'approved':True}

@app.post('/api/glossary-candidates/apply-ai')
def apply_ai_glossary_suggestions(x:AIApplyRequest,user=Depends(operator)):
    """Aplica en bloque las sugerencias guardadas elegidas por la persona (aprobar y/o rechazar) sobre los pendientes filtrados."""
    verdicts=[v for v in x.verdicts if v in ('approve','reject')]
    if not verdicts: raise HTTPException(400,'Elige qué sugerencias aplicar')
    where=["status='pending'","ai_verdict=ANY(%s)"]; params=[verdicts]
    if x.store: where.append('target_store=%s'); params.append(x.store)
    if x.locale: where.append('lower(target_locale)=lower(%s)'); params.append(x.locale)
    approved=rejected=0
    with pool.connection() as c:
        rows=c.execute(f"SELECT id,target_store,target_locale,source_term,target_term,ai_verdict,ai_term FROM glossary_candidates WHERE {' AND '.join(where)} FOR UPDATE",params).fetchall()
        for cid,store,locale,source_term,target_term,verdict,ai_term in rows:
            if verdict=='approve':
                _approve_candidate(c,cid,store,locale,source_term,(ai_term or target_term).strip()); approved+=1
            else:
                c.execute("UPDATE glossary_candidates SET status='rejected' WHERE id=%s",(cid,)); rejected+=1
    return {'approved':approved,'rejected':rejected}

@app.post('/api/glossary-candidates/{candidate_id}/reject')
def reject_glossary_candidate(candidate_id:int,x:CandidateDecision|None=None,user=Depends(operator)):
    with pool.connection() as c:
        _candidate_or_404(c,candidate_id)
        c.execute("UPDATE glossary_candidates SET status='rejected' WHERE id=%s",(candidate_id,))
    return {'rejected':True}

@app.post('/api/glossary-candidates/{candidate_id}/reopen')
def reopen_glossary_candidate(candidate_id:int,x:CandidateDecision|None=None,user=Depends(operator)):
    with pool.connection() as c:
        _,store,locale,source_term,_,status=_candidate_or_404(c,candidate_id)
        # Solo retira la regla que creó esta revisión; los términos manuales no se tocan.
        if status=='approved':
            c.execute("DELETE FROM translation_glossary WHERE source_store='ES' AND target_store=%s AND target_locale=%s AND lower(source_term)=lower(%s) AND origin='historical_review'",(store,locale,source_term))
        c.execute("UPDATE glossary_candidates SET status='pending' WHERE id=%s",(candidate_id,))
    return {'reopened':True}

@app.get('/api/stores')
def list_stores():
    with pool.connection() as c:
        rows=c.execute("SELECT s.code,s.domain,s.active,s.config,COALESCE(array_agg(l.locale) FILTER (WHERE l.locale IS NOT NULL),'{}') FROM stores s LEFT JOIN store_locales l ON l.store_code=s.code GROUP BY s.code ORDER BY s.code").fetchall()
    return [{'code':r[0],'domain':r[1] or '', 'active':r[2], 'has_token':bool((r[3] or {}).get('admin_token')) or env_credentials(r[3]) is not None, 'locales':r[4]} for r in rows]

@app.get('/api/translation-profiles')
def list_translation_profiles():
    """Perfiles activos (tienda, locale, rol, idioma DeepL); solo lectura, para herramientas externas."""
    return [{k:p[k] for k in ('store_code','shopify_locale','role','deepl_target')} for p in list_active_profiles()]

@app.get('/api/stores/{code}/shop-locales')
def shop_locales(code:str):
    store=next((item for item in shopify_stores() if item['code']==code),None)
    if not store or not store['token']: raise HTTPException(404,'Tienda inactiva o sin credenciales Shopify')
    profiles=active_profiles(code)
    locales=[]
    for item in shopify_shop_locales(store):
        profile=profiles.get(item.get('locale','').lower())
        if profile: locales.append({**item,'role':profile['role'],'deepl_target':profile['deepl_target']})
    return sorted(locales,key=lambda item:(not item.get('primary',False),item.get('locale','')))

@app.get('/products',response_class=HTMLResponse)
def product_browser(q:str='',source_id:str=''):
    term=q.strip(); detail=''; results=''
    if source_id:
        try:
            data=compare_shopify_product(source_id)
            esc=lambda x:html.escape(str(x if x is not None else ''),quote=True)
            cols=''.join(f'<th>{esc(c["label"])}</th>' for c in data['columns'])
            rows=[]
            for f in data['fields']:
                cells=[]
                for column_index,value in enumerate(f['values']):
                    if value is None or value=='': shown='<span class="muted">—</span>'
                    else:
                        raw=', '.join(map(str,value)) if isinstance(value,list) else str(value)
                        display=html.unescape(re.sub(r'<[^>]*>',' ',raw)).strip() if f['key']=='descriptionHtml' else raw
                        safe=esc(display)
                        shown=safe if len(display)<=220 else f'<span>{esc(display[:220])}…</span><button type="button" class="more" data-full="{esc(display)}">Ver más</button>'
                    edit=f.get('edits',[None]*len(f['values']))[column_index]
                    if edit:
                        encoded=esc(json.dumps(edit,ensure_ascii=False))
                        states={'CURRENT':'Al día','OUTDATED':'Desactualizado','CONFLICT':'Cambio en destino','MISSING':'Falta en destino','UNTRACKED':'Sin referencia'}
                        state=edit.get('sync_status','UNTRACKED')
                        shown=f'<span class="cell-display">{shown}</span><span class="sync-state sync-{state.lower()}">{states.get(state,state)}</span><button type="button" class="cell-edit" data-edit="{encoded}">Editar celda</button>'
                    cells.append(f'<td>{shown}</td>')
                rows.append(f'<tr><th>{esc(f["label"])}<br><code>{esc(f["key"])}</code></th>{"".join(cells)}</tr>')
            notices=''.join(f'<p class="muted">{esc(n)}</p>' for n in data['notices'])
            detail=f'''<section class="card"><h2>Panel de producto e idiomas</h2><p class="lede">Compara y edita los campos por tienda e idioma. El estado se controla campo a campo: al día, desactualizado, falta en destino, cambio en destino o sin referencia histórica. Los datos anteriores a este panel se marcan sin referencia hasta volver a guardarlos desde aquí.</p><h3>{esc(data['source']['title'])}</h3><p class="muted">SKU: {esc(data['source']['sku'] or '—')} · {esc(data['source']['id'])}</p>{notices}<div class="comparison-scroll" tabindex="0" role="region" aria-label="Comparación de contenido por tienda e idioma"><table><thead><tr><th>Campo</th>{cols}</tr></thead><tbody>{''.join(rows)}</tbody></table></div><dialog class="edit-dialog t-modal" id="cell-editor" aria-labelledby="edit-title"><form method="dialog" id="cell-edit-form"><h2 id="edit-title">Editar contenido</h2><p id="edit-context" class="muted"></p><p class="html-note muted">La descripción se guarda como HTML. Puedes editar etiquetas como &lt;p&gt;, &lt;ul&gt; y &lt;strong&gt;.</p><textarea id="edit-value" aria-label="Contenido"></textarea><div class="actions"><button type="button" class="secondary" id="translate-cell">Traducir con DeepL</button><button type="submit" id="save-cell">Guardar en Shopify</button><button type="button" class="secondary" id="cancel-cell">Cancelar</button><span id="edit-status" class="status" role="status"></span></div></form></dialog></section><script>
const editor=document.querySelector('#cell-editor'),form=document.querySelector('#cell-edit-form'),area=document.querySelector('#edit-value');let editTarget=null;
document.querySelectorAll('.cell-edit').forEach(button=>button.addEventListener('click',()=>{{editTarget=JSON.parse(button.dataset.edit);area.value=editTarget.value||'';document.querySelector('#edit-context').textContent=editTarget.store+' · '+editTarget.locale+' · '+editTarget.key+' · '+(editTarget.mode==='base'?'Contenido base':'Traducción');editor.classList.toggle('html-mode',editTarget.key==='body_html');document.querySelector('#edit-status').textContent='';document.querySelector('#translate-cell').disabled=editTarget.key==='handle'||!editTarget.source_value;openEditor()}}));
document.querySelectorAll('.more').forEach(button=>button.addEventListener('click',()=>{{button.previousElementSibling.textContent=button.dataset.full;button.remove()}}));
let closeTimer,openFrame;
function openEditor() {{clearTimeout(closeTimer);editor.classList.remove('is-closing');editor.showModal();openFrame=requestAnimationFrame(()=>editor.classList.add('is-open'))}}
function closeEditor() {{cancelAnimationFrame(openFrame);editor.classList.remove('is-open');editor.classList.add('is-closing');const duration=matchMedia('(prefers-reduced-motion: reduce)').matches?0:parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--modal-close-dur'))||150;closeTimer=setTimeout(()=>{{editor.close();editor.classList.remove('is-closing')}},duration)}}
editor.addEventListener('cancel',event=>{{event.preventDefault();closeEditor()}});
document.querySelector('#cancel-cell').addEventListener('click',closeEditor);
document.querySelector('#translate-cell').addEventListener('click',async()=>{{if(!editTarget)return;const button=document.querySelector('#translate-cell'),status=document.querySelector('#edit-status');button.disabled=true;status.textContent='Traduciendo…';try{{const response=await fetch('/api/translate-cell',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{source_text:editTarget.source_value||'',source_store:'ES',source_locale:'es-ES',target_store:editTarget.store,target_locale:editTarget.locale,field_key:editTarget.key,metafield_type:editTarget.metafield_type,html:editTarget.key==='body_html'||editTarget.metafield_type==='html'}})}});const result=await response.json();if(!response.ok)throw new Error(result.detail||'Error de traducción');area.value=result.translation;status.textContent=result.memory_hit?'Texto recordado':(result.glossary_terms?`Traducción lista · ${{result.glossary_terms}} término(s) del glosario aplicado(s)`:'Traducción lista para revisar')}}catch(error){{status.textContent=error.message}}finally{{button.disabled=false}}}});
form.addEventListener('submit',async event=>{{event.preventDefault();if(!editTarget)return;const save=document.querySelector('#save-cell'),status=document.querySelector('#edit-status');save.disabled=true;status.textContent='Guardando…';const response=await fetch('/api/shopify/translations',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{...editTarget,value:area.value}})}});const result=await response.json();save.disabled=false;if(response.ok){{status.textContent='Guardado';location.reload()}}else status.textContent=result.detail||'No se pudo guardar'}});
</script>'''
        except HTTPException as exc:
            detail=f'<section class="card"><h2>No se pudo cargar la comparación</h2><p>{html.escape(str(exc.detail))}</p></section>'
    if term:
        try:
            data=search_shopify_products(term); hits=data['products']
            distinct=', '.join(sorted({code for p in hits for code in p.get('store_codes',[p['store']])}))
            summary=f'{len(hits)} coincidencia(s) en {html.escape(distinct)}' if hits else 'Sin resultados en tiendas activas'
            cards=[]
            for p in hits:
                esc=lambda x:html.escape(str(x if x is not None else ''),quote=True)
                codes=p.get('store_codes',[p['store']]); store_label=' / '.join(codes)
                locale_labels=', '.join(dict.fromkeys(locale for code in codes for locale in p.get('locales',{}).get(code,[])))
                candidates=[]
                for candidate in p['candidates']:
                    if 'ES' in codes:
                        args={'source_id':candidate['id'],'q':term}
                        action=f'<a class="button" href="/products?{urlencode(args)}">Comparar este producto</a>'
                    else: action='<span class="badge">Producto encontrado</span>'
                    candidates.append(f'<div class="candidate"><strong>{esc(candidate["title"])}</strong><p class="muted">ID: {esc(candidate["id"])} · Handle: {esc(candidate["handle"])}</p>{action}</div>')
                title=f'{len(candidates)} productos con este SKU' if len(candidates)>1 else candidates and p['candidates'][0]['title'] or p['sku']
                cards.append(f'<article class="card"><strong>{esc(title)}</strong><p class="muted">SKU: {esc(p["sku"] or "Sin SKU")} · Tienda/mercado: {esc(store_label)} · Locales: {esc(locale_labels or "—")}</p>{"".join(candidates)}</article>')
            errors=''.join(f'<p class="muted">{html.escape(str(n))}</p>' for n in data['notices'])
            results=f'<section class="card"><h2>Coincidencias por tienda</h2><p class="muted">{summary}</p>{errors}{"".join(cards) if cards else "<p>No se encontraron productos.</p>"}</section>'
        except HTTPException as exc:
            results=f'<section class="card"><h2>No se pudo completar la búsqueda</h2><p>{html.escape(str(exc.detail))}</p></section>'
    empty = '<section class="empty-state"><h2>Empieza con un producto</h2><p class="muted">Introduce un SKU, nombre o ID. Después selecciona un resultado de ES para comparar los idiomas y editar su contenido.</p></section>' if not term and not source_id else ''
    value=html.escape(term,quote=True)
    body=f'''<main class="wrap"><h1>Gestión de productos</h1><p class="lede">Busca por SKU, nombre, ID o texto en todas las tiendas activas. Abre un producto de España para revisar los campos y su estado por tienda e idioma.</p><section class="card search-panel"><form action="/products" method="get"><div class="field"><label for="term">SKU, nombre o ID de Shopify</label><input id="term" name="q" value="{value}" required placeholder="Ej. NG-1234, lámpara exterior o gid://shopify/Product/…"></div><div class="actions"><button type="submit">Buscar en tiendas</button></div></form></section><section class="batch-shortcut"><div><h2>¿Varios productos a la vez?</h2><p class="muted">Prepara una lista de SKU y revisa cada propuesta antes de guardar.</p></div><a class="button secondary" href="/products/batch">Traducción en lote <span aria-hidden="true">↗</span></a></section>{detail}{results}{empty}</main>'''
    return shell('Explorador de productos',body,'products')

@app.get('/products/batch',response_class=HTMLResponse)
def batch_products_page():
    body='''<main class="wrap"><h1>Campaña de traducción por SKU</h1><p class="lede">Elige varios SKU y, si quieres, solo los campos que te interesan para esta campaña. El servicio prepara las propuestas de un producto cada vez, en segundo plano (puedes cerrar esta página), y las deja en <a href="/translation-batches">Revisión de lotes</a>. Sigue el avance en <a href="/translation-campaigns">Campañas</a>. Por defecto las traducciones se escriben directamente en Shopify, donde se revisan después; si desmarcas esa opción, nada se escribe hasta que alguien revise, apruebe y aplique.</p><section class="card"><form id="batch-form"><div class="field"><label for="skus">SKU (uno por línea o separados por coma)</label><textarea id="skus" required maxlength="200000" placeholder="NG-1234&#10;NG-5678"></textarea></div><div class="field"><label for="sku-file">…o carga un archivo Excel (.xlsx), CSV o TXT con los SKU</label><input id="sku-file" type="file" accept=".xlsx,.xlsm,.csv,.txt"><p class="muted">Se usa la columna con cabecera “SKU”; si no la hay, la primera columna. Los SKU leídos se añaden al cuadro de arriba para que puedas revisarlos. Máximo 5.000 SKU.</p></div><div class="grid"><fieldset class="field"><legend>Tiendas destino</legend><p class="muted">Selecciona una o varias; sin selección se usan todas.</p><div id="target-stores">Cargando tiendas…</div></fieldset><fieldset class="field"><legend>Locales destino</legend><p class="muted">Selecciona locales disponibles en cualquiera de las tiendas; sin selección se usan todos.</p><div id="target-locales">Selecciona primero las tiendas.</div></fieldset></div><fieldset class="field"><legend>Crear si no existe</legend><p class="muted">En las tiendas marcadas, si el producto no se encuentra por SKU, se prepara su creación como producto <strong>archivado</strong> con los datos de ES traducidos (título, descripción, SEO, handle, variantes con SKU, código de barras y precio de ES sin conversión, imágenes y metacampos). Se crea directamente, sin pasar por aprobación: al quedar archivado, no se publica y se revisa en Shopify. Después se preparan las traducciones de sus demás locales.</p><div id="create-stores">Cargando tiendas…</div></fieldset><fieldset class="field"><legend>Campos a traducir</legend><label><input type="checkbox" class="field-choice" value="title" checked style="width:auto"> Título</label><br><label><input type="checkbox" class="field-choice" value="handle" checked style="width:auto"> Slug (handle)</label><br><label><input type="checkbox" class="field-choice" value="descriptionHtml" checked style="width:auto"> Descripción</label><br><label><input type="checkbox" class="field-choice" value="seo.title" checked style="width:auto"> SEO · título</label><br><label><input type="checkbox" class="field-choice" value="seo.description" checked style="width:auto"> SEO · descripción</label><br><label><input type="checkbox" class="field-choice" value="metafields" style="width:auto"> Todos los metacampos (producto y variantes)</label><div id="meta-picker" class="field"><p class="muted" id="meta-status">Cargando metacampos de ES…</p></div><details class="field"><summary>Añadir metacampos a mano</summary><label for="meta-fields">Metacampos adicionales (<code>namespace.key</code>, uno por línea)</label><textarea id="meta-fields" rows="3" maxlength="4000" placeholder="custom.ficha_tecnica"></textarea></details><label><input type="checkbox" id="variant-only" checked style="width:auto"> Metacampos de variante: solo las variantes de los SKU indicados (si no, se traducen las de todas las variantes del producto)</label></fieldset><fieldset class="field"><legend>Destino de las traducciones</legend><label><input type="checkbox" id="auto-apply" checked style="width:auto"> Traducir y subir directamente a Shopify (la revisión se hace en Shopify)</label></fieldset><p id="batch-status" class="muted" role="status" aria-live="polite"></p><div class="actions"><button id="prepare-batch" type="submit">Preparar lotes para revisión</button><button id="cancel-batch" type="button" class="secondary" hidden>Detener</button><a class="button secondary" href="/products">Volver al explorador</a></div><p class="muted">La preparación consulta Shopify y DeepL y guarda los lotes en el hub; con «subir directamente» también escribe las traducciones en Shopify. Las listas largas se procesan automáticamente de 50 en 50 hasta terminar; mantén esta pestaña abierta mientras tanto.</p></form></section><section id="batch-results" aria-live="polite"></section></main><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const storeBox=document.querySelector('#target-stores'),localeBox=document.querySelector('#target-locales'),status=document.querySelector('#batch-status'),results=document.querySelector('#batch-results');let availableStores=[];
async function fetchJSON(url,options){const r=await fetch(url,options);let data={};try{data=await r.json()}catch{}if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'Error al procesar la solicitud');return data}
async function loadStores(){try{availableStores=(await fetchJSON('/api/stores')).filter(s=>s.active&&s.has_token);if(!availableStores.length){storeBox.textContent='Sin tiendas activas';status.textContent='Activa una tienda y configura su token en Tiendas para habilitarla.';return}storeBox.innerHTML=availableStores.map(s=>`<label style="display:block"><input type="checkbox" class="store-choice" value="${esc(s.code)}" style="width:auto"> ${esc(s.code)} · ${esc(s.domain||'sin dominio')}</label>`).join('');document.querySelector('#create-stores').innerHTML=availableStores.filter(s=>s.code!=='ES').map(s=>`<label style="display:block"><input type="checkbox" class="create-choice" value="${esc(s.code)}" style="width:auto"> ${esc(s.code)}</label>`).join('')||'<span class="muted">Sin tiendas distintas de ES.</span>';document.querySelectorAll('.store-choice').forEach(x=>x.addEventListener('change',loadLocales));await loadLocales()}catch(e){storeBox.textContent='No disponible';status.textContent=e.message}}
async function loadLocales(){const chosen=[...document.querySelectorAll('.store-choice:checked')].map(x=>x.value),selected=chosen.length?chosen:availableStores.map(s=>s.code);if(!selected.length){localeBox.textContent='No hay tiendas disponibles.';return}localeBox.textContent='Consultando locales…';const all=new Map();for(const code of selected){try{const data=await fetchJSON('/api/stores/'+encodeURIComponent(code)+'/shop-locales');for(const x of data.filter(x=>x.published&&x.deepl_target&&!(code==='ES'&&x.primary))){const entries=all.get(x.locale)||[];entries.push(code);all.set(x.locale,entries)}}catch(e){status.textContent=e.message}}localeBox.innerHTML=all.size?[...all].sort(([a],[b])=>a.localeCompare(b)).map(([locale,codes])=>`<label style="display:block"><input type="checkbox" class="locale-choice" value="${esc(locale)}" style="width:auto"> ${esc(locale)} <span class="muted">· ${codes.join(', ')}</span></label>`).join(''):'<span class="muted">No hay locales disponibles para las tiendas seleccionadas.</span>'}
loadStores();
const PINNED_META=['custom.descripcion_corta','newgarden.descripcion_corta_variante','newgarden.faq','newgarden.faq_variante','newgarden.meta_descripcion_variante','newgarden.meta_titulo_variante'];
async function loadMetaDefs(refresh){const box=document.querySelector('#meta-picker');box.innerHTML='<p class="muted" id="meta-status">Cargando metacampos de ES…</p>';try{const defs=(await fetchJSON('/api/shopify/metafield-definitions'+(refresh?'?refresh=true':''))).sort((a,b)=>{const x=PINNED_META.indexOf(a.value),y=PINNED_META.indexOf(b.value);return (x<0?99:x)-(y<0?99:y)});box.innerHTML=defs.length?`<label for="meta-search">Buscar metacampo</label><input id="meta-search" type="search" placeholder="Nombre o namespace.key" autocomplete="off"><p class="muted" id="meta-count" role="status" aria-live="polite"></p><p class="muted">Los metacampos habituales (descripción corta, FAQ, meta título y meta descripción SEO, de producto y variante) van siempre arriba y no se ocultan al buscar.</p><div id="meta-list" style="max-height:280px;overflow:auto">${defs.map(d=>`<div class="meta-item${PINNED_META.includes(d.value)?' meta-pinned':''}" data-search="${esc((d.name+' '+d.value).toLowerCase())}"><label><input type="checkbox" class="meta-choice" value="${esc(d.value)}" style="width:auto"> ${esc(d.name)} <code>${esc(d.value)}</code> <span class="muted">· ${d.owners.map(o=>o==='product'?'producto':'variante').join(' + ')}</span></label></div>`).join('')}</div><button type="button" class="secondary" id="reload-meta">Recargar lista</button>`:'<p class="muted">ES no tiene definiciones de metacampos de texto.</p><button type="button" class="secondary" id="reload-meta">Recargar lista</button>';box.querySelector('#reload-meta').addEventListener('click',()=>loadMetaDefs(true));if(defs.length){const search=box.querySelector('#meta-search'),count=box.querySelector('#meta-count'),update=()=>{const q=search.value.trim().toLowerCase();let shown=0;for(const item of box.querySelectorAll('.meta-item')){const hide=!!q&&!item.classList.contains('meta-pinned')&&!item.dataset.search.includes(q);item.hidden=hide;if(!hide)shown++}count.textContent=shown+' de '+defs.length+' metacampos · '+box.querySelectorAll('.meta-choice:checked').length+' seleccionado(s)'};search.addEventListener('input',update);box.addEventListener('change',update);update()}}catch(e){box.innerHTML='<p class="muted">No se pudo cargar la lista ('+esc(e.message)+'). Puedes añadirlos a mano abajo.</p>'}}
loadMetaDefs(false);
function selectedFields(){const fields=[...document.querySelectorAll('.field-choice:checked')].map(x=>x.value);if(!fields.includes('metafields'))for(const c of document.querySelectorAll('.meta-choice:checked'))fields.push('metafield:'+c.value);if(!fields.includes('metafields'))for(const line of document.querySelector('#meta-fields').value.split(String.fromCharCode(10))){const v=line.trim();if(v)fields.push('metafield:'+v)}return fields}
function addResult(sku,html,ok){const card=document.createElement('section');card.className='card';card.innerHTML=`<h2>SKU ${esc(sku)}</h2>${html}`;results.append(card)}
const skuBox=document.querySelector('#skus'),cancelButton=document.querySelector('#cancel-batch');let cancelled=false;
cancelButton.addEventListener('click',()=>{cancelled=true;cancelButton.disabled=true;status.textContent='Deteniendo tras el producto en curso…'});
document.querySelector('#sku-file').addEventListener('change',async e=>{const file=e.target.files[0];if(!file)return;status.textContent='Leyendo '+file.name+'…';try{const data=await fetchJSON('/api/translation-batches/parse-skus?filename='+encodeURIComponent(file.name),{method:'POST',body:file});const current=skuBox.value.split(/[\\n,]/).map(x=>x.trim()).filter(Boolean);skuBox.value=[...new Set([...current,...data.skus])].join(String.fromCharCode(10));status.textContent=data.count+' SKU leídos de '+file.name+'. Total en la lista: '+skuBox.value.split(String.fromCharCode(10)).length+'.'}catch(err){status.textContent=err.message}e.target.value=''});
document.querySelector('#batch-form').addEventListener('submit',async e=>{e.preventDefault();const skus=[...new Set(skuBox.value.replaceAll(',',String.fromCharCode(10)).split(String.fromCharCode(10)).map(x=>x.trim()).filter(Boolean))];if(!skus.length)return;if(skus.length>5000){status.textContent='El máximo es 5.000 SKU por ejecución.';return}const fields=selectedFields();if(!fields.length){status.textContent='Selecciona al menos un campo a traducir.';return}const body={skus,fields,variants_of_skus_only:document.querySelector('#variant-only').checked,auto_apply:document.querySelector('#auto-apply').checked};const stores=[...document.querySelectorAll('.store-choice:checked')].map(x=>x.value),locales=[...document.querySelectorAll('.locale-choice:checked')].map(x=>x.value);const creates=[...document.querySelectorAll('.create-choice:checked')].map(x=>x.value);if(creates.length){body.create_stores=creates;if(stores.length)for(const c of creates)if(!stores.includes(c))stores.push(c)}if(stores.length)body.stores=stores;if(locales.length)body.locales=locales;if(body.auto_apply&&!confirm('Las traducciones se escribirán directamente en Shopify sin revisión previa en el hub. ¿Continuar?'))return;const run=document.querySelector('#prepare-batch');run.disabled=true;status.textContent='Creando la campaña…';try{const campaign=await fetchJSON('/api/translation-campaigns',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});location.href='/translation-campaigns#'+campaign.id}catch(err){status.textContent=err.message;run.disabled=false}});
</script>'''
    return shell('Traducción en lote',body,'batch')

@app.get('/translation-batches',response_class=HTMLResponse)
def translation_batches_page():
    body='''<main class="wrap"><h1>Revisión de lotes</h1><p class="lede">Revisa cada propuesta antes de aprobarla. Solo los campos aprobados se podrán aplicar a Shopify.</p><section class="card" aria-labelledby="batch-search-title"><h2 id="batch-search-title">Buscar histórico por SKU</h2><form id="batch-search" class="actions" role="search"><label for="batch-search-sku" class="sr-only">SKU</label><input id="batch-search-sku" type="search" maxlength="120" required placeholder="LUMLNA040BEWLNW" autocomplete="off" style="max-width:24rem"><button type="submit">Buscar</button></form><p id="batch-search-status" class="muted" role="status" aria-live="polite"></p><div id="batch-search-results"></div></section><details class="card" id="recent-batches"><summary><strong>Lotes recientes</strong> <span class="muted">(últimos 200; pulsa para ver la tabla)</span></summary><div class="section-heading"><span></span><button id="refresh-batches" class="secondary" type="button">Actualizar lotes</button></div><p id="batch-list-status" class="muted" role="status" aria-live="polite"></p><div id="batch-list"></div></details><section id="batch-detail" aria-label="Detalle del lote"></section></main><script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const list=document.querySelector('#batch-list'),listStatus=document.querySelector('#batch-list-status'),detail=document.querySelector('#batch-detail');let current=null;
async function request(url,options){const r=await fetch(url,options);let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.detail||'No se pudo completar la solicitud');return d}
const date=s=>s?new Date(s).toLocaleString('es-ES'):'—';
async function loadBatches(){listStatus.textContent='Cargando lotes…';try{const d=await request('/api/translation-batches');listStatus.textContent=d.length?d.length+' lote(s) recientes.':'No hay lotes persistidos.';list.innerHTML=d.length?'<div class="batch-table-scroll"><table><caption class="sr-only">Lotes de traducción recientes</caption><thead><tr><th scope="col">Lote</th><th scope="col">SKU</th><th scope="col">Producto</th><th scope="col">Estado</th><th scope="col">Actualizado</th><th scope="col">Acción</th></tr></thead><tbody>'+d.map(b=>`<tr><th scope="row">#${b.id}</th><td>${esc(b.sku)}</td><td>${esc(b.source_title||'—')}</td><td>${esc(b.state)}</td><td>${esc(date(b.updated_at))}</td><td><button class="secondary open-batch" data-id="${b.id}" type="button" aria-label="Abrir revisión del lote ${b.id}, SKU ${esc(b.sku)}">Abrir revisión</button></td></tr>`).join('')+'</tbody></table></div>':'';list.querySelectorAll('.open-batch').forEach(b=>b.addEventListener('click',()=>loadDetail(Number(b.dataset.id))))}catch(e){listStatus.textContent=e.message}}
function itemStatus(i){return i.state==='PENDING_REVIEW'?'Pendiente de revisión':i.state==='APPROVED'?(i.quality?.status==='PASS'?'Aprobado automáticamente · pendiente de aplicar':'Aprobado · pendiente de aplicar'):i.state==='SAVED'?'Aplicado en Shopify':i.state==='REJECTED'?'Rechazado':i.state==='APPLY_ERROR'?'Error al aplicar':i.state==='ERROR'?'Error de preparación':i.state}
function renderBatch(b){current=b;const pending=b.items.filter(i=>i.state==='PENDING_REVIEW'),approved=b.items.filter(i=>i.state==='APPROVED'),errors=b.items.filter(i=>['ERROR','APPLY_ERROR'].includes(i.state)),stores=[...new Set(b.items.map(i=>i.store))].sort(),locales=[...new Set(b.items.map(i=>i.locale))].sort();detail.innerHTML=`<section class="card batch-detail-card" aria-labelledby="batch-detail-title"><span class="badge">${esc(b.state)}</span><h2 id="batch-detail-title" tabindex="-1">Lote #${b.id} · ${esc(b.sku)}</h2><p>${esc(b.source_title||'')} · Creado ${esc(date(b.created_at))}</p>${b.error?`<p class="batch-error" role="alert">${esc(b.error)}</p>`:''}<div class="batch-summary" aria-label="Resumen del lote"><div><strong>${b.items.length}</strong><span>Campos</span></div><div><strong>${pending.length}</strong><span>Pendientes</span></div><div><strong>${approved.length}</strong><span>Aprobados</span></div><div><strong>${errors.length}</strong><span>Con errores</span></div></div><div class="batch-actions"><button id="approve-selected" type="button" ${pending.length?'':'disabled'}>Aprobar seleccionados (<span id="selected-count">0</span>)</button><button id="apply-approved" type="button" class="secondary" ${approved.length?'':'disabled'}>Aplicar aprobados a Shopify (${approved.length})</button><button id="reject-batch" type="button" class="secondary" ${pending.length||approved.length||b.items.some(i=>i.state==='APPLY_ERROR')?'':'disabled'}>Rechazar lote</button></div><p class="muted">Puedes volver después por los campos pendientes que dejaste sin seleccionar: apruébalos y aplica el nuevo grupo. Aplicar aprobados escribe esos campos en Shopify.</p><p id="batch-action-status" class="status" role="status" aria-live="polite"></p><div class="batch-filters" aria-label="Filtros de campos"><div class="field"><label for="batch-filter-status">Estado</label><select id="batch-filter-status"><option value="">Todos los estados</option><option value="PENDING_REVIEW">Pendientes de revisión</option><option value="APPROVED">Aprobados</option><option value="ERROR">Errores</option><option value="SAVED">Aplicados</option><option value="REJECTED">Rechazados</option></select></div><div class="field"><label for="batch-filter-store">Tienda</label><select id="batch-filter-store"><option value="">Todas las tiendas</option>${stores.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('')}</select></div><div class="field"><label for="batch-filter-locale">Idioma</label><select id="batch-filter-locale"><option value="">Todos los idiomas</option>${locales.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('')}</select></div><div class="field"><label for="batch-filter-text">Buscar campo</label><input id="batch-filter-text" type="search" placeholder="Descripción, variante, metacampo…"></div></div><label class="batch-select-visible"><input id="select-visible" type="checkbox" ${pending.length?'':'disabled'}> Seleccionar todos los pendientes visibles</label><div id="batch-items" class="batch-items">${b.items.length?b.items.map(i=>{const pendingItem=i.state==='PENDING_REVIEW',approvedItem=i.state==='APPROVED',proposalId=`batch-proposal-${i.id}`;return `<article class="batch-item ${i.state==='ERROR'||i.state==='APPLY_ERROR'?'has-error':''}" data-state="${esc(i.state)}" data-store="${esc(i.store)}" data-locale="${esc(i.locale)}" data-search="${esc((i.field_label||i.field_key).toLowerCase())}" aria-labelledby="batch-item-title-${i.id}"><div class="batch-item-head"><div><h3 id="batch-item-title-${i.id}">${esc(i.field_label||i.field_key)}</h3><p class="muted">${esc(i.store)} · ${esc(i.locale)}</p></div><span class="badge">${esc(itemStatus(i))}</span></div>${i.quality?`<p class="quality-note">Integridad: ${esc(i.quality.status)} · ${esc(i.quality.score)}/100${i.quality.hard_failures?.length?` · ${esc(i.quality.hard_failures.join('; '))}`:''}</p>`:''}${i.error?`<p class="batch-error" role="note"><strong>Requiere atención:</strong> ${esc(i.error)}</p>`:''}${pendingItem||approvedItem?`<div class="batch-translation-pair"><section><h4>Texto de origen</h4><pre>${esc(i.source_text||'—')}</pre></section>${pendingItem?`<div class="field"><label for="${proposalId}">Propuesta de traducción</label><textarea id="${proposalId}" class="batch-proposal" data-item="${i.id}" rows="${i.field_key==='descriptionHtml'?8:4}">${esc(i.proposed_text||'')}</textarea></div>`:`<div class="field"><label for="${proposalId}">Propuesta aprobada (editable: al guardar vuelve a revisión)</label><textarea id="${proposalId}" class="batch-proposal" data-item="${i.id}" rows="${i.field_key==='descriptionHtml'?8:4}">${esc(i.proposed_text||'')}</textarea></div>`}</div>${pendingItem?`<div class="batch-item-actions"><button class="secondary save-proposal" data-item="${i.id}" type="button">Guardar cambios</button><label class="batch-approve-label"><input class="approve-check" type="checkbox" value="${i.id}"> Confirmo que revisé esta traducción</label><span class="item-action-status" id="batch-item-status-${i.id}" role="status" aria-live="polite"></span></div>`:`<div class="batch-item-actions"><button class="secondary save-proposal" data-item="${i.id}" type="button">Guardar cambios</button><button class="secondary reopen-approved" data-item="${i.id}" type="button">Devolver a revisión</button><span class="item-action-status" id="batch-item-status-${i.id}" role="status" aria-live="polite"></span></div>`}`:`${i.source_text!=null||i.proposed_text!=null?`<details class="batch-source"><summary>Ver texto y propuesta</summary>${i.source_text!=null?`<h4>Texto de origen</h4><pre>${esc(i.source_text)}</pre>`:''}${i.proposed_text!=null?`<h4>Propuesta</h4><pre>${esc(i.proposed_text)}</pre>`:''}</details>`:''}`}</article>`}).join(''):'<p class="muted">El lote no contiene campos.</p>'}</div></section>`;
const actionStatus=detail.querySelector('#batch-action-status'),itemStatusNode=id=>detail.querySelector(`#batch-item-status-${id}`),items=detail.querySelector('#batch-items');
function applyFilters(){const status=detail.querySelector('#batch-filter-status').value,store=detail.querySelector('#batch-filter-store').value,locale=detail.querySelector('#batch-filter-locale').value,text=detail.querySelector('#batch-filter-text').value.trim().toLowerCase();for(const card of items.querySelectorAll('.batch-item')){card.hidden=!!((status&&(status==='ERROR'? !['ERROR','APPLY_ERROR'].includes(card.dataset.state):card.dataset.state!==status))||(store&&card.dataset.store!==store)||(locale&&card.dataset.locale!==locale)||(text&&!card.dataset.search.includes(text)));if(card.hidden){const checkbox=card.querySelector('.approve-check');if(checkbox)checkbox.checked=false}}const visiblePending=[...items.querySelectorAll('.batch-item:not([hidden]) .approve-check')];detail.querySelector('#select-visible').disabled=!visiblePending.length;const selected=detail.querySelectorAll('.approve-check:checked').length;detail.querySelector('#selected-count').textContent=selected;detail.querySelector('#approve-selected').disabled=!selected;detail.querySelector('#select-visible').checked=visiblePending.length>0&&visiblePending.every(x=>x.checked);const applyButton=detail.querySelector('#apply-approved');applyButton.disabled=!selected&&!approved.length;applyButton.textContent=selected?`Aplicar seleccionados (${selected})`:`Aplicar aprobados a Shopify (${approved.length})`;}
for(const selector of ['#batch-filter-status','#batch-filter-store','#batch-filter-locale','#batch-filter-text'])detail.querySelector(selector).addEventListener('input',applyFilters);detail.querySelector('#select-visible').addEventListener('change',event=>{for(const checkbox of items.querySelectorAll('.batch-item:not([hidden]) .approve-check'))checkbox.checked=event.target.checked;applyFilters()});items.addEventListener('change',event=>{if(event.target.matches('.approve-check'))applyFilters()});applyFilters();
detail.querySelectorAll('.save-proposal').forEach(btn=>btn.addEventListener('click',async()=>{const id=Number(btn.dataset.item),text=detail.querySelector(`.batch-proposal[data-item="${id}"]`).value,status=itemStatusNode(id);btn.disabled=true;status.textContent='Guardando…';try{await request(`/api/translation-batches/${b.id}/items/${id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({proposed_text:text})});await loadDetail(b.id);await loadBatches();detail.querySelector(`#batch-proposal-${id}`)?.focus({preventScroll:true});detail.querySelector('#batch-action-status').textContent='Cambios guardados y calidad reevaluada. El campo queda pendiente de revisión: apruébalo de nuevo.'}catch(e){status.textContent='No se pudo guardar: '+e.message;btn.disabled=false}}));
detail.querySelectorAll('.reopen-approved').forEach(btn=>btn.addEventListener('click',async()=>{const id=Number(btn.dataset.item),status=itemStatusNode(id);btn.disabled=true;status.textContent='Devolviendo a revisión…';try{await request(`/api/translation-batches/${b.id}/reopen`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_ids:[id]})});await loadDetail(b.id);await loadBatches();detail.querySelector(`#batch-proposal-${id}`)?.focus({preventScroll:true});detail.querySelector('#batch-action-status').textContent='Campo pendiente de revisión. Puedes editarlo o volver a aprobarlo.'}catch(e){status.textContent='No se pudo devolver a revisión: '+e.message;btn.disabled=false}}));
detail.querySelector('#approve-selected')?.addEventListener('click',async()=>{const ids=[...detail.querySelectorAll('.approve-check:checked')].map(x=>Number(x.value));if(!ids.length){actionStatus.textContent='Selecciona al menos una propuesta revisada.';return}const button=detail.querySelector('#approve-selected');button.disabled=true;actionStatus.textContent='Guardando aprobaciones…';try{await request(`/api/translation-batches/${b.id}/approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_ids:ids})});await loadDetail(b.id);await loadBatches();detail.querySelector('#batch-action-status').textContent=`${ids.length} campo(s) aprobado(s).`}catch(e){actionStatus.textContent='No se pudieron guardar las aprobaciones: '+e.message;button.disabled=false}});
detail.querySelector('#reject-batch')?.addEventListener('click',async()=>{if(!confirm('¿Rechazar el lote #'+b.id+'? Se descartarán todas las propuestas no aplicadas; lo ya subido a Shopify no se toca.'))return;const button=detail.querySelector('#reject-batch');button.disabled=true;actionStatus.textContent='Rechazando lote…';try{await request(`/api/translation-batches/${b.id}/reject`,{method:'POST'});await loadDetail(b.id);await loadBatches();detail.querySelector('#batch-action-status').textContent='Lote rechazado. Las propuestas no aplicadas se han descartado.'}catch(e){actionStatus.textContent='No se pudo rechazar el lote: '+e.message;button.disabled=false}});
detail.querySelector('#apply-approved')?.addEventListener('click',async()=>{const ids=[...detail.querySelectorAll('.approve-check:checked')].map(x=>Number(x.value)),selected=ids.length>0;if(!selected&&!approved.length)return;const prompt=selected?`¿Aprobar y aplicar los ${ids.length} campos seleccionados a Shopify?`:`¿Aplicar ${approved.length} campo(s) aprobado(s) a Shopify?`;if(!confirm(prompt+' Esta acción escribe en la tienda.'))return;const button=detail.querySelector('#apply-approved');button.disabled=true;actionStatus.textContent=selected?'Aprobando y aplicando solo los campos seleccionados…':'Aplicando los campos aprobados en Shopify…';try{if(selected)await request(`/api/translation-batches/${b.id}/approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_ids:ids})});await request(`/api/translation-batches/${b.id}/apply`,selected?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_ids:ids})}:{method:'POST'});await loadDetail(b.id);await loadBatches();detail.querySelector('#batch-action-status').textContent=selected?`${ids.length} campo(s) aprobado(s) y aplicado(s).`:'Aplicación finalizada. Los campos pendientes siguen disponibles para otra selección.'}catch(e){actionStatus.textContent='No se pudieron aplicar los campos: '+e.message;button.disabled=false}})}
async function loadDetail(id){detail.innerHTML='<section class="card" role="status">Cargando lote…</section>';try{renderBatch(await request('/api/translation-batches/'+id));detail.querySelector('#batch-detail-title')?.focus({preventScroll:true})}catch(e){detail.innerHTML=`<section class="card"><p class="status" role="alert">${esc(e.message)}</p></section>`}}
const STATE_ES={PENDING_REVIEW:'pendientes',APPROVED:'aprobados',SAVED:'subidos',REJECTED:'rechazados',ERROR:'con error',APPLY_ERROR:'error al aplicar'};
async function searchSku(ev){ev.preventDefault();const sku=document.querySelector('#batch-search-sku').value.trim(),st=document.querySelector('#batch-search-status'),out=document.querySelector('#batch-search-results');if(!sku)return;st.textContent='Buscando…';out.innerHTML='';try{const d=await request('/api/translation-batches/search?sku='+encodeURIComponent(sku));st.textContent=d.batches.length?`${d.batches.length} lote(s) para «${sku}»${d.product_gid?' (incluye otros SKU del mismo producto)':''}, del más reciente al más antiguo.`:`No hay lotes para «${sku}». Las traducciones hechas fuera del hub (p. ej. en el admin de Shopify) no aparecen aquí.`;out.innerHTML=d.batches.map(b=>`<article class="card"><h3>Lote #${b.id} · ${esc(b.sku)} <span class="badge">${esc(b.state)}</span></h3><p>${esc(b.source_title||'')} · Creado ${esc(date(b.created_at))} · Actualizado ${esc(date(b.updated_at))}${b.match==='producto'?' · <em>mismo producto, otro SKU</em>':''}</p>${b.error?`<p class="batch-error">${esc(b.error)}</p>`:''}<ul>${b.targets.map(t=>`<li><strong>${esc(t.store)} · ${esc(t.locale)}</strong>: ${Object.entries(t.states).map(([s,n])=>n+' '+(STATE_ES[s]||s)).join(', ')}</li>`).join('')||'<li class="muted">Sin campos.</li>'}</ul><button class="secondary open-found" type="button" data-id="${b.id}">Abrir revisión</button></article>`).join('');out.querySelectorAll('.open-found').forEach(x=>x.addEventListener('click',()=>{loadDetail(Number(x.dataset.id));detail.scrollIntoView({behavior:'smooth'})}))}catch(e){st.textContent=e.message}}
document.querySelector('#batch-search').addEventListener('submit',searchSku);
document.querySelector('#refresh-batches').addEventListener('click',loadBatches);document.querySelector('#recent-batches').addEventListener('toggle',e=>{if(e.target.open&&!list.innerHTML)loadBatches()});{const m=location.hash.match(/^#([0-9]+)$/);if(m)loadDetail(Number(m[1]))}
</script>'''
    return shell('Revisión de lotes',body,'review')

TEXT_METAFIELD_TYPES={'single_line_text_field','multi_line_text_field','html','rich_text_field','list.single_line_text_field','list.multi_line_text_field'}
_metafield_defs_cache={'at':0.0,'data':None}

@app.get('/api/shopify/metafield-definitions')
def list_metafield_definitions(refresh:bool=False):
    """Text metafield definitions of the ES store (product and variant), for campaign field pickers."""
    if not refresh and _metafield_defs_cache['data'] is not None and time.time()-_metafield_defs_cache['at']<600: return _metafield_defs_cache['data']
    store=next((s for s in shopify_stores() if s['code']=='ES' and s['token']),None)
    if not store: raise HTTPException(503,'La tienda ES no tiene dominio y token configurados')
    query='query Defs($owner:MetafieldOwnerType!,$after:String) { metafieldDefinitions(ownerType:$owner,first:100,after:$after) { nodes { namespace key name type { name } } pageInfo { hasNextPage endCursor } } }'
    found={}
    for owner,label in (('PRODUCT','product'),('PRODUCTVARIANT','variant')):
        after=None
        for _ in range(10):
            page=shopify_graphql(store['domain'],store['token'],query,{'owner':owner,'after':after}).get('metafieldDefinitions') or {}
            for d in page.get('nodes',[]):
                if d['type']['name'] not in TEXT_METAFIELD_TYPES or 'yotpo' in (d['namespace']+d['key']).casefold(): continue
                item=found.setdefault(f"{d['namespace']}.{d['key']}",{'value':f"{d['namespace']}.{d['key']}",'name':d['name'],'type':d['type']['name'],'owners':[]})
                item['owners'].append(label)
            if not page.get('pageInfo',{}).get('hasNextPage'): break
            after=page['pageInfo']['endCursor']
    data=sorted(found.values(),key=lambda x:x['value'])
    _metafield_defs_cache.update(at=time.time(),data=data)
    return data

@app.get('/api/shopify/products/search')
def search_shopify_products(q:str):
    term=q.strip()
    if len(term)<2: raise HTTPException(400,'Escribe al menos 2 caracteres')
    stores=shopify_stores()
    if not any(s['code']=='ES' and s['token'] for s in stores): raise HTTPException(503,'La tienda ES no tiene dominio y token configurados')
    # Several configured market codes can point to the same Shopify shop. Query each
    # unique domain/token once, then annotate the matches with every configured code.
    groups={}
    for store in stores:
        if store['domain'] and store['token']:
            groups.setdefault((store['domain'],store['token']),[]).append(store)
    products=[]; notices=[]
    def search_group(group):
        representative=group[0]
        try:
            direct=shopify_find_by_id(representative,term) if term.isdecimal() or term.startswith('gid://shopify/Product/') else None
            hits=shopify_search(representative,term)
            if direct and all(p['id']!=direct['id'] for p in hits): hits.insert(0,direct)
            return group,hits,None
        except HTTPException as exc:
            return group,[],exc.detail
    with ThreadPoolExecutor(max_workers=min(8,len(groups) or 1)) as executor:
        futures=[executor.submit(search_group,group) for group in groups.values()]
        for future in as_completed(futures):
            group,hits,error=future.result()
            if error: notices.append(f'{", ".join(s["code"] for s in group)}: {error}')
            store_codes=[s['code'] for s in group]
            primary='ES' if 'ES' in store_codes else store_codes[0]
            locales={s['code']:s['locales'] for s in group}
            for p in hits:
                candidate={'id':p['id'],'title':p['title'],'handle':p['handle'],'sku':next((v.get('sku') for v in p.get('variants',{}).get('nodes',[] ) if v.get('sku')),'')}
                products.append({'store':primary,'store_codes':store_codes,'locales':locales,'candidate':candidate})
    # A duplicated SKU can match multiple different Shopify products. Group them
    # under the same store/market card and let the operator select the Spanish row.
    grouped={}
    for hit in products:
        candidate=hit.pop('candidate'); sku=candidate['sku'].strip().casefold()
        key=(tuple(hit['store_codes']),sku if sku else candidate['id'])
        if key not in grouped:
            grouped[key]={**hit,'sku':candidate['sku'],'candidates':[]}
        grouped[key]['candidates'].append(candidate)
    result=list(grouped.values())
    result.sort(key=lambda p:(p['store']!='ES',p['store'],p['sku'],p['candidates'][0]['title']))
    return {'products':result,'notices':notices}

def prefer_active_products(matches):
    """Si un SKU está en varios productos y solo uno está activo, se usa ese; los borradores o archivados
    con el mismo SKU no cuentan. Si hay 0 o 2+ activos se devuelven todos y el llamador decide (409)."""
    active={k:p for k,p in matches.items() if p.get('status')=='ACTIVE'}
    return active if len(matches)>1 and len(active)==1 else matches

@app.get('/api/shopify/products/by-sku')
def get_shopify_product_by_sku(store:str,sku:str):
    store_code=store.strip().upper(); exact_sku=sku.strip()
    selected=shopify_store_by_code(store_code)
    if not selected or not selected['token']: raise HTTPException(404,'Tienda inactiva o sin credenciales Shopify: '+store_code)
    if not exact_sku: raise HTTPException(400,'Indica un SKU')
    hits=shopify_search(selected,exact_sku,50)
    matches={p['id']:p for p in hits if any((v.get('sku') or '').strip().casefold()==exact_sku.casefold() for v in (p.get('variants') or {}).get('nodes',[]))}
    if not matches: raise HTTPException(404,f'No se encontró el SKU exacto {exact_sku} en {store_code}')
    matches=prefer_active_products(matches)
    if len(matches)>1: raise HTTPException(409,f'El SKU {exact_sku} coincide con varios productos en {store_code}')
    product=shopify_product(selected,next(iter(matches)),selected['locales'])
    return {'store':store_code,'sku':exact_sku,'product':product}

@app.post('/api/shopify/graphql')
def execute_shopify_admin_graphql(x:ShopifyGraphQLRequest):
    store=shopify_store_by_code(x.store.strip().upper())
    if not store: raise HTTPException(404,'Tienda inactiva o no configurada: '+x.store)
    if not store['domain'] or not store['token']: raise HTTPException(503,'La tienda no tiene credenciales Shopify disponibles: '+x.store)
    payload=shopify_graphql_payload(store['domain'],store['token'],x.query,x.variables,x.operation_name)
    # Preserve GraphQL-level errors and partial data. Shopify returns many field and
    # mutation errors in HTTP 200 responses, so callers need the whole envelope.
    return {'store':store['code'],'api_version':SHOPIFY_API_VERSION,'data':payload.get('data'),
            'errors':payload.get('errors',[]),'extensions':payload.get('extensions',{})}

@app.get('/api/shopify/products/compare')
def compare_shopify_product(source_id:str,target_store:str|None=None,target_locale:str|None=None):
    return _compare_shopify_product(source_id,target_store,target_locale)

def _compare_shopify_product(source_id:str,target_store:str|None=None,target_locale:str|None=None,target_locales:dict[str,list[str]]|None=None):
    if (target_store is None)!=(target_locale is None): raise HTTPException(400,'Indica tienda y locale destino juntos')
    stores=shopify_stores(); by_code={s['code']:s for s in stores}; source_store=by_code.get('ES')
    if not source_store or not source_store['token']: raise HTTPException(503,'La tienda ES no tiene dominio y token configurados')
    if target_store is not None:
        selected=by_code.get(target_store)
        if not selected: raise HTTPException(404,'Tienda destino inactiva o sin credenciales Shopify')
        published={item['locale'] for item in shopify_shop_locales(selected) if item.get('published')}
        if target_locale not in published: raise HTTPException(400,'El locale destino no está publicado en Shopify')
    source_locales=list(source_store['locales'])
    if target_store=='ES' and target_locale and target_locale not in source_locales:
        source_locales.append(target_locale)
    source_locales.extend(locale for locale in (target_locales or {}).get('ES',[]) if locale not in source_locales)
    source_lookup_locales=list(dict.fromkeys(shopify_locale_code(locale) for locale in source_locales))
    source=shopify_product(source_store,source_id,source_lookup_locales)
    if not source: raise HTTPException(404,'Producto no encontrado en la tienda ES')
    sku=next((v.get('sku') for v in source.get('variants',{}).get('nodes',[]) if v.get('sku')),'')
    targets=[]; notices=[]
    if source.get('translation_error'): notices.append('Shopify no devolvió las traducciones de los locales configurados en ES; esos valores aparecerán vacíos.')
    primary_locales={}
    for store in stores:
        if not store['token']: continue
        try:
            primary=next((item['locale'] for item in shopify_shop_locales(store) if item.get('primary')),None)
            if primary: primary_locales[store['code']]=primary
        except HTTPException as exc:
            notices.append(f'{store["code"]}: no se pudo identificar el locale principal ({exc.detail}); sus celdas no serán editables hasta configurar read_locales.')
    with pool.connection() as c:
        mapping_rows=c.execute("SELECT dst.store_code,dst.shopify_product_gid FROM store_products src JOIN store_products dst USING(product_key) WHERE src.store_code='ES' AND src.shopify_product_gid=%s AND dst.store_code<>'ES' AND dst.mapping_status='CONFIRMED'",(source_id,)).fetchall()
    # Prefer mappings tied to this Spanish product. If none exists, try its SKU in each store.
    known={code:gid for code,gid in mapping_rows}
    for locale in source_locales:
        if locale!=primary_locales.get('ES') and locale.lower() not in ('es-es','es'):
            resolved=shopify_locale_code(locale)
            label=f'ES · {locale}'+(f' (Shopify {resolved})' if resolved!=locale else '')
            mode='translation' if primary_locales.get('ES') and locale!=primary_locales['ES'] else 'readonly'
            targets.append({'store':'ES','domain':source_store['domain'],'locale':locale,'lookup_locale':resolved,'label':label,'product':source,'mode':mode})
    for store in stores:
        if store['code']=='ES': continue
        locales=list(store['locales'])
        # MX needs its Spanish base column alongside configured secondary
        # locales. Other stores keep only their configured locales in the
        # comparison, even when their primary locale is Spanish.
        primary=primary_locales.get(store['code'])
        if store['code']=='MX' and primary and primary not in locales:
            locales.insert(0,primary)
        locales.extend(locale for locale in (target_locales or {}).get(store['code'],[]) if locale not in locales)
        if store['code']==target_store and target_locale and target_locale not in locales:
            locales.append(target_locale)
        if not locales: continue
        def lookup_locale(locale):
            if store['domain']!=source_store['domain']: return locale
            return shopify_locale_code(locale,store['code'])
        locale_map={locale:lookup_locale(locale) for locale in locales}
        query_locales=list(dict.fromkeys(locale_map.values()))
        gid=source_id if store['domain']==source_store['domain'] else known.get(store['code'])
        candidate=None
        if gid and store['token']:
            try: candidate=shopify_product(store,gid,query_locales)
            except HTTPException as exc: notices.append(f'{store["code"]}: no se pudo leer la correspondencia guardada ({exc.detail})')
        if not candidate and sku and store['token']:
            try:
                matches=shopify_search(store,sku,5)
                candidate=next((p for p in matches if any(v.get('sku')==sku for v in p.get('variants',{}).get('nodes',[]))),None)
                if candidate: candidate=shopify_product(store,candidate['id'],query_locales)
            except HTTPException as exc: notices.append(f'{store["code"]}: no se pudo buscar el SKU ({exc.detail})')
        if candidate:
            for locale in locales:
                resolved=locale_map[locale]
                label=f'{store["code"]} · {locale}'+(f' (Shopify {resolved})' if resolved!=locale else '')
                primary=primary_locales.get(store['code'])
                mode='base' if primary and locale==primary else 'translation' if primary and locale!=primary else 'readonly'
                targets.append({'store':store['code'],'domain':store['domain'],'locale':locale,'lookup_locale':resolved,'label':label,'product':candidate,'uses_base_fields':store['domain']!=source_store['domain'],'mode':mode})
        if candidate and candidate.get('translation_error'):
            notices.append(f'{store["code"]}: se muestran los campos actuales de la tienda; Shopify no devolvió sus traducciones registradas.')
    # ES, NL, and PT are configured as codes/markets for the same Shopify shop.
    # Keep one column for each actual product+locale, preferring the market label.
    unique_targets={}
    for target in targets:
        key=(target['domain'],(target['product'] or {}).get('id'),target['lookup_locale'])
        prior=unique_targets.get(key)
        if prior is None or (prior['store']=='ES' and target['store']!='ES'):
            unique_targets[key]=target
    targets=list(unique_targets.values())
    def translations(product,locale): return {x['key']:x.get('value','') for x in (product or {}).get('translations_by_locale',{}).get(locale,[])}
    source_variants=source.get('variants',{}).get('nodes',[])
    source_base={'title':source.get('title',''),'descriptionHtml':source.get('descriptionHtml',''),'handle':source.get('handle',''),'vendor':source.get('vendor',''),'productType':source.get('productType',''),'status':source.get('status',''),'tags':', '.join(source.get('tags') or []),'createdAt':source.get('createdAt',''),'updatedAt':source.get('updatedAt',''),'seo.title':(source.get('seo') or {}).get('title',''),'seo.description':(source.get('seo') or {}).get('description',''),'variants.sku':', '.join(v.get('sku','') for v in source_variants if v.get('sku')),'variants.barcode':', '.join(v.get('barcode','') for v in source_variants if v.get('barcode')),'variants.title':', '.join(v.get('title','') for v in source_variants),'images.altText':', '.join(i.get('altText') or '' for i in source.get('images',{}).get('nodes',[]))}
    keymap={'title':'title','descriptionHtml':'body_html','handle':'handle','vendor':'vendor','productType':'product_type','status':'status','tags':'tags','createdAt':'created_at','updatedAt':'updated_at','seo.title':'seo_title','seo.description':'seo_description','variants.sku':'sku','variants.barcode':'barcode','variants.title':'variant_title','images.altText':'image_alt_text'}
    fields=[]
    variant_mapping_errors=[]
    for key,val in source_base.items():
        values=[val]
        edits=[None]
        for target in targets:
            product=target['product']; tr=translations(product,target['lookup_locale'])
            value=product_field_value(product,key) if target['mode']=='base' else tr.get(keymap[key],'')
            if not value and target.get('uses_base_fields') and target['mode']=='translation': value=product_field_value(product,key)
            values.append(value)
            editable={'title','body_html','handle','seo_title','seo_description','vendor','product_type'}
            edits.append({'store':target['store'],'resource_id':product['id'],'owner_id':product['id'],'source_product_gid':source_id,'locale':target['lookup_locale'],'key':keymap[key],'value':value,'source_value':val,'mode':target['mode']} if product and target['mode']!='readonly' and keymap.get(key) in editable else None)
        fields.append({'key':key,'label':key.replace('.',' / '),'values':values,'edits':edits})
    # Rows are defined by the Spanish source. A missing key and an empty value
    # are shown distinctly in every destination.
    def is_yotpo(mf):
        return 'yotpo' in str(mf.get('namespace','')).casefold() or 'yotpo' in str(mf.get('key','')).casefold()
    def mf_value_raw(mf, locale=None):
        if mf is None: return None
        localized=next((item for item in mf.get('translations_by_locale',{}).get(locale,[]) if item.get('key')=='value'),None) if locale else None
        return localized.get('value') if localized else mf.get('value')
    def mf_value(mf, product=None, locale=None):
        if mf is None: return 'Falta el metacampo'
        value=mf_value_raw(mf,locale)
        return value if value not in (None,'') else 'Metacampo vacío'
    source_mfs=[item for item in source.get('metafields',{}).get('nodes',[]) if not is_yotpo(item)]
    for mf in sorted(source_mfs,key=lambda item:(item.get('namespace',''),item.get('key',''))):
        namespace,mfkey=mf['namespace'],mf['key']
        key=f'metafield.product.{namespace}.{mfkey}'
        values=[mf_value(mf)]
        edits=[None]
        for target in targets:
            if not target['product']:
                values.append('')
                edits.append(None)
                continue
            dest=next((item for item in (target['product'] or {}).get('metafields',{}).get('nodes',[]) if item['namespace']==namespace and item['key']==mfkey and not is_yotpo(item)),None)
            raw_mf_value=mf_value_raw(dest,target['lookup_locale'] if target['mode']=='translation' else None) if dest else None
            values.append(mf_value(dest,target['product'],target['lookup_locale'] if target['mode']=='translation' else None))
            edits.append({'store':target['store'],'resource_id':dest['id'],'owner_id':target['product']['id'],'source_product_gid':source_id,'locale':target['lookup_locale'],'key':'value','value':raw_mf_value,'source_value':mf_value_raw(mf),'namespace':namespace,'metafield_key':mfkey,'metafield_type':dest.get('type'),'compare_digest':dest.get('compareDigest'),'mode':target['mode']} if dest and target['mode']!='readonly' and dest.get('type') in {'single_line_text_field','multi_line_text_field','rich_text_field','html','list.single_line_text_field','list.multi_line_text_field'} else None)
        fields.append({'key':key,'label':f'Producto · {namespace} / {mfkey} ({mf.get("type","")})','values':values,'edits':edits})
    for source_variant in source_variants:
        variant_mfs=[item for item in source_variant.get('metafields',{}).get('nodes',[]) if not is_yotpo(item)]
        identity=source_variant.get('sku') or source_variant.get('title') or source_variant['id']
        identity_label=f'SKU {source_variant["sku"]}' if source_variant.get('sku') else source_variant.get('title') or source_variant['id']
        for mf in sorted(variant_mfs,key=lambda item:(item.get('namespace',''),item.get('key',''))):
            namespace,mfkey=mf['namespace'],mf['key']
            key=f'metafield.variant.{identity}.{namespace}.{mfkey}'
            values=[mf_value(mf)]
            edits=[None]
            for target in targets:
                if not target['product']:
                    values.append('')
                    edits.append(None)
                    continue
                target_variants=(target['product'] or {}).get('variants',{}).get('nodes',[])
                # Match across shops by SKU first, then by the selected option values
                # and position. Variant titles can be translated, so they are not a
                # reliable cross-store identity.
                source_options=tuple((item.get('name',''),item.get('value','')) for item in source_variant.get('selectedOptions',[]))
                dest_variant=next((v for v in target_variants if source_variant.get('sku') and v.get('sku') and v['sku'].strip().casefold()==source_variant['sku'].strip().casefold()),None)
                if dest_variant is None and source_options:
                    dest_variant=next((v for v in target_variants if tuple((item.get('name',''),item.get('value','')) for item in v.get('selectedOptions',[]))==source_options),None)
                if dest_variant is None and source_variant.get('position') is not None:
                    dest_variant=next((v for v in target_variants if v.get('position')==source_variant.get('position')),None)
                if dest_variant is None and target['domain']==source_store['domain']:
                    dest_variant=next((v for v in target_variants if v.get('id')==source_variant.get('id')),None)
                dest_mf=next((item for item in (dest_variant or {}).get('metafields',{}).get('nodes',[]) if item['namespace']==namespace and item['key']==mfkey and not is_yotpo(item)),None)
                if target['mode']!='readonly' and (dest_variant is None or dest_mf is None):
                    reason='No se encontró la variante equivalente en destino' if dest_variant is None else 'La variante de destino no tiene este metacampo'
                    variant_mapping_errors.append({'store':target['store'],'locale':target['locale'],'field_key':key,'field_label':f'Variante · {identity_label} · {namespace} / {mfkey}','error':reason})
                raw_mf_value=mf_value_raw(dest_mf,target['lookup_locale'] if target['mode']=='translation' else None) if dest_mf else None
                values.append(mf_value(dest_mf,target['product'],target['lookup_locale'] if target['mode']=='translation' else None))
                edits.append({'store':target['store'],'resource_id':dest_mf['id'],'owner_id':dest_variant['id'] if dest_variant else '', 'source_product_gid':source_id,'locale':target['lookup_locale'],'key':'value','value':raw_mf_value,'source_value':mf_value_raw(mf),'namespace':namespace,'metafield_key':mfkey,'metafield_type':dest_mf.get('type'),'compare_digest':dest_mf.get('compareDigest'),'mode':target['mode']} if dest_mf and target['mode']!='readonly' and dest_mf.get('type') in {'single_line_text_field','multi_line_text_field','rich_text_field','html','list.single_line_text_field','list.multi_line_text_field'} else None)
            fields.append({'key':key,'label':f'Variante · {identity_label} · {namespace} / {mfkey} ({mf.get("type","")})','values':values,'edits':edits})
    # Keep synchronization state per editable field and destination. Older Shopify
    # values have no baseline and are deliberately reported as unknown until saved
    # through this hub once.
    with pool.connection() as c:
        sync_rows=c.execute('SELECT store_code,locale,resource_id,field_key,source_hash,target_hash FROM product_field_sync_state WHERE source_product_gid=%s',(source_id,)).fetchall()
    sync_state={(row[0],row[1],row[2],row[3]):(row[4],row[5]) for row in sync_rows}
    def value_hash(value): return hashlib.sha256(str(value or '').encode()).hexdigest()
    for field in fields:
        for idx,edit in enumerate(field['edits']):
            if not edit: continue
            source_value=edit.get('source_value') if edit.get('source_value') is not None else (field['values'][0] if field['values'] else '')
            destination_value=edit.get('value') or ''
            baseline=sync_state.get((edit['store'],edit['locale'],edit['resource_id'],edit['key']))
            if not destination_value and source_value:
                status='MISSING'
            elif not baseline:
                status='UNTRACKED'
            elif value_hash(destination_value)!=baseline[1]:
                status='CONFLICT'
            elif value_hash(source_value)!=baseline[0]:
                status='OUTDATED'
            else:
                status='CURRENT'
            edit['sync_status']=status
    columns=[{'label':'ES · español (base)'}]+[{'label':t['label']} for t in targets]
    if not sku: notices.append('El producto de ES no tiene SKU; no se pudo proponer una correspondencia automática en otras tiendas.')
    for t in targets:
        if not t['product']: notices.append(f'{t["store"]} · {t["locale"]}: no hay producto equivalente localizado por correspondencia o SKU.')
    return {'source':{'id':source['id'],'title':source['title'],'sku':sku},'columns':columns,'fields':fields,'notices':list(dict.fromkeys(notices)),'variant_mapping_errors':variant_mapping_errors}

def remember_manual_correction(x:CellTranslation):
    src=(x.source_value or '').strip()
    dst=x.value.strip()
    if src and dst and src!=dst and len(src)<=500 and len(dst)<=500:
        with pool.connection() as c:
            c.execute("INSERT INTO translation_memory(target_store,source_locale,target_locale,source_text,target_text) VALUES(%s,'es-ES',%s,%s,%s) ON CONFLICT(target_store,source_locale,target_locale,source_text) WHERE target_store IS NOT NULL DO UPDATE SET target_text=EXCLUDED.target_text,created_at=now()",(x.store,x.locale,src,dst))

def slugify_handle(value:str)->str:
    import unicodedata
    text=unicodedata.normalize('NFKD',value or '').encode('ascii','ignore').decode().lower()
    return re.sub(r'-{2,}','-',re.sub(r'[^a-z0-9]+','-',text)).strip('-')[:255]

def fold_accent_chars(value:str)->str:
    import unicodedata
    # Carácter a carácter para conservar la longitud original.
    return ''.join((d[0] if (d:=unicodedata.normalize('NFD',ch)) and d[0].isascii() else ch) for ch in value)

@app.post('/api/translate-cell')
def translate_cell(x:CellTranslateRequest,user=Depends(operator)):
    if x.field_key=='handle':
        # Traduce las palabras del slug y vuelve a normalizarlo a un handle válido.
        words=translate_cell(x.model_copy(update={'source_text':x.source_text.replace('-',' ').strip(),'field_key':'handle_text','html':False}),user)
        slug=slugify_handle(words['translation'])
        if not slug: raise HTTPException(422,'La traducción del slug quedó vacía')
        return {**words,'translation':slug}
    profile=target_profile(x.target_store,x.target_locale)
    if not profile: raise HTTPException(400,f'{x.target_store} · {x.target_locale} no tiene un perfil de traducción activo')
    if x.metafield_type in {'rich_text_field','list.single_line_text_field','list.multi_line_text_field'}:
        try: structured=json.loads(x.source_text)
        except (TypeError,json.JSONDecodeError): raise HTTPException(400,'El metacampo estructurado no contiene JSON válido')
        memory_hits=0; glossary_terms=0; translated_nodes=0
        def translate_leaf(source):
            nonlocal memory_hits,glossary_terms,translated_nodes
            if not source.strip(): return source
            leaf=CellTranslateRequest(source_text=source,source_store=x.source_store,source_locale=x.source_locale,target_store=x.target_store,target_locale=x.target_locale,field_key='metafield_text',html=False)
            result=translate_cell(leaf,user)
            memory_hits+=bool(result.get('memory_hit')); glossary_terms+=int(result.get('glossary_terms',0)); translated_nodes+=1
            return result['translation']
        def walk_text_nodes(node):
            if isinstance(node,dict):
                if node.get('type')=='text' and isinstance(node.get('value'),str):
                    node['value']=translate_leaf(node['value'])
                else:
                    for child in node.values(): walk_text_nodes(child)
            elif isinstance(node,list):
                for child in node: walk_text_nodes(child)
        if x.metafield_type=='rich_text_field':
            if not isinstance(structured,dict) or structured.get('type')!='root': raise HTTPException(400,'Rich text debe tener un nodo raíz Shopify válido')
            walk_text_nodes(structured)
        else:
            if not isinstance(structured,list) or not all(isinstance(item,str) for item in structured): raise HTTPException(400,'La lista del metacampo debe contener textos')
            structured=[translate_leaf(item) for item in structured]
        if not translated_nodes: raise HTTPException(400,'El metacampo no contiene texto traducible')
        return {'translation':json.dumps(structured,ensure_ascii=False,separators=(',',':')),'memory_hit':memory_hits==translated_nodes,'glossary_terms':glossary_terms,'require_review':profile['require_review']}
    if os.getenv('TRANSLATION_PROVIDER','deepl').lower()!='claude' and not os.getenv('DEEPL_AUTH_KEY','').strip(): raise HTTPException(503,'DeepL no está configurado en el servidor')
    with pool.connection() as c:
        exact=c.execute("""SELECT target_text FROM translation_memory
            WHERE target_store=%s
              AND split_part(lower(source_locale),'-',1)=split_part(lower(%s),'-',1)
              AND split_part(lower(target_locale),'-',1)=split_part(lower(%s),'-',1)
              AND source_text=%s
            ORDER BY ((lower(source_locale)=lower(%s))::int + (lower(target_locale)=lower(%s))::int) DESC
            LIMIT 1""",(x.target_store,x.source_locale,x.target_locale,x.source_text.strip(),x.source_locale,x.target_locale)).fetchone()
        terms=c.execute("""SELECT source_term,target_term FROM (
              SELECT DISTINCT ON (lower(source_term)) source_term,target_term,
                (((source_store=%s AND target_store=%s)::int * 2)
                  + (lower(source_locale)=lower(%s))::int
                  + (lower(target_locale)=lower(%s))::int) AS specificity
              FROM translation_glossary
              WHERE enabled
                AND ((source_store=%s AND target_store=%s) OR (source_store IS NULL AND target_store IS NULL))
                AND split_part(lower(source_locale),'-',1)=split_part(lower(%s),'-',1)
                AND split_part(lower(target_locale),'-',1)=split_part(lower(%s),'-',1)
              ORDER BY lower(source_term),specificity DESC,updated_at DESC
            ) preferred_terms ORDER BY length(source_term) DESC""",
            (x.source_store,x.target_store,x.source_locale,x.target_locale,x.source_store,x.target_store,x.source_locale,x.target_locale)).fetchall()
    # Keep approved terms intact across DeepL without exposing a glossary or API key
    # to the browser. Replace only text nodes when the value contains HTML.
    import html as html_module
    tokens={}; text=x.source_text; token_index=0
    def preserve_term_case(source, target):
        letters=''.join(char for char in source if char.isalpha())
        if letters and letters.isupper(): return target.upper()
        if letters and letters.islower(): return target.lower()
        if letters and letters[0].isupper() and letters[1:].islower():
            return target[:1].upper()+target[1:].lower()
        return target
    segments=re.split(r'(<[^>]+>)',text) if x.html else [text]
    # Un slug llega sin tildes: el glosario debe coincidir sin distinguirlas.
    fold_accents=x.field_key=='handle_text'
    if fold_accents: terms=[(fold_accent_chars(s),t) for s,t in terms]
    for i,segment in enumerate(segments):
        if segment.startswith('<') and segment.endswith('>'): continue
        if fold_accents: segment=fold_accent_chars(segment)
        for n,(source_term,target_term) in enumerate(terms):
            pattern=re.compile(r'(?<!\w)'+re.escape(source_term)+r'(?!\w)',re.IGNORECASE)
            def protect(match):
                nonlocal token_index
                marker=f'ZXQGLOSSARY{token_index:06d}QXZ'
                token_index+=1
                replacement=preserve_term_case(match.group(),target_term)
                tokens[marker]=html_module.escape(replacement) if x.html else replacement
                return marker
            segment=pattern.sub(protect,segment)
        segments[i]=segment
    protected=''.join(segments)
    # A saved translation may predate a glossary rule. Reuse memory only when
    # this source text contains no term that the current scoped glossary protects.
    if exact and not tokens: return {'translation':exact[0],'memory_hit':True,'glossary_terms':0,'require_review':profile['require_review']}
    try:
        from .worker import translate_texts
        translated=translate_texts([protected],profile['deepl_target'],handle_html=x.html,formality=profile['formality'],source_lang='ES')[0]
    except RuntimeError as exc:
        raise HTTPException(502,str(exc)[:500])
    translated=strip_added_quotes(x.source_text,translated,x.html)
    for marker,target_term in tokens.items(): translated=translated.replace(marker,target_term)
    return {'translation':translated,'memory_hit':False,'glossary_terms':len(tokens),'require_review':profile['require_review']}

@app.get('/api/translation-context')
def translation_context(store:str,locale:str,source_store:str='ES',source_locale:str|None=None):
    """Context for an agent doing its own translation: published locale, profile, and matching glossary."""
    target_code=store.strip().upper(); source_code=source_store.strip().upper()
    target=shopify_store_by_code(target_code)
    if not target or not target['token']: raise HTTPException(404,'Tienda destino inactiva o sin credenciales: '+target_code)
    source=shopify_store_by_code(source_code)
    if not source or not source['token']: raise HTTPException(404,'Tienda origen inactiva o sin credenciales: '+source_code)
    source_locales=shopify_shop_locales(source)
    resolved_source_locale=source_locale or next((item['locale'] for item in source_locales if item.get('primary')),'es-ES')
    target_locales=shopify_shop_locales(target)
    locale_info=next((item for item in target_locales if item.get('locale','').casefold()==locale.casefold()),None)
    profile=target_profile(target_code,locale)
    with pool.connection() as c:
        rows=c.execute("""SELECT DISTINCT ON (lower(source_term)) source_term,target_term,
                (((source_store=%s AND target_store=%s)::int * 2)
                  + (lower(source_locale)=lower(%s))::int
                  + (lower(target_locale)=lower(%s))::int) AS specificity
              FROM translation_glossary
              WHERE enabled
                AND ((source_store=%s AND target_store=%s) OR (source_store IS NULL AND target_store IS NULL))
                AND split_part(lower(source_locale),'-',1)=split_part(lower(%s),'-',1)
                AND split_part(lower(target_locale),'-',1)=split_part(lower(%s),'-',1)
              ORDER BY lower(source_term),specificity DESC,updated_at DESC""",
            (source_code,target_code,resolved_source_locale,locale,source_code,target_code,resolved_source_locale,locale)).fetchall()
    terms=[{'source_term':row[0],'target_term':row[1]} for row in sorted(rows,key=lambda row:len(row[0]),reverse=True)]
    return {'source_store':source_code,'source_locale':resolved_source_locale,'target_store':target_code,
            'target_locale':locale,'locale':locale_info,'profile':({k:profile[k] for k in ('role','formality','require_review')} if profile else None),
            'glossary_terms':terms}

@app.get('/api/glossary')
def list_glossary(target_locale:str|None=None,store:str|None=None,q:str|None=None,user=Depends(operator)):
    where=[]; params=[]
    if target_locale: where.append('target_locale=%s'); params.append(target_locale)
    if store: where.append('(source_store=%s OR target_store=%s)'); params+=[store,store]
    if q and q.strip():
        like='%'+q.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        where.append('(source_term ILIKE %s OR target_term ILIKE %s)'); params+=[like,like]
    clause=('WHERE '+' AND '.join(where)) if where else ''
    with pool.connection() as c:
        rows=c.execute(f"SELECT id,source_store,source_locale,target_store,target_locale,source_term,target_term,enabled,origin FROM translation_glossary {clause} ORDER BY source_store,target_store,target_locale,source_term",params).fetchall()
    return {'terms':[dict(zip(('id','source_store','source_locale','target_store','target_locale','source_term','target_term','enabled','origin'),r)) for r in rows]}

@app.post('/api/glossary')
def add_glossary_term(x:GlossaryTerm,user=Depends(operator)):
    active={item['code']:item for item in shopify_stores()}
    if x.source_store not in active or x.target_store not in active: raise HTTPException(400,'Selecciona tiendas activas de la lista')
    if x.source_locale not in active[x.source_store]['locales']: raise HTTPException(400,'El idioma origen no está configurado en la tienda origen')
    if x.target_locale not in active[x.target_store]['locales']: raise HTTPException(400,'El idioma destino no está configurado en la tienda destino')
    with pool.connection() as c:
        row=c.execute("""INSERT INTO translation_glossary(source_store,source_locale,target_store,target_locale,source_term,target_term,enabled)
          VALUES(%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT (source_store,source_locale,target_store,target_locale,(lower(source_term)))
          WHERE source_store IS NOT NULL AND target_store IS NOT NULL
          DO UPDATE SET target_term=EXCLUDED.target_term,enabled=EXCLUDED.enabled,updated_at=now()
          RETURNING id""",(x.source_store,x.source_locale,x.target_store,x.target_locale,x.source_term,x.target_term,x.enabled)).fetchone()
    return {'id':row[0],'saved':True}

@app.delete('/api/glossary/{term_id}')
def delete_glossary_term(term_id:int,user=Depends(operator)):
    with pool.connection() as c: c.execute('DELETE FROM translation_glossary WHERE id=%s',(term_id,))
    return {'deleted':True}

@app.post('/api/shopify/translations')
def save_shopify_translation(x:CellTranslation,user=Depends(operator)):
    store=next((item for item in shopify_stores() if item['code']==x.store),None)
    if not store or not store['token']: raise HTTPException(404,'Tienda sin configurar')
    enabled_locales=shopify_shop_locales(store)
    shop_locale=next((item for item in enabled_locales if item.get('locale')==x.locale and item.get('published')),None)
    if not shop_locale: raise HTTPException(400,'El locale no está publicado en Shopify: '+', '.join(item.get('locale','') for item in enabled_locales if item.get('published')))
    if x.store=='ES':
        if shop_locale.get('primary'):
            raise HTTPException(400,'Desde ES solo se guardan traducciones para locales secundarios')
        mode='translation'
    elif shop_locale.get('primary'):
        mode='base'
    else:
        mode='translation'
    resource_type=x.resource_id.removeprefix('gid://shopify/')
    if mode=='base':
        if resource_type.startswith('Metafield/'):
            if x.key!='value' or not x.namespace or not x.owner_id.startswith(('gid://shopify/Product/','gid://shopify/ProductVariant/')) or x.metafield_type not in {'single_line_text_field','multi_line_text_field','rich_text_field','html','list.single_line_text_field','list.multi_line_text_field'}:
                raise HTTPException(400,'Datos de metacampo no válidos para editar el contenido base')
            mutation='''mutation SetBaseMetafield($metafields:[MetafieldsSetInput!]!) { metafieldsSet(metafields:$metafields) { userErrors { field message } } }'''
            if not x.metafield_key: raise HTTPException(400,'Falta la clave Shopify del metacampo')
            variables={'metafields':[{'ownerId':x.owner_id,'namespace':x.namespace,'key':x.metafield_key,'type':x.metafield_type,'value':x.value,'compareDigest':x.compare_digest}]}
            result=shopify_graphql(store['domain'],store['token'],mutation,variables).get('metafieldsSet') or {}
            errors=result.get('userErrors',[])
            if errors: raise HTTPException(400,'Shopify: '+'; '.join(error.get('message','error') for error in errors)[:500])
            remember_manual_correction(x)
            record_field_sync_state(x)
            return {'store':x.store,'resource_id':x.resource_id,'locale':x.locale,'key':x.key,'mode':'base','saved':True}
        if not resource_type.startswith('Product/') or x.owner_id!=x.resource_id:
            raise HTTPException(400,'El recurso de contenido base debe ser el producto de esa tienda')
        fields={'title':('title',None),'body_html':('descriptionHtml',None),'handle':('handle',None),'vendor':('vendor',None),'product_type':('productType',None),'seo_title':('seo.title','title'),'seo_description':('seo.description','description')}
        if x.key not in fields: raise HTTPException(400,'El campo no está habilitado para actualizar el contenido base')
        field,seo_field=fields[x.key]
        product={'id':x.resource_id}
        if seo_field:
            # ProductUpdateInput.seo replaces the whole object: resend the sibling field or Shopify clears it.
            current=shopify_graphql(store['domain'],store['token'],'query($id:ID!){product(id:$id){seo{title description}}}',{'id':x.resource_id}).get('product') or {}
            product['seo']={**{k:v for k,v in (current.get('seo') or {}).items() if v is not None},seo_field:x.value}
        else: product[field]=x.value
        if x.key=='handle': product['redirectNewHandle']=True
        mutation='''mutation UpdateBaseProduct($product:ProductUpdateInput!) { productUpdate(product:$product) { userErrors { field message } } }'''
        result=shopify_graphql(store['domain'],store['token'],mutation,{'product':product}).get('productUpdate') or {}
        errors=result.get('userErrors',[])
        if errors: raise HTTPException(400,'Shopify: '+'; '.join(error.get('message','error') for error in errors)[:500])
        remember_manual_correction(x)
        record_field_sync_state(x)
        return {'store':x.store,'resource_id':x.resource_id,'locale':x.locale,'key':x.key,'mode':'base','saved':True}
    if resource_type.startswith('Metafield/'):
        if x.key!='value': raise HTTPException(400,'La clave del metacampo no es válida')
    elif resource_type.startswith(('Product/','ProductVariant/')):
        if x.key not in {'title','body_html','handle','seo_title','seo_description','vendor','product_type'}:
            raise HTTPException(400,'El campo no está habilitado para traducción')
    else: raise HTTPException(400,'Tipo de recurso Shopify no admitido')
    shopify_key={'seo_title':'meta_title','seo_description':'meta_description'}.get(x.key,x.key)
    resource_query='''query TranslationDigest($id:ID!) { translatableResource(resourceId:$id) { resourceId translatableContent { key value digest } } }'''
    resource=shopify_graphql(store['domain'],store['token'],resource_query,{'id':x.resource_id}).get('translatableResource')
    digest=next((item.get('digest') for item in (resource or {}).get('translatableContent',[]) if item.get('key')==shopify_key),None)
    if not digest: raise HTTPException(409,'Shopify no ofrece un digest traducible para ese campo; comprueba el tipo de metacampo y los permisos')
    mutation='''mutation RegisterTranslation($id:ID!,$translations:[TranslationInput!]!) { translationsRegister(resourceId:$id,translations:$translations) { userErrors { field message } translations { key value locale } } }'''
    result=shopify_graphql(store['domain'],store['token'],mutation,{'id':x.resource_id,'translations':[{'locale':x.locale,'key':shopify_key,'value':x.value,'translatableContentDigest':digest}]}).get('translationsRegister') or {}
    errors=result.get('userErrors',[])
    if errors: raise HTTPException(400,'Shopify: '+'; '.join(error.get('message','error') for error in errors)[:500])
    remember_manual_correction(x)
    record_field_sync_state(x)
    return {'store':x.store,'resource_id':x.resource_id,'locale':x.locale,'key':x.key,'mode':'translation','saved':True}

def record_field_sync_state(x:CellTranslation):
    source_id=x.source_product_gid or (x.owner_id if x.owner_id.startswith('gid://shopify/Product/') else '')
    if not source_id: return
    digest=lambda value: hashlib.sha256(str(value or '').encode()).hexdigest()
    with pool.connection() as c:
        c.execute('''INSERT INTO product_field_sync_state(source_product_gid,store_code,locale,resource_id,field_key,source_hash,target_hash)
          VALUES(%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT(source_product_gid,store_code,locale,resource_id,field_key) DO UPDATE
          SET source_hash=EXCLUDED.source_hash,target_hash=EXCLUDED.target_hash,synced_at=now()''',
          (source_id,x.store,x.locale,x.resource_id,x.key,digest(x.source_value),digest(x.value)))

def translation_batch_payload(batch_id:int):
    with pool.connection() as c:
        batch=c.execute('SELECT id,source_sku,source_product_gid,source_title,state,error,created_at,updated_at FROM translation_batches WHERE id=%s',(batch_id,)).fetchone()
        if not batch: raise HTTPException(404,'Lote de traducción no encontrado')
        rows=c.execute('SELECT id,store_code,locale,field_key,field_label,source_text,proposed_text,edit_payload,state,error,created_at,updated_at,quality FROM translation_batch_items WHERE batch_id=%s ORDER BY id',(batch_id,)).fetchall()
    return {'id':batch[0],'sku':batch[1],'source_product_gid':batch[2],'source_title':batch[3],'state':batch[4],'error':batch[5],'created_at':batch[6],'updated_at':batch[7],
      'items':[dict(zip(('id','store','locale','field_key','field_label','source_text','proposed_text','edit_payload','state','error','created_at','updated_at','quality'),row)) for row in rows]}

def batch_variant_allowed(key:str,skus:list[str]|None)->bool:
    if skus is None or not key.startswith('metafield.variant.'): return True
    rest=key[len('metafield.variant.'):].casefold()
    return any(rest.startswith(s.strip().casefold()+'.') for s in skus)

def resolve_es_product_by_sku(source_store,sku:str):
    hits=shopify_search(source_store,sku,20)
    unique={p['id']:p for p in hits if any((v.get('sku') or '').strip().casefold()==sku.casefold() for v in p.get('variants',{}).get('nodes',[]))}
    if not unique: raise HTTPException(404,'No se encontró un producto ES con ese SKU exacto')
    unique=prefer_active_products(unique)
    if len(unique)>1: raise HTTPException(409,'El SKU coincide con varios productos de ES')
    return next(iter(unique.values()))

@app.post('/api/translation-batches/resolve')
def resolve_translation_batch_skus(x:TranslationBatchResolve,user=Depends(operator)):
    groups,errors=resolve_skus_to_groups(x.skus)
    return {'groups':groups,'errors':errors}

@app.post('/api/translation-batches/parse-skus')
async def parse_translation_batch_skus(request:Request,filename:str='skus.txt',user=Depends(operator)):
    """Read SKUs from an uploaded xlsx/csv/txt file sent as the raw request body."""
    data=await request.body()
    try: skus=extract_skus(filename[:200],data)
    except ValueError as exc: raise HTTPException(400,str(exc))
    except Exception: raise HTTPException(400,'No se pudo leer el archivo')
    return {'skus':skus,'count':len(skus)}

@app.get('/api/translation-batches')
def list_translation_batches(user=Depends(operator)):
    with pool.connection() as c:
        rows=c.execute('SELECT id,source_sku,source_title,state,error,created_at,updated_at FROM translation_batches ORDER BY updated_at DESC,id DESC LIMIT 200').fetchall()
    return [dict(zip(('id','sku','source_title','state','error','created_at','updated_at'),row)) for row in rows]

@app.get('/api/translation-batches/search')
def search_translation_batches(sku:str,user=Depends(operator)):
    sku=sku.strip()
    if not sku or len(sku)>120: raise HTTPException(422,'Indica un SKU de hasta 120 caracteres')
    like='%'+sku.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
    # Un lote se registra con el SKU con el que se lanzó; si el SKU existe en ES,
    # también se buscan los lotes del mismo producto (otras variantes).
    gid=None
    try:
        source_store=next((s for s in shopify_stores() if s['code']=='ES' and s['token']),None)
        if source_store: gid=resolve_es_product_by_sku(source_store,sku)['id']
    except HTTPException: gid=None
    with pool.connection() as c:
        rows=c.execute("""SELECT b.id,b.source_sku,b.source_title,b.source_product_gid,b.state,b.error,b.created_at,b.updated_at,
            (b.source_sku ILIKE %s ESCAPE '\\') AS sku_match
            FROM translation_batches b WHERE b.source_sku ILIKE %s ESCAPE '\\' OR (%s::text IS NOT NULL AND b.source_product_gid=%s)
            ORDER BY b.created_at DESC,b.id DESC LIMIT 100""",(like,like,gid,gid)).fetchall()
        ids=[r[0] for r in rows]
        items=c.execute("""SELECT batch_id,store_code,locale,state,count(*),max(updated_at) FROM translation_batch_items
            WHERE batch_id=ANY(%s) GROUP BY batch_id,store_code,locale,state ORDER BY store_code,locale""",(ids,)).fetchall() if ids else []
    by_batch={}
    for batch_id,store,locale,state,n,last in items:
        entry=by_batch.setdefault(batch_id,{}).setdefault((store,locale),{'store':store,'locale':locale,'states':{},'updated_at':last})
        entry['states'][state]=n
        if last>entry['updated_at']: entry['updated_at']=last
    return {'sku':sku,'product_gid':gid,'batches':[{'id':r[0],'sku':r[1],'source_title':r[2],'state':r[4],'error':r[5],'created_at':r[6],'updated_at':r[7],
        'match':'sku' if r[8] else 'producto','targets':list(by_batch.get(r[0],{}).values())} for r in rows]}

@app.post('/api/translation-batches')
def create_translation_batch(x:TranslationBatchRequest,user=Depends(operator)):
    sku=x.sku.strip()
    stores=shopify_stores(); source_store=next((s for s in stores if s['code']=='ES' and s['token']),None)
    if not source_store: raise HTTPException(503,'La tienda ES no tiene dominio y token configurados')
    source_product=resolve_es_product_by_sku(source_store,sku)
    allowed_skus=[sku,*[v.strip() for v in x.also_skus if v.strip()]] if x.variants_of_skus_only else None
    label_sku=', '.join(dict.fromkeys([sku,*[v.strip() for v in x.also_skus if v.strip()]]))
    source_locale=next((item['locale'] for item in shopify_shop_locales(source_store) if item.get('primary')),'es-ES')
    targets=[]; target_locales={}; primaries={}
    for store in stores:
        if not store['token']: continue
        try: locales=shopify_shop_locales(store)
        except HTTPException: continue
        primaries[store['code']]=next((item['locale'] for item in locales if item.get('primary')),None)
        profiles=active_profiles(store['code'])
        published=[item['locale'] for item in locales if item.get('published') and not (store['code']=='ES' and item.get('primary')) and (profiles.get(item['locale'].lower()) or {}).get('deepl_target')]
        targets.extend((store['code'],locale) for locale in published)
        if published: target_locales[store['code']]=published
    selected_stores=set(x.stores or ([x.store] if x.store else []))
    selected_locales={v.lower() for v in (x.locales or ([x.locale] if x.locale else []))}
    usable_create=[c for c in x.create_stores if c in {t[0] for t in targets} and primaries.get(c) and (not selected_stores or c in selected_stores)]
    create_problems={c:'No se puede crear: la tienda no tiene locales publicados con perfil de traducción activo, o no está entre las tiendas seleccionadas' for c in x.create_stores if c not in usable_create}
    followup_locales={c:[l for (s,l) in targets if s==c and l!=primaries.get(c) and (not selected_locales or l.lower() in selected_locales)] for c in usable_create}
    if selected_stores: targets=[t for t in targets if t[0] in selected_stores]
    if selected_locales: targets=[t for t in targets if t[1].lower() in selected_locales]
    if not targets and not usable_create: raise HTTPException(409,'No hay locales publicados de destino en las tiendas activas para la selección indicada')
    target_locales={store:[l for l in locales if (store,l) in set(targets)] for store,locales in target_locales.items()}
    target_locales={store:locales for store,locales in target_locales.items() if locales}
    with pool.connection() as c:
        batch_id=c.execute("INSERT INTO translation_batches(source_sku,source_product_gid,source_title,state) VALUES(%s,%s,%s,'PREPARING') RETURNING id",(label_sku,source_product['id'],source_product['title'])).fetchone()[0]
    try:
        comparison=_compare_shopify_product(source_product['id'],target_locales=target_locales)
    except HTTPException as exc:
        with pool.connection() as c:
            for store_code,locale in targets:
                c.execute('''INSERT INTO translation_batch_items(batch_id,store_code,locale,field_key,field_label,state,error)
                  VALUES(%s,%s,%s,'__product__','Producto','ERROR',%s) ON CONFLICT(batch_id,store_code,locale,field_key) DO NOTHING''',
                  (batch_id,store_code,locale,str(exc.detail)[:500]))
            c.execute("UPDATE translation_batches SET state='READY_WITH_ERRORS',error=%s,updated_at=now() WHERE id=%s",(str(exc.detail)[:500],batch_id))
        return translation_batch_payload(batch_id)
    empty_message='Ninguno de los campos seleccionados existe, tiene contenido en origen o tiene equivalente editable en este destino' if x.fields is not None else 'No se encontró un producto equivalente o campos principales editables en el destino'
    quota_error=None  # once DeepL reports no quota left, remaining fields fail fast instead of calling it again
    creating=set()
    def add_item(store_code,locale,key,label,state,error=None,source_text='',proposed=None,payload=None):
        with pool.connection() as c:
            c.execute('''INSERT INTO translation_batch_items(batch_id,store_code,locale,field_key,field_label,source_text,proposed_text,edit_payload,state,error)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(batch_id,store_code,locale,field_key) DO NOTHING''',(batch_id,store_code,locale,key,label,source_text,proposed,Jsonb(payload or {}),state,error))
    for code,message in create_problems.items(): add_item(code,primaries.get(code) or '-','__product__','Producto','ERROR',message)
    for code in usable_create:
        locale=primaries[code]
        has_product=any(col['label'].startswith(f'{code} · ') for col in comparison['columns'][1:])
        if has_product: continue
        creating.add(code)
        problem=next((n for n in comparison.get('notices',[]) if n.startswith(code+':')),None)
        if problem: add_item(code,locale,'__product__','Producto','ERROR','No se crea el producto porque no se pudo confirmar que no exista: '+problem[:300]); continue
        if not comparison['source'].get('sku'): add_item(code,locale,'__product__','Producto','ERROR','No se crea el producto: el de ES no tiene SKU, no se podría localizar después'); continue
        try:
            spec=build_product_creation(code,source_product['id'],locale,source_locale,allowed_skus)
            spec['followup']={'sku':sku,'also_skus':[v.strip() for v in x.also_skus if v.strip()],'fields':x.fields,'locales':followup_locales.get(code,[]),'variants_of_skus_only':x.variants_of_skus_only,'auto_apply':x.auto_apply}
            add_item(code,locale,CREATE_ITEM_KEY,'Crear producto archivado','APPROVED',None,source_product['title'],creation_summary(spec),spec)
        except Exception as exc:
            add_item(code,locale,'__product__','Producto','ERROR',str(exc.detail if isinstance(exc,HTTPException) else exc)[:500])
    for store_code,locale in targets:
        if store_code in creating: continue
        column_index=next((i for i,column in enumerate(comparison['columns']) if i>0 and column['label'].startswith(f'{store_code} · {locale}')),None)
        target_items=0
        for field in comparison['fields']:
            if not batch_field_selected(field['key'],x.fields) or not batch_variant_allowed(field['key'],allowed_skus): continue
            edit_index=column_index
            if edit_index is None or edit_index>=len(field.get('edits',[])) or not field['edits'][edit_index]:
                edit_index=next((i for i,item in enumerate(field.get('edits',[])) if item and item.get('store')==store_code and item.get('locale')==locale),None)
            edit=field.get('edits',[None]*len(field.get('values',[])))[edit_index] if edit_index is not None and edit_index<len(field.get('edits',[])) else None
            source_text=(edit.get('source_value') if field['key'].startswith('metafield.') and edit else field['values'][0] if field.get('values') else '')
            if not edit or not source_text: continue
            try:
                if quota_error: raise RuntimeError(quota_error)
                request=CellTranslateRequest(source_text=source_text,source_store='ES',source_locale=source_locale,target_store=store_code,target_locale=locale,field_key=edit['key'],metafield_type=edit.get('metafield_type'),html=field['key']=='descriptionHtml' or edit.get('metafield_type')=='html')
                translated=translate_cell(request,user)
                proposed=enforce_choices(store_code,field['key'],edit.get('metafield_type'),source_text,translated['translation'],locale); error=None
                quality=assess_translation(source_text,proposed,is_html=request.html)
                # Every non-empty translation is approved: the integrity score is only recorded on the item.
                state='APPROVED' if (proposed or '').strip() else 'PENDING_REVIEW'
            except Exception as exc:
                proposed=None; quality=None; state='ERROR'; error=str(exc.detail if isinstance(exc,HTTPException) else exc)[:500]
                if error.startswith('DEEPL_QUOTA'): quota_error=error
            with pool.connection() as c:
                c.execute('''INSERT INTO translation_batch_items(batch_id,store_code,locale,field_key,field_label,source_text,proposed_text,edit_payload,state,error,quality)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(batch_id,store_code,locale,field_key) DO NOTHING''',
                  (batch_id,store_code,locale,field['key'],field['label'],source_text,proposed,Jsonb(edit),state,error,Jsonb(quality) if quality else None))
            target_items+=1
        for missing in comparison.get('variant_mapping_errors',[]):
            if missing['store']!=store_code or missing['locale']!=locale or not batch_field_selected(missing['field_key'],x.fields) or not batch_variant_allowed(missing['field_key'],allowed_skus): continue
            with pool.connection() as c:
                c.execute('''INSERT INTO translation_batch_items(batch_id,store_code,locale,field_key,field_label,state,error)
                  VALUES(%s,%s,%s,%s,%s,'ERROR',%s) ON CONFLICT(batch_id,store_code,locale,field_key) DO NOTHING''',
                  (batch_id,store_code,locale,missing['field_key'],missing['field_label'],missing['error']))
            target_items+=1
        if target_items==0:
            with pool.connection() as c:
                c.execute('''INSERT INTO translation_batch_items(batch_id,store_code,locale,field_key,field_label,state,error)
                  VALUES(%s,%s,%s,'__product__','Producto','ERROR',%s)
                  ON CONFLICT(batch_id,store_code,locale,field_key) DO NOTHING''',(batch_id,store_code,locale,empty_message))
    with pool.connection() as c:
        c.execute("UPDATE translation_batches SET state=CASE WHEN EXISTS(SELECT 1 FROM translation_batch_items WHERE batch_id=%s AND state='PENDING_REVIEW') THEN 'PENDING_REVIEW' WHEN EXISTS(SELECT 1 FROM translation_batch_items WHERE batch_id=%s AND state='APPROVED') THEN 'APPROVED' ELSE 'READY_WITH_ERRORS' END,updated_at=now() WHERE id=%s",(batch_id,batch_id,batch_id))
    if creating:
        # Las creaciones (siempre ARCHIVADAS) se ejecutan sin aprobación previa, también sin auto_apply.
        with pool.connection() as c:
            rows=c.execute("SELECT id,proposed_text,edit_payload FROM translation_batch_items WHERE batch_id=%s AND state='APPROVED' AND field_key=%s ORDER BY id",(batch_id,CREATE_ITEM_KEY)).fetchall()
        if rows: apply_batch_items(batch_id,rows,user)
    if x.auto_apply:
        with pool.connection() as c:
            rows=c.execute("SELECT id,proposed_text,edit_payload FROM translation_batch_items WHERE batch_id=%s AND state='APPROVED' ORDER BY id",(batch_id,)).fetchall()
        if rows: apply_batch_items(batch_id,rows,user)
    return translation_batch_payload(batch_id)

@app.get('/api/translation-batches/{batch_id}')
def get_translation_batch(batch_id:int,user=Depends(operator)):
    return translation_batch_payload(batch_id)

@app.patch('/api/translation-batches/{batch_id}/items/{item_id}')
def edit_translation_batch_item(batch_id:int,item_id:int,x:TranslationBatchItemEdit,user=Depends(operator)):
    with pool.connection() as c:
        current=c.execute("SELECT source_text,field_key,edit_payload FROM translation_batch_items WHERE id=%s AND batch_id=%s AND state IN ('PENDING_REVIEW','APPROVED')",(item_id,batch_id)).fetchone()
        if current and current[1]==CREATE_ITEM_KEY: raise HTTPException(409,'La creación de producto no se edita aquí: ajusta el producto archivado directamente en Shopify')
        if not current: raise HTTPException(409,'El campo no existe o ya se aplicó o falló; solo se editan campos pendientes o aprobados sin aplicar')
        is_html=current[1]=='descriptionHtml' or (current[2] or {}).get('metafield_type')=='html'
        quality=assess_translation(current[0],x.proposed_text,is_html=is_html)
        # Editing an approved proposal invalidates that approval: the new text must be approved again.
        c.execute("UPDATE translation_batch_items SET proposed_text=%s,quality=%s,state='PENDING_REVIEW',updated_at=now() WHERE id=%s AND batch_id=%s",(x.proposed_text,Jsonb(quality),item_id,batch_id))
        c.execute("UPDATE translation_batches SET state='PENDING_REVIEW',updated_at=now() WHERE id=%s",(batch_id,))
    return {'batch_id':batch_id,'item_id':item_id,'updated':True}

@app.post('/api/translation-batches/{batch_id}/approve')
def approve_translation_batch(batch_id:int,x:TranslationBatchItemIds,user=Depends(operator)):
    with pool.connection() as c:
        if not c.execute('SELECT 1 FROM translation_batches WHERE id=%s',(batch_id,)).fetchone(): raise HTTPException(404,'Lote de traducción no encontrado')
        rows=c.execute("UPDATE translation_batch_items SET state='APPROVED',updated_at=now() WHERE batch_id=%s AND id=ANY(%s) AND state='PENDING_REVIEW' RETURNING id",(batch_id,x.item_ids)).fetchall()
        if len(rows)!=len(set(x.item_ids)): raise HTTPException(409,'Uno o más campos no están pendientes para este lote')
        c.execute("""UPDATE translation_batches b SET state=CASE
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='PENDING_REVIEW') THEN 'PENDING_REVIEW'
          ELSE 'APPROVED' END,updated_at=now() WHERE b.id=%s""",(batch_id,))
    return translation_batch_payload(batch_id)

@app.post('/api/translation-batches/{batch_id}/reopen')
def reopen_translation_batch_items(batch_id:int,x:TranslationBatchItemIds,user=Depends(operator)):
    with pool.connection() as c:
        if not c.execute('SELECT 1 FROM translation_batches WHERE id=%s',(batch_id,)).fetchone(): raise HTTPException(404,'Lote de traducción no encontrado')
        rows=c.execute("UPDATE translation_batch_items SET state='PENDING_REVIEW',updated_at=now() WHERE batch_id=%s AND id=ANY(%s) AND state='APPROVED' RETURNING id",(batch_id,x.item_ids)).fetchall()
        if len(rows)!=len(set(x.item_ids)): raise HTTPException(409,'Solo se pueden devolver a revisión campos aprobados que aún no se hayan aplicado')
        c.execute("""UPDATE translation_batches b SET state=CASE
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='PENDING_REVIEW') THEN 'PENDING_REVIEW'
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='APPROVED') THEN 'APPROVED'
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='SAVED') THEN 'COMPLETED'
          ELSE 'READY_WITH_ERRORS' END,updated_at=now() WHERE b.id=%s""",(batch_id,))
    return translation_batch_payload(batch_id)

CREATE_ITEM_KEY='__create_product__'
CREATE_TRANSLATE_TYPES={'single_line_text_field','multi_line_text_field','rich_text_field','list.single_line_text_field','list.multi_line_text_field'}
CREATE_COPY_TYPES={'number_integer','number_decimal','boolean','date','date_time','dimension','weight','volume','rating','color','url','json','money','link'}
TARGET_DEFINITIONS_QUERY='query TargetDefinitions($ownerType: MetafieldOwnerType!) { metafieldDefinitions(ownerType: $ownerType, first: 250) { nodes { namespace key name type { name } validations { name value } } pageInfo { hasNextPage } } }'
SOURCE_COMMERCIAL_QUERY='query SourceCommercial($id: ID!, $after: String) { product(id: $id) { options { name position values } variants(first: 100, after: $after) { nodes { id sku price compareAtPrice } pageInfo { hasNextPage endCursor } } } }'
CREATE_PRODUCT_MUTATION='mutation CreateArchivedProduct($product: ProductCreateInput!, $media: [CreateMediaInput!]) { productCreate(product: $product, media: $media) { product { id status variants(first: 1) { nodes { id } } } userErrors { field message } } }'
CREATE_VARIANTS_MUTATION='mutation CreateVariants($productId: ID!, $variants: [ProductVariantsBulkInput!]!, $strategy: ProductVariantsBulkCreateStrategy) { productVariantsBulkCreate(productId: $productId, variants: $variants, strategy: $strategy) { productVariants { id } userErrors { field message } } }'
UPDATE_VARIANTS_MUTATION='mutation UpdateDefaultVariant($productId: ID!, $variants: [ProductVariantsBulkInput!]!) { productVariantsBulkUpdate(productId: $productId, variants: $variants) { productVariants { id } userErrors { field message } } }'
DELETE_PRODUCT_MUTATION='mutation RollbackProduct($input: ProductDeleteInput!) { productDelete(input: $input) { deletedProductId userErrors { field message } } }'

def shopify_stores_by_code(code):
    store=next((s for s in shopify_stores() if s['code']==code and s['token']),None)
    if not store: raise HTTPException(404,f'La tienda {code} no tiene dominio y token configurados')
    return store

_DEFINITIONS_CACHE={}
def metafield_definitions(store_code):
    """Definiciones de metacampos (tipo y opciones permitidas) de una tienda, en caché 5 minutos."""
    cached=_DEFINITIONS_CACHE.get(store_code)
    if cached and time.time()-cached[0]<300: return cached[1]
    store=shopify_stores_by_code(store_code); definitions={}
    for owner in ('PRODUCT','PRODUCTVARIANT'):
        try:
            for d in shopify_graphql(store['domain'],store['token'],TARGET_DEFINITIONS_QUERY,{'ownerType':owner}).get('metafieldDefinitions',{}).get('nodes',[]):
                choices=next((json.loads(v['value']) for v in d.get('validations') or [] if v.get('name')=='choices'),None)
                definitions[(owner,d['namespace'],d['key'])]={'type':(d.get('type') or {}).get('name'),'choices':choices,'name':d.get('name') or ''}
        except (HTTPException,ValueError) as exc:
            raise HTTPException(502,f'No se pudieron leer las definiciones de metacampos de {store_code}: {getattr(exc,"detail",exc)}')
    _DEFINITIONS_CACHE[store_code]=(time.time(),definitions)
    return definitions

def enforce_choices(store_code,field_key,kind,source_text,proposed,locale):
    """Si el metacampo de la tienda tiene una lista cerrada de opciones, devuelve valores válidos o lanza error claro."""
    if not field_key.startswith('metafield.'): return proposed
    parts=field_key.split('.')
    definition=metafield_definitions(store_code).get(('PRODUCT' if parts[1]=='product' else 'PRODUCTVARIANT',parts[-2],parts[-1]))
    choices=definition['choices'] if definition else None
    if not choices: return proposed
    from .worker import choose_option
    def resolve(source,candidate):
        if source in choices: return source
        if candidate in choices: return candidate
        return choose_option(source,choices,locale)
    fail=lambda: RuntimeError(f'El valor no coincide con las opciones de {store_code}: {", ".join(choices)}'[:480])
    if (kind or '').startswith('list.'):
        sources=json.loads(source_text); candidates=json.loads(proposed)
        picked=[resolve(src,candidates[i] if i<len(candidates) else '') for i,src in enumerate(sources)]
        if None in picked: raise fail()
        return json.dumps(picked,ensure_ascii=False)
    picked=resolve(source_text,proposed)
    if picked is None: raise fail()
    return picked

def build_product_creation(store_code,source_gid,locale,source_locale,allowed_skus):
    """Prepara (sin escribir nada) la creación del producto de ES en otra tienda, traducida a su idioma principal."""
    source_store=shopify_stores_by_code('ES')
    source=shopify_product(source_store,source_gid,[])
    if not source: raise HTTPException(404,'Producto no encontrado en la tienda ES')
    options=[]; commercial={}; after=None
    while True:
        data=shopify_graphql(source_store['domain'],source_store['token'],SOURCE_COMMERCIAL_QUERY,{'id':source_gid,'after':after}).get('product') or {}
        options=data.get('options') or options
        connection=data.get('variants') or {}
        commercial.update((v['id'],v) for v in connection.get('nodes',[]))
        page=connection.get('pageInfo') or {}
        if not page.get('hasNextPage') or not page.get('endCursor'): break
        after=page['endCursor']
    warnings=[]; cache={}
    same_language=locale.split('-')[0].lower()==source_locale.split('-')[0].lower()
    def tr(text,key,html=False,mf_type=None):
        # Si el idioma principal de la tienda es el de ES (p. ej. UK en español), se copia tal cual.
        if same_language or not (text or '').strip(): return text or ''
        cache_key=(text,key,html,mf_type)
        if cache_key not in cache:
            cache[cache_key]=translate_cell(CellTranslateRequest(source_text=text,source_locale=source_locale,target_store=store_code,target_locale=locale,field_key=key,metafield_type=mf_type,html=html),'local')['translation']
        return cache[cache_key]
    definitions=metafield_definitions(store_code)
    def convert_metafields(nodes,owner='PRODUCT'):
        out=[]
        for mf in nodes:
            ns,key,kind,value=mf.get('namespace') or '',mf.get('key') or '',mf.get('type') or '',mf.get('value')
            ident=f'{ns}.{key}'
            if not value or 'yotpo' in ident.casefold() or ns.startswith(('shopify--','reviews')): continue
            definition=definitions.get((owner,ns,key))
            if definition and definition['type']!=kind:
                warnings.append(f'Metacampo {ident} no copiado: en {store_code} está definido como {definition["type"]} y en ES como {kind}'); continue
            choices=definition['choices'] if definition else None
            try:
                if choices:
                    # Valores permitidos por la definición de la tienda: se usa el de ES si es válido; si no, su traducción.
                    def pick(text):
                        if text in choices: return text
                        candidate=tr(text,'metafield.'+ident)
                        if candidate in choices: return candidate
                        from .worker import choose_option
                        return choose_option(text,choices,locale)
                    if kind.startswith('list.'):
                        picked=[pick(t) for t in json.loads(value)]
                        if None in picked: warnings.append(f'Metacampo {ident}: algún valor no coincide con las opciones de {store_code} y se omitió')
                        picked=[t for t in picked if t is not None]
                        if not picked: continue
                        value=json.dumps(picked,ensure_ascii=False)
                    else:
                        value=pick(value)
                        if value is None:
                            warnings.append(f'Metacampo {ident} no copiado: el valor de ES no coincide con las opciones de {store_code}'); continue
                elif kind in CREATE_TRANSLATE_TYPES: value=tr(value,'metafield.'+ident,mf_type=kind)
                elif kind not in CREATE_COPY_TYPES:
                    warnings.append(f'Metacampo {ident} no copiado (tipo {kind or "desconocido"}: referencias o archivos son propios de cada tienda)'); continue
            except (HTTPException,RuntimeError,ValueError) as exc:
                warnings.append(f'Metacampo {ident} no copiado: {str(exc.detail if isinstance(exc,HTTPException) else exc)[:150]}'); continue
            out.append({'namespace':ns,'key':key,'type':kind,'value':value})
        return out
    wanted={s.strip().casefold() for s in allowed_skus} if allowed_skus is not None else None
    chosen=[v for v in source.get('variants',{}).get('nodes',[]) if wanted is None or (v.get('sku') or '').strip().casefold() in wanted]
    if not chosen: raise HTTPException(409,'Ninguna variante del producto coincide con los SKU de la campaña')
    default_variant=len(options)==1 and options[0]['name']=='Title' and options[0]['values']==['Default Title']
    product={'title':tr(source.get('title',''),'title'),'descriptionHtml':tr(source.get('descriptionHtml',''),'body_html',html=True),
      'handle':tr(source.get('handle',''),'handle') if source.get('handle') else '','vendor':source.get('vendor') or '',
      'productType':tr(source.get('productType',''),'product_type'),'tags':list(source.get('tags') or []),'status':'ARCHIVED',
      'seo':{'title':tr((source.get('seo') or {}).get('title') or '','seo_title'),'description':tr((source.get('seo') or {}).get('description') or '','seo_description')},
      'metafields':convert_metafields(source.get('metafields',{}).get('nodes',[]))}
    product={k:v for k,v in product.items() if v not in ('',None,[])}
    if not default_variant:
        used={}
        for v in chosen:
            for sel in v.get('selectedOptions',[]): used.setdefault(sel['name'],[]).append(sel['value'])
        product['productOptions']=[{'name':tr(o['name'],'option_name'),'values':[{'name':tr(val,'option_value')} for val in dict.fromkeys(used.get(o['name'],[]))]} for o in options]
    variants=[]
    for v in chosen:
        price=commercial.get(v['id'],{})
        item={'price':price.get('price'),'compareAtPrice':price.get('compareAtPrice'),'barcode':v.get('barcode') or None,'inventoryItem':{'sku':v.get('sku')} if v.get('sku') else None,'metafields':convert_metafields(v.get('metafields',{}).get('nodes',[]),'PRODUCTVARIANT') or None}
        if not default_variant: item['optionValues']=[{'optionName':tr(sel['name'],'option_name'),'name':tr(sel['value'],'option_value')} for sel in v.get('selectedOptions',[])]
        variants.append({k:val for k,val in item.items() if val is not None})
    media=[{'originalSource':i['url'],'alt':tr(i.get('altText') or '','image_alt'),'mediaContentType':'IMAGE'} for i in source.get('images',{}).get('nodes',[]) if i.get('url')]
    return {'kind':'create_product','store':store_code,'locale':locale,'source_product_gid':source_gid,'skus':[v['sku'] for v in chosen if v.get('sku')],
      'default_variant':default_variant,'product':product,'media':media,'variants':variants,'warnings':warnings}

def creation_summary(spec):
    product=spec['product']
    lines=[f"PRODUCTO CREADO ARCHIVADO en {spec['store']} ({spec['locale']}) — no está publicado; revísalo en Shopify",f"Título: {product.get('title','')}",f"Handle: {product.get('handle','(automático)')}",
      f"Proveedor: {product.get('vendor','')} · Tipo: {product.get('productType','')} · Etiquetas: {', '.join(product.get('tags',[]))}",
      f"SEO: {product.get('seo',{}).get('title','')} | {product.get('seo',{}).get('description','')}",
      f"Variantes ({len(spec['variants'])}): "+'; '.join(f"{(v.get('inventoryItem') or {}).get('sku','sin SKU')} · precio {v.get('price')}"+(' · '+'/'.join(o['name'] for o in v.get('optionValues',[])) if v.get('optionValues') else '') for v in spec['variants'][:15])+(' …' if len(spec['variants'])>15 else ''),
      f"Imágenes: {len(spec['media'])} · Metacampos de producto: {len(product.get('metafields',[]))}",'Precio copiado de ES sin conversión de moneda.']
    lines+=['Aviso: '+w for w in spec.get('warnings',[])]
    lines+=['','Descripción:',product.get('descriptionHtml','')]
    return '\n'.join(lines)

def _shopify_mutation(store,query,variables,root):
    result=shopify_graphql(store['domain'],store['token'],query,variables).get(root) or {}
    errors=result.get('userErrors',[])
    if errors: raise HTTPException(400,'Shopify: '+'; '.join(e.get('message','error') for e in errors)[:500])
    return result

def create_product_in_shopify(spec):
    """Crea el producto ARCHIVADO en la tienda. Devuelve datos para guardar en el item."""
    store=shopify_stores_by_code(spec['store'])
    for sku in spec['skus']:
        for found in shopify_search(store,sku,5):
            if any((v.get('sku') or '').strip().casefold()==sku.casefold() for v in found.get('variants',{}).get('nodes',[])):
                raise HTTPException(409,f"El SKU {sku} ya existe en {spec['store']} ({found['id']}): no se crea un duplicado")
    created=_shopify_mutation(store,CREATE_PRODUCT_MUTATION,{'product':{**spec['product'],'status':'ARCHIVED'},'media':spec['media'] or None},'productCreate').get('product') or {}
    gid=created.get('id')
    if not gid: raise HTTPException(502,'Shopify no devolvió el producto creado')
    try:
        if spec['default_variant']:
            default_id=created['variants']['nodes'][0]['id']
            _shopify_mutation(store,UPDATE_VARIANTS_MUTATION,{'productId':gid,'variants':[{**spec['variants'][0],'id':default_id}]},'productVariantsBulkUpdate')
        else:
            for n in range(0,len(spec['variants']),100):
                _shopify_mutation(store,CREATE_VARIANTS_MUTATION,{'productId':gid,'variants':spec['variants'][n:n+100],'strategy':'REMOVE_STANDALONE_VARIANT' if n==0 else 'DEFAULT'},'productVariantsBulkCreate')
    except HTTPException as exc:
        try: _shopify_mutation(store,DELETE_PRODUCT_MUTATION,{'input':{'id':gid}},'productDelete'); rolled='se revirtió la creación'
        except HTTPException: rolled=f'REVISA Y BORRA a mano el producto incompleto {gid}'
        raise HTTPException(502,f'Falló la creación de variantes ({exc.detail}); {rolled}')
    extra={'created_gid':gid}
    follow=spec.get('followup') or {}
    if follow.get('locales'):
        try:
            batch=create_translation_batch(TranslationBatchRequest(sku=follow['sku'],also_skus=follow['also_skus'],fields=follow['fields'],stores=[spec['store']],locales=follow['locales'],variants_of_skus_only=follow['variants_of_skus_only'],auto_apply=follow['auto_apply']),'local')
            extra['followup_batch_id']=batch['id']
        except Exception as exc: extra['followup_error']=str(exc.detail if isinstance(exc,HTTPException) else exc)[:300]
    return extra

def apply_batch_items(batch_id:int,rows,user):
    for item_id,proposed,payload in rows:
        extra={}
        try:
            if (payload or {}).get('kind')=='create_product':
                extra=create_product_in_shopify(payload)
            else:
                save_shopify_translation(CellTranslation(**{**payload,'value':proposed}),user)
            state='SAVED'; error=None
        except (HTTPException,RuntimeError,ValueError) as exc:
            state='APPLY_ERROR'; error=str(exc.detail if isinstance(exc,HTTPException) else exc)[:500]
        with pool.connection() as c:
            c.execute('UPDATE translation_batch_items SET state=%s,error=%s,edit_payload=edit_payload||%s,updated_at=now() WHERE id=%s AND batch_id=%s',(state,error,Jsonb(extra),item_id,batch_id))
    with pool.connection() as c:
        c.execute("""UPDATE translation_batches b SET state=CASE
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='PENDING_REVIEW') THEN 'PENDING_REVIEW'
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state IN ('APPROVED','APPLY_ERROR')) THEN 'APPROVED'
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='SAVED') THEN 'COMPLETED'
          ELSE 'READY_WITH_ERRORS' END,updated_at=now() WHERE b.id=%s""",(batch_id,))

@app.post('/api/translation-batches/{batch_id}/reject')
def reject_translation_batch(batch_id:int,user=Depends(operator)):
    with pool.connection() as c:
        if not c.execute('SELECT 1 FROM translation_batches WHERE id=%s',(batch_id,)).fetchone(): raise HTTPException(404,'Lote de traducción no encontrado')
        rows=c.execute("UPDATE translation_batch_items SET state='REJECTED',updated_at=now() WHERE batch_id=%s AND state IN ('PENDING_REVIEW','APPROVED','APPLY_ERROR') RETURNING id",(batch_id,)).fetchall()
        if not rows: raise HTTPException(409,'El lote no tiene campos pendientes de revisar o aplicar que rechazar')
        # Lo ya aplicado en Shopify se conserva: el lote queda completado en lugar de rechazado.
        c.execute("""UPDATE translation_batches b SET state=CASE
          WHEN EXISTS(SELECT 1 FROM translation_batch_items i WHERE i.batch_id=b.id AND i.state='SAVED') THEN 'COMPLETED'
          ELSE 'REJECTED' END,updated_at=now() WHERE b.id=%s""",(batch_id,))
    return translation_batch_payload(batch_id)

@app.post('/api/translation-batches/{batch_id}/apply')
def apply_translation_batch(batch_id:int,x:TranslationBatchItemIds|None=None,user=Depends(operator)):
    with pool.connection() as c:
        if x:
            rows=c.execute("SELECT id,proposed_text,edit_payload FROM translation_batch_items WHERE batch_id=%s AND id=ANY(%s) AND state='APPROVED' ORDER BY id",(batch_id,x.item_ids)).fetchall()
            if len(rows)!=len(set(x.item_ids)): raise HTTPException(409,'Todos los campos seleccionados deben estar aprobados antes de aplicarse')
        else:
            rows=c.execute("SELECT id,proposed_text,edit_payload FROM translation_batch_items WHERE batch_id=%s AND state='APPROVED' ORDER BY id",(batch_id,)).fetchall()
        if not rows:
            if not c.execute('SELECT 1 FROM translation_batches WHERE id=%s',(batch_id,)).fetchone(): raise HTTPException(404,'Lote de traducción no encontrado')
            raise HTTPException(409,'Aprueba al menos un campo antes de aplicarlo')
    apply_batch_items(batch_id,rows,user)
    return translation_batch_payload(batch_id)

@app.put('/api/stores')
def save_store(x:StoreConfig):
    domain=x.domain.lower().removeprefix('https://').removeprefix('http://').strip('/')
    if not domain.endswith('.myshopify.com'): raise HTTPException(400,'Usa el dominio *.myshopify.com')
    with pool.connection() as c:
        prior=c.execute("SELECT config FROM stores WHERE code=%s",(x.code,)).fetchone()
        prior_config=(prior[0] if prior else None) or {}
        token=x.token or prior_config.get('admin_token') or ''
        config={'admin_token':token} if token else {}
        # Las credenciales por entorno (renovación automática del token) se conservan al guardar la tienda.
        if prior_config.get('credentials_env'): config['credentials_env']=prior_config['credentials_env']
        c.execute('INSERT INTO stores(code,domain,config,active) VALUES(%s,%s,%s,true) ON CONFLICT(code) DO UPDATE SET domain=EXCLUDED.domain,config=EXCLUDED.config,active=true',(x.code,domain,Jsonb(config)))
        c.execute('DELETE FROM store_locales WHERE store_code=%s',(x.code,))
        for locale in dict.fromkeys(x.locales):
            c.execute("INSERT INTO store_locales(store_code,locale,strategy) VALUES(%s,%s,'BASE')",(x.code,locale))
    return {'code':x.code,'domain':domain,'active':True,'has_token':bool(token) or env_credentials(config) is not None,'locales':list(dict.fromkeys(x.locales))}

@app.post('/api/products/import')
def import_product(x:ImportProduct,user=Depends(operator)):
    raw=json.dumps(x.source,sort_keys=True,ensure_ascii=False); digest=hashlib.sha256(raw.encode()).hexdigest()
    with pool.connection() as c:
        old=c.execute('SELECT source_hash,source_version FROM global_products WHERE product_key=%s FOR UPDATE',(x.product_key,)).fetchone()
        ver=(old[1]+1 if old and old[0]!=digest else old[1] if old else 1)
        c.execute('INSERT INTO global_products(product_key,source,source_hash,source_version) VALUES(%s,%s,%s,%s) ON CONFLICT(product_key) DO UPDATE SET source=EXCLUDED.source,source_hash=EXCLUDED.source_hash,source_version=EXCLUDED.source_version,updated_at=now()', (x.product_key,Jsonb(x.source),digest,ver))
        for store,m in x.mappings.items():
            gid=m.get('shopify_product_gid'); handle=m.get('handle'); status='CONFIRMED' if gid else 'BLOCKED_MAPPING'
            c.execute('INSERT INTO store_products(product_key,store_code,shopify_product_gid,current_handle,mapping_status) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(product_key,store_code) DO UPDATE SET shopify_product_gid=EXCLUDED.shopify_product_gid,current_handle=EXCLUDED.current_handle,mapping_status=EXCLUDED.mapping_status',(x.product_key,store,gid,handle,status))
            if status=='CONFIRMED':
                c.execute("UPDATE localization_jobs SET state='PENDING',reason=NULL WHERE product_key=%s AND store_code=%s AND state='BLOCKED_MAPPING'",(x.product_key,store))
        if old and old[0]!=digest:
            c.execute("UPDATE localization_jobs SET state='STALE',reason='La fuente cambió desde la generación',updated_at=now() WHERE product_key=%s AND state IN ('READY_FOR_REVIEW','APPROVED')",(x.product_key,))
    return {'product_key':x.product_key,'source_version':ver,'source_hash':digest,'mapping_status':'manual confirmation required for unmapped destinations'}

# ---- Translation campaigns: persisted SKU lists processed by a background runner ----
CAMPAIGN_ACTIVE=('RESOLVING','RUNNING')
CAMPAIGN_OPTION_KEYS={'fields','store','locale','stores','locales','variants_of_skus_only','auto_apply','create_stores'}
class TranslationCampaignRequest(TranslationBatchRequest):
    sku:str=Field(default='',max_length=120)
    skus:list[str]=Field(min_length=1,max_length=20000)

def resolve_skus_to_groups(skus:list[str]):
    """Group SKUs by their ES product so a campaign creates one batch per product."""
    source_store=next((s for s in shopify_stores() if s['code']=='ES' and s['token']),None)
    if not source_store: raise HTTPException(503,'La tienda ES no tiene dominio y token configurados')
    groups={}; errors=[]
    for sku in dict.fromkeys(v.strip() for v in skus if v.strip()):
        try: product=resolve_es_product_by_sku(source_store,sku)
        except HTTPException as exc:
            errors.append({'sku':sku,'error':str(exc.detail)}); continue
        group=groups.setdefault(product['id'],{'product_gid':product['id'],'title':product['title'],'skus':[]})
        group['skus'].append(sku)
    return list(groups.values()),errors

def campaign_payload(campaign_id:int,items:bool=False):
    with pool.connection() as c:
        row=c.execute('SELECT id,state,options,skus,resolved,resolved_count,cancel_requested,error,created_at,updated_at,finished_at FROM translation_campaigns WHERE id=%s',(campaign_id,)).fetchone()
        if not row: raise HTTPException(404,'Campaña no encontrada')
        products={k:0 for k in ('PENDING','RUNNING','DONE','ERROR','CANCELLED')}
        for state,n in c.execute('SELECT state,count(*) FROM translation_campaign_items WHERE campaign_id=%s GROUP BY state',(campaign_id,)).fetchall(): products[state]=n
        fields={}
        for state,n in c.execute('SELECT bi.state,count(*) FROM translation_batch_items bi JOIN translation_campaign_items ci ON ci.batch_id=bi.batch_id WHERE ci.campaign_id=%s GROUP BY bi.state',(campaign_id,)).fetchall(): fields[state]=n
        current=c.execute("SELECT skus,title,started_at FROM translation_campaign_items WHERE campaign_id=%s AND state='RUNNING' ORDER BY position LIMIT 1",(campaign_id,)).fetchone()
        avg=c.execute("SELECT avg(extract(epoch FROM finished_at-started_at)) FROM translation_campaign_items WHERE campaign_id=%s AND state IN ('DONE','ERROR') AND started_at IS NOT NULL AND finished_at IS NOT NULL",(campaign_id,)).fetchone()[0]
        failed=[s for (skus,) in c.execute("SELECT skus FROM translation_campaign_items WHERE campaign_id=%s AND state='ERROR' ORDER BY position",(campaign_id,)).fetchall() for s in skus]
        rows=[]
        if items:
            rows=c.execute('''SELECT ci.id,ci.position,ci.skus,ci.title,ci.state,ci.batch_id,ci.error,
              count(bi.id) FILTER (WHERE bi.state='PENDING_REVIEW'),count(bi.id) FILTER (WHERE bi.state='APPROVED'),
              count(bi.id) FILTER (WHERE bi.state='SAVED'),count(bi.id) FILTER (WHERE bi.state IN ('ERROR','APPLY_ERROR'))
              FROM translation_campaign_items ci LEFT JOIN translation_batch_items bi ON bi.batch_id=ci.batch_id
              WHERE ci.campaign_id=%s GROUP BY ci.id ORDER BY ci.position,ci.id''',(campaign_id,)).fetchall()
    payload={'id':row[0],'state':row[1],'options':row[2],'total_skus':len(row[3]),'resolved':row[4],'resolved_count':row[5],'cancel_requested':row[6],'error':row[7],
      'created_at':row[8],'updated_at':row[9],'finished_at':row[10],'products':products,'fields':fields,
      'current':{'skus':current[0],'title':current[1],'started_at':current[2]} if current else None,
      'avg_seconds':float(avg) if avg is not None else None,'failed_skus':failed}
    if items: payload['items']=[dict(zip(('id','position','skus','title','state','batch_id','error','pending_review','approved','saved','errors'),r)) for r in rows]
    return payload

@app.post('/api/translation-campaigns')
def create_translation_campaign(x:TranslationCampaignRequest,user=Depends(operator)):
    skus=list(dict.fromkeys(s.strip() for s in x.skus if s.strip()))
    if not skus: raise HTTPException(400,'Indica al menos un SKU')
    if len(skus)>5000: raise HTTPException(400,'El máximo es 5.000 SKU por campaña')
    options=x.model_dump(include=CAMPAIGN_OPTION_KEYS)
    with pool.connection() as c:
        campaign_id=c.execute('INSERT INTO translation_campaigns(options,skus) VALUES(%s,%s) RETURNING id',(Jsonb(options),skus)).fetchone()[0]
    return campaign_payload(campaign_id)

@app.get('/api/translation-campaigns')
def list_translation_campaigns(user=Depends(operator)):
    with pool.connection() as c:
        ids=[r[0] for r in c.execute('SELECT id FROM translation_campaigns ORDER BY id DESC LIMIT 30').fetchall()]
    return [campaign_payload(i) for i in ids]

@app.get('/api/translation-campaigns/{campaign_id}')
def get_translation_campaign(campaign_id:int,user=Depends(operator)):
    return campaign_payload(campaign_id,items=True)

@app.post('/api/translation-campaigns/{campaign_id}/cancel')
def cancel_translation_campaign(campaign_id:int,user=Depends(operator)):
    with pool.connection() as c:
        updated=c.execute("UPDATE translation_campaigns SET cancel_requested=true,updated_at=now() WHERE id=%s AND state IN ('RESOLVING','RUNNING','PAUSED') RETURNING id,state",(campaign_id,)).fetchone()
        if updated and updated[1]=='PAUSED':
            # The runner skips paused campaigns, so close it here.
            c.execute("UPDATE translation_campaign_items SET state='CANCELLED',finished_at=now() WHERE campaign_id=%s AND state='PENDING'",(campaign_id,))
            c.execute("UPDATE translation_campaigns SET state='CANCELLED',finished_at=now() WHERE id=%s",(campaign_id,))
    if not updated: raise HTTPException(409,'La campaña no existe o ya terminó')
    return campaign_payload(campaign_id)

@app.post('/api/translation-campaigns/{campaign_id}/resume')
def resume_translation_campaign(campaign_id:int,user=Depends(operator)):
    with pool.connection() as c:
        updated=c.execute("UPDATE translation_campaigns SET state='RUNNING',error=NULL,updated_at=now() WHERE id=%s AND state='PAUSED' RETURNING id",(campaign_id,)).fetchone()
    if not updated: raise HTTPException(409,'Solo se puede reanudar una campaña en pausa')
    return campaign_payload(campaign_id)

@app.post('/api/translation-campaigns/{campaign_id}/retry-failed')
def retry_failed_translation_campaign(campaign_id:int,user=Depends(operator)):
    """Start a new campaign with the SKUs that failed, keeping the original as history."""
    with pool.connection() as c:
        row=c.execute('SELECT options,state FROM translation_campaigns WHERE id=%s',(campaign_id,)).fetchone()
        if not row: raise HTTPException(404,'Campaña no encontrada')
        failed=[s for (skus,) in c.execute("SELECT skus FROM translation_campaign_items WHERE campaign_id=%s AND state IN ('ERROR','CANCELLED') ORDER BY position",(campaign_id,)).fetchall() for s in skus]
        if not failed: raise HTTPException(409,'No hay SKU fallidos ni pendientes que reintentar')
        new_id=c.execute('INSERT INTO translation_campaigns(options,skus) VALUES(%s,%s) RETURNING id',(Jsonb(row[0]),list(dict.fromkeys(failed)))).fetchone()[0]
    return campaign_payload(new_id)

def _campaign_finish(campaign_id:int):
    with pool.connection() as c:
        errors=c.execute("SELECT count(*) FROM translation_campaign_items WHERE campaign_id=%s AND state='ERROR'",(campaign_id,)).fetchone()[0]
        c.execute("UPDATE translation_campaigns SET state=%s,updated_at=now(),finished_at=now() WHERE id=%s",('DONE_WITH_ERRORS' if errors else 'DONE',campaign_id))

def _campaign_resolve(campaign_id:int,skus:list[str]):
    by_product={}; errors=[]
    for i in range(0,len(skus),50):
        with pool.connection() as c:
            if c.execute('SELECT cancel_requested FROM translation_campaigns WHERE id=%s',(campaign_id,)).fetchone()[0]: return False
        chunk=skus[i:i+50]
        try: groups,errs=resolve_skus_to_groups(chunk)
        except Exception as exc:
            msg=str(exc.detail if isinstance(exc,HTTPException) else exc)[:500]
            groups,errs=[],[{'sku':s,'error':'No se pudo resolver este bloque de SKU: '+msg} for s in chunk]
        for g in groups:
            known=by_product.get(g['product_gid'])
            if known: known['skus'].extend(g['skus'])
            else: by_product[g['product_gid']]=g
        errors.extend(errs)
        with pool.connection() as c:
            c.execute('UPDATE translation_campaigns SET resolved_count=%s,updated_at=now() WHERE id=%s',(min(i+50,len(skus)),campaign_id))
    with pool.connection() as c:
        c.execute('DELETE FROM translation_campaign_items WHERE campaign_id=%s',(campaign_id,))
        position=0
        for g in by_product.values():
            c.execute('INSERT INTO translation_campaign_items(campaign_id,position,skus,product_gid,title) VALUES(%s,%s,%s,%s,%s)',(campaign_id,position,g['skus'],g['product_gid'],g['title'])); position+=1
        for e in errors:
            c.execute("INSERT INTO translation_campaign_items(campaign_id,position,skus,title,state,error,finished_at) VALUES(%s,%s,%s,'','ERROR',%s,now())",(campaign_id,position,[e['sku']],e['error'])); position+=1
        c.execute("UPDATE translation_campaigns SET resolved=true,resolved_count=%s,state='RUNNING',updated_at=now() WHERE id=%s",(len(skus),campaign_id))
    return True

def _campaign_step():
    """Do one unit of campaign work (resolve SKUs or prepare one product). False when idle."""
    with pool.connection() as c:
        row=c.execute("SELECT id,options,skus,resolved,cancel_requested FROM translation_campaigns WHERE state IN ('RESOLVING','RUNNING') ORDER BY id LIMIT 1").fetchone()
    if not row: return False
    campaign_id,options,skus,resolved,cancel=row
    if cancel:
        with pool.connection() as c:
            c.execute("UPDATE translation_campaign_items SET state='CANCELLED',finished_at=now() WHERE campaign_id=%s AND state='PENDING'",(campaign_id,))
            c.execute("UPDATE translation_campaigns SET state='CANCELLED',updated_at=now(),finished_at=now() WHERE id=%s",(campaign_id,))
        return True
    if not resolved:
        _campaign_resolve(campaign_id,skus)
        return True
    with pool.connection() as c:
        item=c.execute("""UPDATE translation_campaign_items SET state='RUNNING',started_at=now() WHERE id=(
          SELECT id FROM translation_campaign_items WHERE campaign_id=%s AND state='PENDING' ORDER BY position LIMIT 1 FOR UPDATE SKIP LOCKED)
          RETURNING id,skus""",(campaign_id,)).fetchone()
    if not item:
        _campaign_finish(campaign_id); return True
    item_id,item_skus=item
    try:
        batch=create_translation_batch(TranslationBatchRequest(**options,sku=item_skus[0],also_skus=item_skus[1:51]),'local')
        quota=next((i['error'] for i in batch['items'] if (i.get('error') or '').startswith('DEEPL_QUOTA')),None)
        if quota:
            # No DeepL quota left: put the product back in the queue and pause the whole campaign.
            with pool.connection() as c:
                c.execute("UPDATE translation_campaign_items SET state='PENDING',batch_id=%s,started_at=NULL WHERE id=%s",(batch['id'],item_id))
                c.execute("UPDATE translation_campaigns SET state='PAUSED',error=%s,updated_at=now() WHERE id=%s",('Cuota de DeepL agotada: la campaña está en pausa. Amplía el plan o espera a la renovación y pulsa Reanudar.',campaign_id))
            return True
        usable=[i for i in batch['items'] if i['state']!='ERROR']
        state='DONE' if usable else 'ERROR'
        error=None if usable else next((i['error'] for i in batch['items'] if i.get('error')),batch.get('error') or 'No se generó ningún campo')
        batch_id=batch['id']
    except Exception as exc:
        state,batch_id,error='ERROR',None,str(exc.detail if isinstance(exc,HTTPException) else exc)[:500]
        if error.startswith('DEEPL_QUOTA'):
            with pool.connection() as c:
                c.execute("UPDATE translation_campaign_items SET state='PENDING',started_at=NULL WHERE id=%s",(item_id,))
                c.execute("UPDATE translation_campaigns SET state='PAUSED',error=%s,updated_at=now() WHERE id=%s",('Cuota de DeepL agotada: la campaña está en pausa. Amplía el plan o espera a la renovación y pulsa Reanudar.',campaign_id))
            return True
    with pool.connection() as c:
        c.execute('UPDATE translation_campaign_items SET state=%s,batch_id=%s,error=%s,finished_at=now() WHERE id=%s',(state,batch_id,error,item_id))
        c.execute('UPDATE translation_campaigns SET updated_at=now() WHERE id=%s',(campaign_id,))
    return True

def _campaign_runner():
    while True:
        try: busy=_campaign_step()
        except Exception:
            logging.getLogger('hub.campaigns').exception('Error en el ejecutor de campañas'); busy=False
        if not busy: time.sleep(3)

@app.get('/translation-campaigns',response_class=HTMLResponse)
def translation_campaigns_page():
    body='''<main class="wrap"><h1>Campañas de traducción</h1><p class="lede">Las campañas se ejecutan en el servidor: puedes cerrar esta página o moverte por el hub y volver aquí para ver cómo van. Las propuestas se revisan en <a href="/translation-batches">Revisión de lotes</a>.</p><section class="card" aria-labelledby="campaign-list-title"><div class="section-heading"><h2 id="campaign-list-title">Campañas recientes</h2><a class="button" href="/products/batch">Nueva campaña</a></div><div id="campaign-list" aria-live="polite"><p class="muted">Cargando…</p></div></section><div id="campaign-detail"></div></main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const list=document.querySelector('#campaign-list'),detail=document.querySelector('#campaign-detail');
let openId=null,lastList='',lastTop='',lastItems='',itemFilter='';
async function request(url,options){const r=await fetch(url,options);let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'No se pudo completar la solicitud');return d}
const date=s=>s?new Date(s).toLocaleString('es-ES'):'—';
const STATE={RESOLVING:'Resolviendo SKU',RUNNING:'En curso',DONE:'Completada',DONE_WITH_ERRORS:'Completada con errores',CANCELLED:'Cancelada',PAUSED:'En pausa (cuota DeepL)'};
const ITEM_STATE={PENDING:'En cola',RUNNING:'Preparando…',DONE:'Preparado',ERROR:'Error',CANCELLED:'No procesado'};
const isActive=c=>c.state==='RESOLVING'||c.state==='RUNNING'||c.state==='PAUSED';
const total=c=>Object.values(c.products).reduce((a,b)=>a+b,0);
const finished=c=>c.products.DONE+c.products.ERROR+c.products.CANCELLED;
function pct(c){if(c.state==='RESOLVING')return c.total_skus?Math.round(100*c.resolved_count/c.total_skus):0;const t=total(c);return t?Math.round(100*finished(c)/t):100}
function eta(c){if(c.state!=='RUNNING'||c.avg_seconds==null)return '';const left=(c.products.PENDING+c.products.RUNNING)*c.avg_seconds;if(left<90)return 'menos de 2 min';if(left<5400)return '≈ '+Math.round(left/60)+' min';return '≈ '+(left/3600).toFixed(1)+' h'}
function bar(c){const p=pct(c);return `<progress max="100" value="${p}" aria-label="Progreso de la campaña ${c.id}" style="width:100%">${p}%</progress>`}
function headline(c){if(c.state==='RESOLVING')return `Resolviendo SKU en ES: ${c.resolved_count} de ${c.total_skus}`;const t=total(c);if(c.state==='RUNNING'){const cur=c.current?` · ahora: ${esc(c.current.skus.join(', '))}`:'';return `Producto ${Math.min(finished(c)+1,t)} de ${t}${cur}`}return `${finished(c)} de ${t} productos procesados`}
async function loadList(){try{const d=await request('/api/translation-campaigns');const html=d.length?`<div class="batch-table-scroll"><table><caption class="sr-only">Campañas recientes</caption><thead><tr><th scope="col">Campaña</th><th scope="col">Creada</th><th scope="col">SKU</th><th scope="col">Estado</th><th scope="col">Progreso</th></tr></thead><tbody>${d.map(c=>`<tr><td><a href="#${c.id}">#${c.id}</a></td><td>${date(c.created_at)}</td><td>${c.total_skus}</td><td>${STATE[c.state]||c.state}${c.cancel_requested&&isActive(c)?' (cancelando…)':''}</td><td style="min-width:140px">${bar(c)}<span class="muted">${pct(c)}%${c.products.ERROR?' · '+c.products.ERROR+' con error':''}</span></td></tr>`).join('')}</tbody></table></div>`:'<p class="muted">Todavía no hay campañas. Crea una desde Traducción en lote.</p>';if(html!==lastList){list.innerHTML=html;lastList=html}return d}catch(e){list.innerHTML=`<p class="batch-error" role="alert">${esc(e.message)}</p>`;return []}}
function topHtml(c){const f=c.fields,p=c.products,e=eta(c);return `<section class="card" aria-labelledby="cd-title"><div class="section-heading"><h2 id="cd-title" tabindex="-1">Campaña #${c.id} · ${STATE[c.state]||c.state}</h2><div class="actions" style="margin:0">${c.state==='PAUSED'?'<button type="button" data-act="resume">Reanudar</button>':''}${isActive(c)?`<button type="button" class="secondary" data-act="cancel"${c.cancel_requested?' disabled':''}>${c.cancel_requested?'Cancelando…':'Cancelar campaña'}</button>`:''}${!isActive(c)&&(p.ERROR||p.CANCELLED)?'<button type="button" class="secondary" data-act="retry">Reintentar fallidos y no procesados</button>':''}</div></div>${c.error?`<p class="batch-error" role="alert">${esc(c.error)}</p>`:''}${bar(c)}<p><strong>${pct(c)}%</strong> · ${headline(c)}${e?' · quedan '+e:''}</p><p class="muted">Creada ${date(c.created_at)}${c.finished_at?' · terminada '+date(c.finished_at):' · última actividad '+date(c.updated_at)}${c.options.auto_apply?' · <strong>aplica directamente en Shopify</strong>':''}${(c.options.create_stores||[]).length?' · crea productos archivados si faltan en '+esc(c.options.create_stores.join(', ')):''}</p><ul class="muted"><li>Productos: ${p.DONE} preparados, ${p.ERROR} con error, ${p.PENDING+p.RUNNING} en cola${p.CANCELLED?', '+p.CANCELLED+' sin procesar':''}.</li><li>Campos: ${f.PENDING_REVIEW||0} pendientes de revisión, ${f.APPROVED||0} aprobados sin aplicar, ${f.SAVED||0} subidos a Shopify, ${(f.ERROR||0)+(f.APPLY_ERROR||0)} con error.</li></ul><div class="actions"><a class="button secondary" href="/translation-batches">Ir a Revisión de lotes</a></div><p id="cd-status" class="status" role="status"></p></section>`}
function itemsHtml(c){const rows=c.items.filter(i=>!itemFilter||(itemFilter==='PROBLEMS'?(i.state==='ERROR'||i.errors>0):i.state===itemFilter));const failed=c.failed_skus.length?`<div class="card"><h3>SKU con error (${c.failed_skus.length})</h3><p class="muted">Copia esta lista para reintentarla.</p><textarea readonly rows="4" style="width:100%">${esc(c.failed_skus.join(String.fromCharCode(10)))}</textarea></div>`:'';return failed+`<div class="batch-table-scroll"><table><caption class="sr-only">Productos de la campaña</caption><thead><tr><th scope="col">#</th><th scope="col">SKU</th><th scope="col">Producto</th><th scope="col">Estado</th><th scope="col">Campos</th><th scope="col">Lote</th></tr></thead><tbody>${rows.map(i=>`<tr><td>${i.position+1}</td><td>${esc(i.skus.join(', '))}</td><td>${esc(i.title)}</td><td>${ITEM_STATE[i.state]||i.state}${i.error?`<br><span class="batch-error">${esc(i.error)}</span>`:''}</td><td>${i.batch_id?`${i.pending_review} rev. · ${i.approved} aprob. · ${i.saved} subidos${i.errors?' · '+i.errors+' error':''}`:'—'}</td><td>${i.batch_id?`<a href="/translation-batches#${i.batch_id}">#${i.batch_id}</a>`:'—'}</td></tr>`).join('')||'<tr><td colspan="6" class="muted">Sin productos para este filtro.</td></tr>'}</tbody></table></div>`}
function buildDetail(){detail.innerHTML='<div id="cd-top"></div><section class="card"><div class="section-heading"><h2>Productos</h2><label>Filtrar <select id="cd-filter"><option value="">Todos</option><option value="PROBLEMS">Con problemas</option><option value="ERROR">Con error</option><option value="RUNNING">En curso</option><option value="PENDING">En cola</option><option value="DONE">Preparados</option></select></label></div><div id="cd-items"></div></section>';detail.querySelector('#cd-filter').addEventListener('change',e=>{itemFilter=e.target.value;lastItems='';refresh()})}
async function loadDetail(){if(!openId)return null;try{const c=await request('/api/translation-campaigns/'+openId);if(!detail.querySelector('#cd-top'))buildDetail();const top=topHtml(c),items=itemsHtml(c);if(top!==lastTop){detail.querySelector('#cd-top').innerHTML=top;lastTop=top}if(items!==lastItems){detail.querySelector('#cd-items').innerHTML=items;lastItems=items}return c}catch(e){detail.innerHTML=`<section class="card"><p class="batch-error" role="alert">${esc(e.message)}</p></section>`;lastTop=lastItems='';return null}}
detail.addEventListener('click',async e=>{const btn=e.target.closest('[data-act]');if(!btn)return;const st=detail.querySelector('#cd-status');btn.disabled=true;try{if(btn.dataset.act==='cancel'){if(!confirm('¿Cancelar la campaña? Se termina el producto en curso y el resto queda sin procesar; lo ya preparado se conserva.')){btn.disabled=false;return}await request('/api/translation-campaigns/'+openId+'/cancel',{method:'POST'})}else if(btn.dataset.act==='resume'){await request('/api/translation-campaigns/'+openId+'/resume',{method:'POST'})}else{const c=await request('/api/translation-campaigns/'+openId+'/retry-failed',{method:'POST'});location.hash=c.id}refresh()}catch(err){if(st)st.textContent=err.message;btn.disabled=false}});
let busy=false;
async function refresh(){if(busy)return;busy=true;try{const [d]=await Promise.all([loadList(),loadDetail()]);return d}finally{busy=false}}
function select(){const m=location.hash.match(/^#([0-9]+)$/);const id=m?Number(m[1]):null;if(id!==openId){openId=id;lastTop=lastItems='';detail.innerHTML='';itemFilter=''}refresh().then(()=>{if(openId)detail.querySelector('#cd-title')?.focus({preventScroll:false})})}
window.addEventListener('hashchange',select);
setInterval(()=>{if(!document.hidden)refresh()},3000);
select();
</script>'''
    return shell('Campañas de traducción',body,'campaigns')

@app.post('/api/jobs/enqueue')
def enqueue(x:Enqueue,user=Depends(operator)):
    with pool.connection() as c:
        p=c.execute('SELECT source_version FROM global_products WHERE product_key=%s',(x.product_key,)).fetchone()
        if not p: raise HTTPException(404,'Producto no registrado')
        added=[]
        for dest in x.destinations:
            bits=dest.split(':',1); store=bits[0]; requested_locale=bits[1] if len(bits)>1 else None
            locale=c.execute('SELECT locale FROM store_locales WHERE store_code=%s AND (%s::text IS NULL OR locale=%s) ORDER BY strategy LIMIT 1',(store,requested_locale,requested_locale)).fetchone()
            if not locale: raise HTTPException(400,f'Destino desconocido: {dest}')
            mp=c.execute('SELECT mapping_status FROM store_products WHERE product_key=%s AND store_code=%s',(x.product_key,store)).fetchone()
            state='PENDING' if store=='ES' or (mp and mp[0]=='CONFIRMED') else 'BLOCKED_MAPPING'
            row=c.execute('INSERT INTO localization_jobs(product_key,store_code,locale,source_version,state,reason) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(product_key,store_code,locale,source_version) DO NOTHING RETURNING id,state',(x.product_key,store,locale[0],p[0],state,None if state=='PENDING' else 'Falta correspondencia aprobada del producto destino')).fetchone()
            if row: added.append({'id':row[0],'state':row[1]})
    return {'created':added}

@app.get('/api/jobs')
def jobs(user=Depends(operator)):
    with pool.connection() as c: rows=c.execute('SELECT id,product_key,store_code,locale,source_version,state,reason FROM localization_jobs ORDER BY id').fetchall()
    return [dict(zip(('id','product_key','store','locale','source_version','state','reason'),r)) for r in rows]

def set_running(user):
    with pool.connection() as c:
        row=c.execute('SELECT state FROM process_control FOR UPDATE').fetchone()
        if row[0]=='RUNNING': raise HTTPException(409,'El worker ya está activo')
        c.execute("UPDATE process_control SET state='RUNNING',last_incident=NULL,resumed_by=%s,updated_at=now() WHERE singleton",(user,))
    return {'state':'RUNNING'}
@app.post('/api/process/start')
def start(user=Depends(operator)): return set_running(user)
@app.post('/api/process/continue')
def resume(user=Depends(operator)): return set_running(user)
@app.post('/api/process/stop')
def stop(user=Depends(operator)):
    with pool.connection() as c: c.execute("UPDATE process_control SET state='STOPPED',resumed_by=%s,updated_at=now() WHERE singleton",(user,))
    return {'state':'STOPPED'}
@app.get('/api/process')
def process(user=Depends(operator)):
    with pool.connection() as c: r=c.execute('SELECT state,last_incident,resumed_by,updated_at FROM process_control').fetchone()
    return dict(zip(('state','last_incident','resumed_by','updated_at'),r))

@app.get('/api/jobs/{job_id}/results')
def results(job_id:int,user=Depends(operator)):
    with pool.connection() as c: rs=c.execute('SELECT id,output,validation,approved_fields,approved_by,created_at FROM localization_results WHERE job_id=%s ORDER BY id',(job_id,)).fetchall()
    return [dict(zip(('id','output','validation','approved_fields','approved_by','created_at'),r)) for r in rs]
@app.get('/api/products/{product_key}')
def product_detail(product_key:str,user=Depends(operator)):
    with pool.connection() as c:
        p=c.execute('SELECT product_key,source,source_hash,source_version,updated_at FROM global_products WHERE product_key=%s',(product_key,)).fetchone()
        if not p: raise HTTPException(404,'Producto no registrado')
        mappings=c.execute('SELECT store_code,shopify_product_gid,current_handle,mapping_status FROM store_products WHERE product_key=%s',(product_key,)).fetchall()
    return {'product_key':p[0],'source':p[1],'source_hash':p[2],'source_version':p[3],'updated_at':p[4],'mappings':[dict(zip(('store','shopify_product_gid','handle','status'),r)) for r in mappings]}
@app.get('/api/jobs/{job_id}/publications')
def publications(job_id:int,user=Depends(operator)):
    with pool.connection() as c: rows=c.execute('SELECT id,fields,outcomes,shopify_gid,simulated,verified_at,created_at FROM publication_records WHERE job_id=%s ORDER BY id',(job_id,)).fetchall()
    return [dict(zip(('id','fields','outcomes','shopify_gid','simulated','verified_at','created_at'),r)) for r in rows]
@app.post('/api/jobs/{job_id}/approve')
def approve(job_id:int,x:Fields,user=Depends(operator)):
    with pool.connection() as c:
        row=c.execute("SELECT r.id,r.output,j.source_version,p.source_version FROM localization_results r JOIN localization_jobs j ON j.id=r.job_id JOIN global_products p ON p.product_key=j.product_key WHERE j.id=%s AND j.state='READY_FOR_REVIEW' ORDER BY r.id DESC LIMIT 1",(job_id,)).fetchone()
        if not row: raise HTTPException(409,'No hay resultado listo para revisar')
        if row[2]!=row[3]: raise HTTPException(409,'Propuesta obsoleta: cambió el origen')
        unknown=set(x.fields)-set(row[1] or {})
        if unknown: raise HTTPException(400,f'Campos no disponibles: {sorted(unknown)}')
        approved={k:row[1][k] for k in x.fields}
        c.execute('UPDATE localization_results SET approved_fields=%s,approved_by=%s WHERE id=%s',(Jsonb(approved),user,row[0]))
        c.execute("UPDATE localization_jobs SET state='APPROVED',updated_at=now() WHERE id=%s",(job_id,))
    return {'job_id':job_id,'approved_fields':approved,'publication':'requires explicit publish call; simulation only'}
@app.post('/api/jobs/{job_id}/publish')
def publish(job_id:int,user=Depends(operator)):
    if os.getenv('SIMULATION','true').lower()!='true': raise HTTPException(409,'Publicación Shopify deshabilitada en este piloto')
    with pool.connection() as c:
        row=c.execute("SELECT r.id,r.approved_fields,j.state,sp.shopify_product_gid FROM localization_results r JOIN localization_jobs j ON j.id=r.job_id LEFT JOIN store_products sp ON sp.product_key=j.product_key AND sp.store_code=j.store_code WHERE j.id=%s ORDER BY r.id DESC LIMIT 1",(job_id,)).fetchone()
        if not row or row[2]!='APPROVED' or not row[1]: raise HTTPException(409,'Aprueba campos concretos antes de simular publicación')
        c.execute('INSERT INTO publication_records(job_id,result_id,fields,shopify_gid,simulated,outcomes) VALUES(%s,%s,%s,%s,true,%s)',(job_id,row[0],Jsonb(row[1]),row[3],Jsonb({k:'SIMULATED_NO_WRITE' for k in row[1]})))
        # A simulated record is not a publication; keep the job approved.
    return {'job_id':job_id,'simulated':True,'shopify_writes':0}

# ---- Control de stock multitienda ----
STOCK_VARIANT_QUERY='''query StockBySku($query:String!) { productVariants(first:25,query:$query) { nodes { id sku title product { id title status } inventoryItem { id tracked inventoryLevels(first:25) { nodes { location { id name } quantities(names:["available"]) { name quantity } } } } } } }'''

class StockApply(BaseModel):
    sku:str=Field(min_length=1,max_length=120)
    stores:list[str]=Field(min_length=1)
    # Artículos de inventario concretos por tienda; si una tienda no aparece, se aplica a todas sus variantes con ese SKU.
    items:dict[str,list[str]]=Field(default_factory=dict)
    track:bool
    quantity:int|None=Field(default=None,ge=0,le=1_000_000)
    # inventory_item_id -> location_id (solo si el artículo tiene varias ubicaciones).
    locations:dict[str,str]=Field(default_factory=dict)
    restock_at:str|None=Field(default=None,pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$')
    restock_quantity:int|None=Field(default=None,ge=0,le=1_000_000)
    restock_track:bool=True

def stock_lookup_store(store, sku):
    """Variantes con ese SKU exacto en una tienda (puede haber varias) y su estado de stock."""
    safe=sku.replace('\\','\\\\').replace('"','\\"')
    nodes=shopify_graphql(store['domain'],store['token'],STOCK_VARIANT_QUERY,{'query':f'sku:"{safe}"'}).get('productVariants',{}).get('nodes',[])
    variants=[]
    for v in nodes:
        if (v.get('sku') or '').strip().casefold()!=sku.casefold(): continue
        item=v.get('inventoryItem') or {}
        if not item.get('id'): continue
        levels=[]
        for level in (item.get('inventoryLevels') or {}).get('nodes',[]):
            available=next((q['quantity'] for q in level.get('quantities') or [] if q.get('name')=='available'),None)
            levels.append({'location_id':level['location']['id'],'location':level['location']['name'],'available':available})
        product=v.get('product') or {}
        variants.append({'product_id':product.get('id'),'product':product.get('title'),'status':product.get('status'),'variant':v.get('title'),'inventory_item_id':item['id'],'tracked':bool(item.get('tracked')),'levels':levels})
    return {'found':bool(variants),'variants':variants}

def stock_mutation(store, query, variables, root):
    data=shopify_graphql(store['domain'],store['token'],query,variables).get(root) or {}
    errors=data.get('userErrors') or []
    if errors: raise HTTPException(502,'Shopify: '+'; '.join(e.get('message','error') for e in errors)[:300])
    return data

def stock_apply_variant(store, variant, track, quantity, location_id=None):
    item_id=variant['inventory_item_id']
    if variant['tracked']!=track:
        stock_mutation(store,'mutation($id:ID!,$input:InventoryItemInput!) { inventoryItemUpdate(id:$id,input:$input) { inventoryItem { id tracked } userErrors { field message } } }',{'id':item_id,'input':{'tracked':track}},'inventoryItemUpdate')
    if track and quantity is not None:
        level=next((l for l in variant['levels'] if l['location_id']==location_id),None) if location_id else None
        level=level or (variant['levels'][0] if variant['levels'] else None)
        if not level: raise HTTPException(409,'El SKU no tiene ubicación de inventario en esta tienda')
        stock_mutation(store,'mutation($input:InventorySetQuantitiesInput!) { inventorySetQuantities(input:$input) @idempotent(key:"'+str(uuid.uuid4())+'") { inventoryAdjustmentGroup { id } userErrors { field message } } }',{'input':{'name':'available','reason':'correction','quantities':[{'inventoryItemId':item_id,'locationId':level['location_id'],'quantity':quantity,'changeFromQuantity':None}]}},'inventorySetQuantities')

def stock_apply_store(store, sku, track, quantity, locations=None, item_ids=None):
    """Aplica seguimiento/cantidad a las variantes con ese SKU (todas, o solo `item_ids`).
    Devuelve (ids aplicados, errores por artículo)."""
    state=stock_lookup_store(store,sku)
    variants=[v for v in state['variants'] if not item_ids or v['inventory_item_id'] in item_ids]
    if not variants: raise HTTPException(404,'SKU no encontrado en la tienda')
    done=[]; errors=[]
    for v in variants:
        try:
            stock_apply_variant(store,v,track,quantity,(locations or {}).get(v['inventory_item_id']))
            done.append(v['inventory_item_id'])
        except HTTPException as exc: errors.append(f"{v['product']}: {exc.detail}")
    return done,errors

@app.get('/api/stock/lookup')
def stock_lookup(sku:str):
    sku=sku.strip()
    if not sku: raise HTTPException(400,'Indica un SKU')
    stores=shopify_stores()
    def one(store):
        if not store['domain'] or not store['token']: return {'store':store['code'],'found':False,'variants':[],'error':'Sin credenciales Shopify'}
        try: return {'store':store['code'],'domain':store['domain'],**stock_lookup_store(store,sku)}
        except HTTPException as exc: return {'store':store['code'],'found':False,'variants':[],'error':str(exc.detail)}
    with ThreadPoolExecutor(max_workers=6) as pool_:
        results=list(pool_.map(one,stores))
    return {'sku':sku,'stores':results,'schedules':stock_schedules_for(sku)}

def stock_schedules_for(sku):
    with pool.connection() as c:
        rows=c.execute("SELECT id,store_code,quantity,to_char(run_at AT TIME ZONE 'Europe/Madrid','YYYY-MM-DD HH24:MI'),state,error,track,inventory_item_id FROM stock_schedules WHERE lower(sku)=lower(%s) AND (state IN ('PENDING','RUNNING') OR (state='FAILED' AND created_at>now()-interval '7 days')) ORDER BY run_at,id",(sku,)).fetchall()
    return [{'id':r[0],'store':r[1],'quantity':r[2],'run_at':r[3],'state':r[4],'error':r[5],'track':r[6],'inventory_item_id':r[7]} for r in rows]

@app.post('/api/stock/apply')
def stock_apply(x:StockApply,user=Depends(operator)):
    sku=x.sku.strip()
    if x.restock_at and x.restock_track and x.restock_quantity is None: raise HTTPException(400,'Indica la cantidad del reaprovisionamiento')
    if x.restock_quantity is not None and not x.restock_at: raise HTTPException(400,'Indica la fecha del reaprovisionamiento')
    codes=list(dict.fromkeys(c.strip().upper() for c in x.stores if c.strip()))
    by_code={s['code']:s for s in shopify_stores()}
    unknown=[c for c in codes if c not in by_code or not by_code[c]['domain'] or not by_code[c]['token']]
    if unknown: raise HTTPException(404,'Tienda inactiva o sin credenciales: '+', '.join(unknown))
    if x.restock_at:
        with pool.connection() as c:
            if not c.execute("SELECT (%s::timestamp AT TIME ZONE 'Europe/Madrid')>now()",(x.restock_at,)).fetchone()[0]: raise HTTPException(400,'La fecha de reaprovisionamiento debe ser futura')
    def one(code):
        try: done,errors=stock_apply_store(by_code[code],sku,x.track,x.quantity,x.locations,x.items.get(code))
        except HTTPException as exc: return {'store':code,'ok':False,'applied':0,'error':str(exc.detail)}
        if x.restock_at and done:
            with pool.connection() as c:
                for item_id in done:
                    c.execute("UPDATE stock_schedules SET state='CANCELLED' WHERE lower(sku)=lower(%s) AND store_code=%s AND state='PENDING' AND (inventory_item_id=%s OR inventory_item_id IS NULL)",(sku,code,item_id))
                    c.execute("INSERT INTO stock_schedules(sku,store_code,inventory_item_id,location_id,quantity,track,run_at) VALUES(%s,%s,%s,%s,%s,%s,%s::timestamp AT TIME ZONE 'Europe/Madrid')",(sku,code,item_id,x.locations.get(item_id),x.restock_quantity if x.restock_track else None,x.restock_track,x.restock_at))
        return {'store':code,'ok':not errors,'applied':len(done),'error':'; '.join(errors) or None}
    with ThreadPoolExecutor(max_workers=6) as pool_:
        results=list(pool_.map(one,codes))
    logging.getLogger('hub.stock').info('Stock %s: seguimiento=%s cantidad=%s reaprovisionamiento=%s en %s',sku,x.track,x.quantity,x.restock_at,codes)
    return {'sku':sku,'results':results,'schedules':stock_schedules_for(sku)}

@app.post('/api/stock/schedules/{schedule_id}/cancel')
def stock_cancel_schedule(schedule_id:int,user=Depends(operator)):
    with pool.connection() as c:
        row=c.execute("UPDATE stock_schedules SET state='CANCELLED' WHERE id=%s AND state IN ('PENDING','FAILED') RETURNING id",(schedule_id,)).fetchone()
    if not row: raise HTTPException(404,'Reaprovisionamiento no encontrado o ya ejecutado')
    return {'id':schedule_id,'state':'CANCELLED'}

def _stock_step():
    with pool.connection() as c:
        row=c.execute("UPDATE stock_schedules SET state='RUNNING' WHERE id=(SELECT id FROM stock_schedules WHERE state='PENDING' AND run_at<=now() ORDER BY run_at,id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id,sku,store_code,inventory_item_id,location_id,quantity,track").fetchone()
    if not row: return False
    schedule_id,sku,code,item_id,location_id,quantity,track=row
    try:
        store=shopify_store_by_code(code)
        if not store or not store['domain'] or not store['token']: raise HTTPException(503,'Tienda inactiva o sin credenciales: '+code)
        done,errors=stock_apply_store(store,sku,track,quantity,{item_id:location_id} if item_id and location_id else None,[item_id] if item_id else None)
        if errors: raise HTTPException(502,'; '.join(errors))
        state,error='DONE',None
    except Exception as exc:
        state,error='FAILED',str(getattr(exc,'detail',None) or exc)[:500]
        logging.getLogger('hub.stock').warning('Reaprovisionamiento %s (%s en %s) falló: %s',schedule_id,sku,code,error)
    with pool.connection() as c:
        c.execute("UPDATE stock_schedules SET state=%s,error=%s,applied_at=now() WHERE id=%s",(state,error,schedule_id))
    return True

def _stock_runner():
    while True:
        try: busy=_stock_step()
        except Exception:
            logging.getLogger('hub.stock').exception('Error en el ejecutor de reaprovisionamientos'); busy=False
        if not busy: time.sleep(30)

@app.get('/stock',response_class=HTMLResponse)
def stock_page():
    body='''<main class="wrap"><h1>Control de stock multitienda</h1><p class="lede">Busca un SKU y verás en qué tiendas existe (si varias variantes comparten SKU, salen todas, cada una con su casilla), si tiene el seguimiento de inventario activado y cuánto stock hay. Elige las tiendas a las que quieres aplicar el cambio (a menudo solo Europa). Los cambios se escriben directamente en Shopify. Si indicas una fecha de reaprovisionamiento, ese día el hub fijará la cantidad (activando el seguimiento) o devolverá el SKU a sin seguimiento, a tu elección (hora de España peninsular).</p>
<section class="card"><form id="stock-search" class="field"><label for="sku">SKU exacto</label><div style="display:flex;gap:.5rem;flex-wrap:wrap"><input id="sku" required maxlength="120" autocomplete="off" placeholder="NG-1234" style="flex:1;min-width:14rem"><button class="button primary" id="search-button">Buscar</button></div></form><p class="muted" id="stock-status" role="status" aria-live="polite"></p></section>
<section class="card" id="stock-result" hidden><h2 id="result-title"></h2><div class="actions" style="display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.75rem"><button type="button" class="button" id="pick-all">Todas</button><button type="button" class="button" id="pick-eu">Solo Europa</button><button type="button" class="button" id="pick-none">Ninguna</button></div><div class="table-wrap"><table><thead><tr><th></th><th>Tienda</th><th>Producto / variante</th><th>Seguimiento</th><th>Stock</th><th>Ubicación</th></tr></thead><tbody id="stock-rows"></tbody></table></div>
<form id="stock-form"><div class="grid"><fieldset class="field"><legend>Seguimiento de stock</legend><label style="display:block"><input type="radio" name="track" value="on" checked style="width:auto"> Activar</label><label style="display:block"><input type="radio" name="track" value="off" style="width:auto"> Desactivar</label></fieldset><div class="field"><label for="qty">Cantidad ahora (opcional, solo si se activa)</label><input id="qty" type="number" min="0" max="1000000" step="1" placeholder="Dejar como está"></div></div>
<fieldset class="field"><legend>Reaprovisionamiento programado (opcional)</legend><div class="grid"><div class="field"><label for="restock-at">Fecha y hora</label><input id="restock-at" type="datetime-local"></div><div class="field"><label style="display:block"><input type="radio" name="restock-mode" value="qty" checked style="width:auto"> Fijar cantidad (activa el seguimiento)</label><label style="display:block"><input type="radio" name="restock-mode" value="untrack" style="width:auto"> Volver a sin seguimiento (reaprovisionamiento definitivo)</label><label for="restock-qty">Cantidad que se fijará ese día</label><input id="restock-qty" type="number" min="0" max="1000000" step="1"></div></div><p class="muted">Sustituye cualquier reaprovisionamiento pendiente de este SKU en las tiendas elegidas.</p></fieldset>
<button class="button primary" id="apply-button">Aplicar</button> <span class="muted" id="apply-status" role="status" aria-live="polite"></span></form></section>
<section class="card" id="schedules-card" hidden><h2>Reaprovisionamientos de este SKU</h2><div class="table-wrap"><table><thead><tr><th>Tienda</th><th>Fecha</th><th>Cantidad</th><th>Estado</th><th></th></tr></thead><tbody id="schedule-rows"></tbody></table></div></section>
</main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function fetchJSON(url,options){const r=await fetch(url,options);let data={};try{data=await r.json()}catch{}if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'Error al procesar la solicitud');return data}
const EUROPE=['EU','ES','FR','DE','IT','B2B'];const rows=document.querySelector('#stock-rows'),statusBox=document.querySelector('#stock-status'),applyStatus=document.querySelector('#apply-status');let current=null;
function stateBadge(v){return v.tracked?'<span class="badge">ACTIVO</span>':'<span class="badge">DESACTIVADO</span>'}
function statusBadge(v){return v.status&&v.status!=='ACTIVE'?' <span class="badge">'+esc(v.status)+'</span>':''}
function stockText(v){if(!v.tracked)return '<span class="muted">sin seguimiento</span>'+(v.levels.length?' · '+v.levels.map(l=>esc(l.available??'—')).join(' / '):'');return v.levels.length?v.levels.map(l=>esc(l.available??'—')).join(' / '):'—'}
function locationCell(v){if(v.levels.length<=1)return esc(v.levels[0]?.location||'—');return '<select class="loc" data-item="'+esc(v.inventory_item_id)+'" aria-label="Ubicación">'+v.levels.map(l=>'<option value="'+esc(l.location_id)+'">'+esc(l.location)+' ('+esc(l.available??'—')+')</option>').join('')+'</select>'}
function leafRow(s,v,nested,hideProduct){const pad=nested?' style="padding-left:1.5rem"':'';return '<tr><td><input type="checkbox" class="pick" data-store="'+esc(s.store)+'" data-product="'+esc(v.product_id)+'" value="'+esc(v.inventory_item_id)+'" style="width:auto" aria-label="Aplicar a '+esc(s.store)+' · '+esc(v.product)+(v.variant&&v.variant!=='Default Title'?' · '+esc(v.variant):'')+'"'+(checkedSet.has(v.inventory_item_id)?' checked':'')+'></td><td>'+(nested?'':'<strong>'+esc(s.store)+'</strong>')+'</td><td'+pad+'>'+(hideProduct?'':esc(v.product)+statusBadge(v))+(v.variant&&v.variant!=='Default Title'?' <span class="muted">'+(hideProduct?'':'· ')+esc(v.variant)+'</span>':'')+'</td><td>'+stateBadge(v)+'</td><td>'+stockText(v)+'</td><td>'+locationCell(v)+'</td></tr>'}
function groupRow(kind,s,label,count,productId,nested){return '<tr class="group-row"><td><input type="checkbox" class="grp" data-kind="'+kind+'" data-store="'+esc(s.store)+'"'+(productId?' data-product="'+esc(productId)+'"':'')+' style="width:auto" aria-label="Seleccionar todas las variantes de '+esc(label)+' ('+count+')"></td><td colspan="5"'+(nested?' style="padding-left:1.5rem"':'')+'><strong>'+esc(kind==='store'?s.store:label)+'</strong> <span class="muted">· '+count+' variantes'+(kind==='store'?'':'')+'</span></td></tr>'}
let checkedSet=new Set();
function storeRows(s){if(!s.found)return '<tr><td><input type="checkbox" disabled style="width:auto" aria-label="'+esc(s.store)+' no disponible"></td><td><strong>'+esc(s.store)+'</strong></td><td colspan="4" class="muted">'+esc(s.error||'No existe')+'</td></tr>';if(s.variants.length===1)return leafRow(s,s.variants[0],false,false);const groups=new Map();for(const v of s.variants){const k=v.product_id||v.product;(groups.get(k)||groups.set(k,[]).get(k)).push(v)}let out=groupRow('store',s,s.store,s.variants.length,null,false);for(const [pid,list] of groups){if(list.length>1){out+=groupRow('product',s,list[0].product+(list[0].status&&list[0].status!=='ACTIVE'?' ('+list[0].status+')':''),list.length,list[0].product_id,true);for(const v of list)out+=leafRow(s,v,true,true).replace('padding-left:1.5rem','padding-left:3rem')}else out+=leafRow(s,list[0],true,false)}return out}
function syncGroups(){document.querySelectorAll('.grp').forEach(g=>{const leaves=[...document.querySelectorAll('.pick')].filter(x=>x.dataset.store===g.dataset.store&&(g.dataset.kind==='store'||x.dataset.product===g.dataset.product));const on=leaves.filter(x=>x.checked).length;g.checked=on===leaves.length&&on>0;g.indeterminate=on>0&&on<leaves.length})}
function render(data,keepChecked){current=data;checkedSet=new Set(keepChecked||[]);document.querySelector('#result-title').textContent='SKU '+data.sku;rows.innerHTML=data.stores.map(storeRows).join('');document.querySelector('#stock-result').hidden=false;syncGroups();renderSchedules(data.schedules)}
rows.addEventListener('change',e=>{const t=e.target;if(t.classList.contains('grp')){document.querySelectorAll('.pick').forEach(x=>{if(x.dataset.store===t.dataset.store&&(t.dataset.kind==='store'||x.dataset.product===t.dataset.product))x.checked=t.checked})}syncGroups()});
function itemName(id){for(const s of (current?.stores||[]))for(const v of s.variants||[])if(v.inventory_item_id===id)return v.product+(v.status&&v.status!=='ACTIVE'?' ('+v.status+')':'');return ''}
function renderSchedules(list){document.querySelector('#schedules-card').hidden=!list.length;document.querySelector('#schedule-rows').innerHTML=list.map(x=>'<tr><td>'+esc(x.store)+(itemName(x.inventory_item_id)?' <span class="muted">· '+esc(itemName(x.inventory_item_id))+'</span>':'')+'</td><td>'+esc(x.run_at)+'</td><td>'+(x.track===false?'Sin seguimiento':esc(x.quantity))+'</td><td>'+(x.state==='FAILED'?'<span class="badge">ERROR</span> '+esc(x.error||''):'<span class="badge">'+(x.state==='RUNNING'?'EN CURSO':'PENDIENTE')+'</span>')+'</td><td><button type="button" class="button cancel-schedule" data-id="'+x.id+'">'+(x.state==='FAILED'?'Descartar':'Cancelar')+'</button></td></tr>').join('')}
async function search(sku,keep){statusBox.textContent='Consultando todas las tiendas…';document.querySelector('#search-button').disabled=true;try{render(await fetchJSON('/api/stock/lookup?sku='+encodeURIComponent(sku)),keep);const n=current.stores.filter(s=>s.found).length,nv=current.stores.reduce((a,s)=>a+s.variants.length,0);statusBox.textContent=n?'Encontrado en '+n+' de '+current.stores.length+' tiendas ('+nv+' variante'+(nv===1?'':'s')+').':'No se encontró este SKU en ninguna tienda.'}catch(e){statusBox.textContent=e.message;document.querySelector('#stock-result').hidden=true}document.querySelector('#search-button').disabled=false}
document.querySelector('#stock-search').addEventListener('submit',e=>{e.preventDefault();search(document.querySelector('#sku').value.trim())});
const pick=fn=>{document.querySelectorAll('.pick').forEach(x=>{x.checked=fn(x.dataset.store)});syncGroups()};
document.querySelector('#pick-all').addEventListener('click',()=>pick(()=>true));document.querySelector('#pick-eu').addEventListener('click',()=>pick(s=>EUROPE.includes(s)));document.querySelector('#pick-none').addEventListener('click',()=>pick(()=>false));
document.querySelectorAll('input[name=restock-mode]').forEach(x=>x.addEventListener('change',()=>{const u=document.querySelector('input[name=restock-mode]:checked').value==='untrack';document.querySelector('#restock-qty').disabled=u;if(u)document.querySelector('#restock-qty').value=''}));
document.querySelector('#schedules-card').addEventListener('click',async e=>{const b=e.target.closest('.cancel-schedule');if(!b)return;b.disabled=true;try{await fetchJSON('/api/stock/schedules/'+b.dataset.id+'/cancel',{method:'POST'});await search(current.sku,[...document.querySelectorAll('.pick:checked')].map(x=>x.value))}catch(err){applyStatus.textContent=err.message;b.disabled=false}});
document.querySelector('#stock-form').addEventListener('submit',async e=>{e.preventDefault();const picked=[...document.querySelectorAll('.pick:checked')],items={};for(const x of picked)(items[x.dataset.store]??=[]).push(x.value);const stores=Object.keys(items);if(!stores.length){applyStatus.textContent='Elige al menos una tienda.';return}
const track=document.querySelector('input[name=track]:checked').value==='on',qty=document.querySelector('#qty').value,at=document.querySelector('#restock-at').value,untrack=document.querySelector('input[name=restock-mode]:checked').value==='untrack',rq=untrack?'':document.querySelector('#restock-qty').value;if(at===''&&document.querySelector('#restock-qty').value!==''){applyStatus.textContent='Indica la fecha del reaprovisionamiento.';return}if(at!==''&&!untrack&&rq===''){applyStatus.textContent='El reaprovisionamiento necesita una cantidad.';return}
const locations={};document.querySelectorAll('.loc').forEach(x=>{locations[x.dataset.item]=x.value});const body={sku:current.sku,stores,items,track,locations};if(track&&qty!=='')body.quantity=Number(qty);if(at){body.restock_at=at;body.restock_track=!untrack;if(!untrack)body.restock_quantity=Number(rq)}
const button=document.querySelector('#apply-button');button.disabled=true;applyStatus.textContent='Aplicando en '+picked.length+' variante(s) de '+stores.length+' tienda(s)…';try{const data=await fetchJSON('/api/stock/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const bad=data.results.filter(r=>!r.ok);await search(current.sku,picked.map(x=>x.value));applyStatus.textContent=(data.results.length-bad.length)+' tienda(s) actualizadas'+(bad.length?'. Errores: '+bad.map(r=>r.store+': '+r.error).join(' · '):'.')+(body.restock_at?' Reaprovisionamiento programado.':'')}catch(err){applyStatus.textContent=err.message}button.disabled=false});
</script>'''
    return shell('Control de stock',body,'stock')


# ---- Etiquetado masivo: buscar productos y añadir/quitar etiquetas ----
TAG_SEARCH_QUERY='''query TagSearch($query:String,$after:String) { productsCount(query:$query) { count precision } products(first:100,query:$query,after:$after) { pageInfo { hasNextPage endCursor } nodes { id title handle status productType category { name } tags variants(first:5) { nodes { sku } } } } }'''

class TagApply(BaseModel):
    store:str=Field(pattern=r'^[A-Z0-9_-]{2,12}$')
    product_ids:list[str]=Field(min_length=1,max_length=500)
    add:list[str]=Field(default_factory=list,max_length=50)
    remove:list[str]=Field(default_factory=list,max_length=50)
    # Otras tiendas donde aplicar lo mismo: los productos se enlazan por SKU exacto.
    stores:list[str]=Field(default_factory=list,max_length=20)
    skus:list[str]=Field(default_factory=list,max_length=500)

    @field_validator('product_ids')
    @classmethod
    def check_ids(cls,value):
        if any(not re.fullmatch(r'gid://shopify/Product/\d+',v) for v in value): raise ValueError('Identificador de producto no válido')
        return list(dict.fromkeys(value))

    @field_validator('add','remove')
    @classmethod
    def check_tags(cls,value):
        tags=list(dict.fromkeys(t.strip() for t in value if t.strip()))
        if any(len(t)>255 or ',' in t for t in tags): raise ValueError('Etiqueta no válida (máx. 255 caracteres y sin comas)')
        return tags

def _tag_term(field,value):
    safe=value.strip().replace('\\','\\\\').replace('"','\\"')
    return f'{field}:"{safe}"'

TAG_COLLECTIONS_QUERY='''query TagCollections($query:String) { collections(first:25,query:$query,sortKey:TITLE) { nodes { id title handle } } }'''

@app.get('/api/tags/collections')
def tags_collections(store:str,q:str=''):
    st=shopify_store_by_code(store.strip().upper())
    if not st or not st['domain'] or not st['token']: raise HTTPException(404,'Tienda inactiva o sin credenciales')
    words=re.findall(r'[\w-]+',q)
    query=' '.join(f'title:{w}*' for w in words) or None
    nodes=shopify_graphql(st['domain'],st['token'],TAG_COLLECTIONS_QUERY,{'query':query}).get('collections',{}).get('nodes',[])
    return {'collections':[{'id':n['id'].rsplit('/',1)[-1],'title':n['title'],'handle':n['handle']} for n in nodes]}

@app.get('/api/tags/search')
def tags_search(store:str,sku:str='',collection_id:str='',tag:str='',title:str='',title_mode:str='any',status:str='',after:str|None=None):
    terms=[]
    # Varios SKU (separados por comas, espacios o saltos de línea): coincide cualquiera de ellos.
    skus=list(dict.fromkeys(re.split(r'[\s,;]+',sku.strip()))) if sku.strip() else []
    if len(skus)>100: raise HTTPException(400,'Máximo 100 SKU por búsqueda')
    if skus: terms.append('('+' OR '.join(_tag_term('sku',v) for v in skus)+')')
    if tag.strip(): terms.append(_tag_term('tag',tag))
    if status.strip():
        if status.strip().upper() not in ('ACTIVE','DRAFT','ARCHIVED'): raise HTTPException(400,'Estado no válido')
        terms.append('status:'+status.strip().lower())
    if collection_id.strip():
        if not collection_id.strip().isdigit(): raise HTTPException(400,'Colección no válida')
        terms.append(f'collection_id:{collection_id.strip()}')
    # Shopify no encuentra `title:"lola"` entre comillas: hay que usar cada palabra como prefijo (`title:lola*`).
    # Varias palabras: por defecto basta que coincida cualquiera ('any'); con title_mode='all' deben coincidir todas.
    words=list(dict.fromkeys(re.findall(r'[\w-]+',title)))[:20]
    if words:
        if title_mode=='all': terms+=[f'title:{w}*' for w in words]
        else: terms.append('('+' OR '.join(f'title:{w}*' for w in words)+')')
    if not terms: raise HTTPException(400,'Indica al menos un criterio de búsqueda')
    st=shopify_store_by_code(store.strip().upper())
    if not st or not st['domain'] or not st['token']: raise HTTPException(404,'Tienda inactiva o sin credenciales')
    result=shopify_graphql(st['domain'],st['token'],TAG_SEARCH_QUERY,{'query':' '.join(terms),'after':after})
    data=result.get('products') or {}
    products=[{'id':n['id'],'title':n['title'],'handle':n['handle'],'status':n['status'],'category':n.get('productType') or ((n.get('category') or {}).get('name') or ''),'tags':n.get('tags') or [],'skus':[v['sku'] for v in n['variants']['nodes'] if v.get('sku')]} for n in data.get('nodes',[])]
    page=data.get('pageInfo') or {}
    return {'products':products,'has_next':bool(page.get('hasNextPage')),'cursor':page.get('endCursor'),'total':(result.get('productsCount') or {}).get('count')}

TAG_VARIANT_QUERY='''query TagBySku($query:String!) { productVariants(first:25,query:$query) { nodes { sku product { id } } } }'''
TAG_ADD='mutation($id:ID!,$tags:[String!]!) { tagsAdd(id:$id,tags:$tags) { node { ... on Product { tags } } userErrors { field message } } }'
TAG_REMOVE='mutation($id:ID!,$tags:[String!]!) { tagsRemove(id:$id,tags:$tags) { node { ... on Product { tags } } userErrors { field message } } }'

def tag_product(st,gid,add,remove):
    try:
        tags=None
        for root,mutation,values in (('tagsAdd',TAG_ADD,add),('tagsRemove',TAG_REMOVE,remove)):
            if not values: continue
            result=stock_mutation(st,mutation,{'id':gid,'tags':values},root)
            tags=(result.get('node') or {}).get('tags',tags)
        return {'id':gid,'ok':True,'tags':tags}
    except HTTPException as exc: return {'id':gid,'ok':False,'error':str(exc.detail)}

def tag_ids_by_sku(st,skus):
    """Productos de la tienda con alguna variante de esos SKU exactos. Devuelve (ids, skus no encontrados)."""
    ids={}; missing=[]
    for sku in skus:
        hits=[]
        nodes=shopify_graphql(st['domain'],st['token'],TAG_VARIANT_QUERY,{'query':_tag_term('sku',sku)}).get('productVariants',{}).get('nodes',[])
        for v in nodes:
            if (v.get('sku') or '').strip().casefold()==sku.casefold() and v.get('product'): hits.append(v['product']['id'])
        for h in hits: ids[h]=True
        if not hits: missing.append(sku)
    return list(ids),missing

@app.post('/api/tags/apply')
def tags_apply(x:TagApply,user=Depends(operator)):
    if not x.add and not x.remove: raise HTTPException(400,'Indica etiquetas que añadir o quitar')
    overlap=set(t.casefold() for t in x.add)&set(t.casefold() for t in x.remove)
    if overlap: raise HTTPException(400,'Una misma etiqueta no puede añadirse y quitarse a la vez')
    codes=list(dict.fromkeys([x.store]+[c.strip().upper() for c in x.stores if c.strip()]))
    by_code={s['code']:s for s in shopify_stores()}
    unknown=[c for c in codes if c not in by_code or not by_code[c]['domain'] or not by_code[c]['token']]
    if unknown: raise HTTPException(404,'Tienda inactiva o sin credenciales: '+', '.join(unknown))
    skus=list(dict.fromkeys(s.strip() for s in x.skus if s.strip()))
    if len(codes)>1 and not skus: raise HTTPException(400,'Para otras tiendas hacen falta SKUs de los productos seleccionados')
    def one_store(code):
        st=by_code[code]
        try:
            if code==x.store: ids,missing=x.product_ids,[]
            else:
                ids,missing=tag_ids_by_sku(st,skus)
            with ThreadPoolExecutor(max_workers=4) as pool_:
                results=list(pool_.map(lambda g:tag_product(st,g,x.add,x.remove),ids))
        except HTTPException as exc: return {'store':code,'results':[],'missing':[],'error':str(exc.detail)}
        return {'store':code,'results':results,'missing':missing,'error':None}
    with ThreadPoolExecutor(max_workers=4) as pool_:
        stores=list(pool_.map(one_store,codes))
    logging.getLogger('hub.tags').info('Etiquetas +%s -%s: %s',x.add,x.remove,{r['store']:(len(r['results']),sum(1 for i in r['results'] if not i['ok']),len(r['missing'])) for r in stores})
    return {'stores':stores}

@app.get('/tags',response_class=HTMLResponse)
def tags_page():
    body='''<main class="wrap"><h1>Etiquetado masivo</h1><p class="lede">Busca productos de una tienda por referencia (SKU), colección, etiqueta o nombre; los criterios rellenados se combinan. Selecciona los resultados y añade o quita etiquetas de golpe. Los cambios se escriben directamente en Shopify.</p>
<section class="card"><form id="tag-search" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(15rem,1fr))"><div class="field"><label for="store">Tienda</label><select id="store"></select></div><div class="field"><label for="f-sku">Referencias (SKU, varias separadas por coma, espacio o línea)</label><textarea id="f-sku" rows="2" autocomplete="off" spellcheck="false"></textarea></div><div class="field"><label for="f-collection">Colección</label><input id="f-collection" list="collection-list" autocomplete="off" placeholder="Escribe para buscar…"><datalist id="collection-list"></datalist></div><div class="field"><label for="f-tag">Etiqueta</label><input id="f-tag" autocomplete="off"></div><div class="field"><label for="f-title">Nombre (varias palabras)</label><input id="f-title" autocomplete="off" placeholder="Lola Hortensia Tarida"></div><div class="field"><label for="f-title-mode">Coincidencia de nombre</label><select id="f-title-mode"><option value="any">Cualquiera de las palabras</option><option value="all">Todas las palabras</option></select></div><div class="field"><label for="f-status">Estado del producto</label><select id="f-status"><option value="">Todos</option><option value="ACTIVE">Activo</option><option value="DRAFT">Borrador</option><option value="ARCHIVED">Archivado</option></select></div><div class="field"><label>&nbsp;</label><button class="button primary" id="search-button">Buscar</button></div></form><p class="muted" id="search-status" role="status" aria-live="polite"></p></section>
<section class="card" id="result-card" hidden><div style="display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.75rem"><button type="button" class="button" id="more" hidden>Cargar todos</button><span class="muted" id="count"></span></div><div class="table-wrap"><table><thead><tr><th><input type="checkbox" id="all" style="width:auto" aria-label="Seleccionar todos"></th><th>Producto</th><th>Estado</th><th>SKU</th><th style="min-width:14rem">Categoría</th><th>Etiquetas</th></tr></thead><tbody id="rows"></tbody></table></div></section>
<section class="card" id="apply-card" hidden><form id="tag-form" class="grid"><div class="field"><label for="add">Añadir etiquetas (separadas por comas)</label><input id="add" autocomplete="off"></div><div class="field"><label for="remove">Quitar etiquetas (separadas por comas)</label><input id="remove" autocomplete="off"></div><fieldset class="field"><legend>Aplicar también en otras tiendas (se buscan por SKU)</legend><div id="targets" style="display:flex;gap:.75rem;flex-wrap:wrap"></div></fieldset><div class="field"><label>&nbsp;</label><button class="button primary" id="apply-button">Aplicar a la selección</button></div></form><p class="muted" id="apply-status" role="status" aria-live="polite"></p></section>
</main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function fetchJSON(url,options){const r=await fetch(url,options);let data={};try{data=await r.json()}catch{}if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'Error al procesar la solicitud');return data}
const $=s=>document.querySelector(s),rows=$('#rows');let products=[],cursor=null,total=null,selected=new Set(),loading=false;
const list=v=>v.split(',').map(t=>t.trim()).filter(Boolean);
let storeCodes=[];function renderTargets(){const cur=$('#store').value;$('#targets').innerHTML=storeCodes.filter(c=>c!==cur).map(c=>'<label style="display:inline-flex;gap:.25rem;align-items:center"><input type="checkbox" class="target" value="'+esc(c)+'" style="width:auto"> '+esc(c)+'</label>').join('')}
fetchJSON('/api/stores').then(d=>{const s=(Array.isArray(d)?d:(d.stores||[])).filter(x=>x.active&&x.has_token);storeCodes=s.map(x=>x.code);$('#store').innerHTML=s.map(x=>'<option value="'+esc(x.code)+'">'+esc(x.code)+'</option>').join('');loadCollections('');renderTargets()}).catch(e=>$('#search-status').textContent=e.message);
let collMap=new Map(),collTimer=null,collSeq=0;
async function loadCollections(q){const n=++collSeq,store=$('#store').value;if(!store)return;try{const d=await fetchJSON('/api/tags/collections?store='+encodeURIComponent(store)+'&q='+encodeURIComponent(q||''));if(n!==collSeq)return;const dup=new Set(d.collections.map(c=>c.title).filter((t,i,a)=>a.indexOf(t)!==i));collMap=new Map(d.collections.map(c=>[dup.has(c.title)?c.title+' ('+c.handle+')':c.title,c.id]));$('#collection-list').innerHTML=[...collMap.keys()].map(k=>'<option value="'+esc(k)+'"></option>').join('')}catch(e){$('#search-status').textContent='No se pudieron cargar las colecciones: '+e.message}}
$('#f-collection').addEventListener('input',()=>{clearTimeout(collTimer);collTimer=setTimeout(()=>loadCollections($('#f-collection').value),250)});
$('#store').addEventListener('change',()=>{$('#f-collection').value='';loadCollections('');renderTargets()});
const STATUS_ES={ACTIVE:'Activo',DRAFT:'Borrador',ARCHIVED:'Archivado'};
function row(p){return '<tr data-id="'+esc(p.id)+'"><td><input type="checkbox" class="pick" value="'+esc(p.id)+'" style="width:auto" aria-label="Seleccionar '+esc(p.title)+'"></td><td>'+esc(p.title)+'</td><td><span class="badge">'+esc(STATUS_ES[p.status]||p.status)+'</span></td><td>'+esc(p.skus.join(', '))+'</td><td style="white-space:normal;min-width:14rem">'+esc(p.category)+'</td><td class="tags">'+p.tags.map(t=>'<span class="badge">'+esc(t)+'</span>').join(' ')+'</td></tr>'}
function render(){rows.innerHTML=products.map(row).join('');document.querySelectorAll('.pick').forEach(x=>x.checked=selected.has(x.value));$('#result-card').hidden=!products.length;$('#apply-card').hidden=!products.length;$('#more').hidden=!cursor;$('#count').textContent=products.length+(total!=null?' de '+total:'')+' producto(s) cargados · '+selected.size+' seleccionados'+(cursor?' · faltan por cargar':'');$('#all').checked=products.length>0&&products.every(p=>selected.has(p.id))}
async function fetchPage(append){const cv=$('#f-collection').value.trim(),colId=cv?(collMap.get(cv)||''):'';if(cv&&!colId)throw new Error('Elige una colección de la lista.');const q=new URLSearchParams({store:$('#store').value,sku:$('#f-sku').value,collection_id:colId,tag:$('#f-tag').value,title:$('#f-title').value,title_mode:$('#f-title-mode').value,status:$('#f-status').value});if(append&&cursor)q.set('after',cursor);const d=await fetchJSON('/api/tags/search?'+q);products=append?products.concat(d.products):d.products;cursor=d.has_next?d.cursor:null;if(!append||d.total!=null)total=d.total??total;return d}
async function load(append,all){if(loading)return;loading=true;const b=$('#search-button'),m=$('#more');b.disabled=true;m.disabled=true;$('#search-status').textContent='Buscando…';if(!append){selected=new Set();products=[];cursor=null;total=null}try{await fetchPage(append);render();while(all&&cursor){$('#search-status').textContent='Cargando '+products.length+(total!=null?' de '+total:'')+'…';await fetchPage(true);render()}$('#search-status').textContent=products.length?'':'Sin resultados.'}catch(e){$('#search-status').textContent=e.message;render()}finally{loading=false;b.disabled=false;m.disabled=false}}
$('#tag-search').addEventListener('submit',e=>{e.preventDefault();load(false)});$('#more').addEventListener('click',()=>load(true,true));
rows.addEventListener('change',e=>{if(e.target.classList.contains('pick')){e.target.checked?selected.add(e.target.value):selected.delete(e.target.value);render()}});
$('#all').addEventListener('change',e=>{products.forEach(p=>e.target.checked?selected.add(p.id):selected.delete(p.id));render()});
$('#tag-form').addEventListener('submit',async e=>{e.preventDefault();const ids=[...selected],add=list($('#add').value),remove=list($('#remove').value),st=$('#apply-status');if(!ids.length){st.textContent='Selecciona al menos un producto.';return}if(!add.length&&!remove.length){st.textContent='Indica etiquetas que añadir o quitar.';return}
const targets=[...document.querySelectorAll('.target:checked')].map(x=>x.value),skus=[...new Set(ids.flatMap(i=>products.find(p=>p.id===i)?.skus||[]))];if(targets.length&&!skus.length){st.textContent='Los productos seleccionados no tienen SKU para enlazar con otras tiendas.';return}
const b=$('#apply-button');b.disabled=true;st.textContent='Aplicando en '+(1+targets.length)+' tienda(s)…';try{const d=await fetchJSON('/api/tags/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({store:$('#store').value,product_ids:ids,add,remove,stores:targets,skus})});for(const r of d.stores[0].results)if(r.ok&&r.tags){const p=products.find(x=>x.id===r.id);if(p)p.tags=r.tags}render();st.innerHTML=d.stores.map(r=>{const bad=r.results.filter(i=>!i.ok);return '<strong>'+esc(r.store)+'</strong>: '+(r.error?esc(r.error):(r.results.length-bad.length)+' actualizados'+(bad.length?', '+bad.length+' con error ('+esc(bad.slice(0,2).map(i=>i.error).join('; '))+')':'')+(r.missing.length?', sin encontrar '+r.missing.length+' SKU ('+esc(r.missing.slice(0,5).join(', '))+(r.missing.length>5?'…':'')+')':''))}).join('<br>')}catch(err){st.textContent=err.message}finally{b.disabled=false}});
</script>'''
    return shell('Etiquetado masivo',body,'tags')


# ---- Traducción de colecciones ya existentes (origen y destino preexisten) ----
COLLECTION_FIELDS={'title':'Título','descriptionHtml':'Descripción','seo.title':'Título SEO','seo.description':'Descripción SEO','handle':'URL (handle)'}
COLLECTION_DEFAULT_FIELDS=['title','descriptionHtml','seo.title','seo.description']
COLLECTION_TRANSLATION_KEY={'title':'title','descriptionHtml':'body_html','seo.title':'meta_title','seo.description':'meta_description','handle':'handle'}
COLLECTION_QUERY='''query CollectionForTranslation($id:ID!) { collection(id:$id) { id title handle descriptionHtml seo { title description } } }'''

class CollectionTranslate(BaseModel):
    source_store:str=Field(pattern=r'^[A-Z0-9_-]{2,12}$')
    source_collection_id:str=Field(pattern=r'^\d{1,20}$')
    target_store:str=Field(pattern=r'^[A-Z0-9_-]{2,12}$')
    target_collection_id:str=Field(pattern=r'^\d{1,20}$')
    target_locale:str|None=Field(default=None,min_length=2,max_length=20)
    fields:list[str]=Field(default_factory=lambda:list(COLLECTION_DEFAULT_FIELDS),min_length=1,max_length=10)

    @field_validator('fields')
    @classmethod
    def check_fields(cls,value):
        bad=[f for f in value if f not in COLLECTION_FIELDS]
        if bad: raise ValueError('Campo no válido: '+', '.join(bad))
        return list(dict.fromkeys(value))

def collection_value(collection:dict,key:str)->str:
    if key.startswith('seo.'): return (collection.get('seo') or {}).get(key[4:]) or ''
    return collection.get(key) or ''

def load_collection(store:dict,numeric_id:str)->dict:
    c=shopify_graphql(store['domain'],store['token'],COLLECTION_QUERY,{'id':f'gid://shopify/Collection/{numeric_id}'}).get('collection')
    if not c: raise HTTPException(404,f'Colección {numeric_id} no encontrada en {store["code"]}')
    return c

@app.get('/api/collections/search')
def collections_search(store:str,q:str=''):
    return tags_collections(store,q)

@app.post('/api/collections/translate')
def collections_translate(x:CollectionTranslate,user=Depends(operator)):
    """Traduce una colección de la tienda origen y sustituye el contenido de otra colección ya existente en la tienda
    destino. Solo se tocan los campos de texto elegidos: reglas, productos, orden, imagen y demás ajustes se conservan."""
    if x.source_store==x.target_store: raise HTTPException(400,'La tienda origen y la destino deben ser distintas')
    source=shopify_store_by_code(x.source_store); target=shopify_store_by_code(x.target_store)
    for st,code in ((source,x.source_store),(target,x.target_store)):
        if not st or not st['domain'] or not st['token']: raise HTTPException(404,'Tienda inactiva o sin credenciales: '+code)
    source_locales=shopify_shop_locales(source); target_locales=shopify_shop_locales(target)
    source_locale=next((l['locale'] for l in source_locales if l.get('primary')),None)
    if not source_locale: raise HTTPException(502,'No se pudo determinar el idioma base de la tienda origen')
    locale_info=(next((l for l in target_locales if l.get('locale','').casefold()==x.target_locale.casefold()),None) if x.target_locale
                 else next((l for l in target_locales if l.get('primary')),None))
    if not locale_info: raise HTTPException(400,'El idioma destino no está habilitado en '+x.target_store)
    target_locale=locale_info['locale']
    if not target_profile(x.target_store,target_locale): raise HTTPException(400,f'{x.target_store} · {target_locale} no tiene un perfil de traducción activo')
    src=load_collection(source,x.source_collection_id); dst=load_collection(target,x.target_collection_id)
    translated={}; skipped=[]
    for key in x.fields:
        text=collection_value(src,key).strip()
        if not text: skipped.append(key); continue
        req=CellTranslateRequest(source_text=text,source_store=x.source_store,source_locale=source_locale,target_store=x.target_store,target_locale=target_locale,
            field_key={'title':'title','descriptionHtml':'body_html','seo.title':'seo_title','seo.description':'seo_description','handle':'handle'}[key],html=key=='descriptionHtml')
        translated[key]=translate_cell(req,user)['translation']
    if not translated: raise HTTPException(400,'La colección origen no tiene contenido en los campos elegidos')
    gid=dst['id']
    if locale_info.get('primary'):
        collection={'id':gid}
        for key,value in translated.items():
            if not key.startswith('seo.'): collection[key]=value
        if any(k.startswith('seo.') for k in translated):
            # CollectionInput.seo reemplaza el objeto entero: se reenvía el campo hermano.
            seo={k:v for k,v in (dst.get('seo') or {}).items() if v is not None}
            seo.update({k[4:]:v for k,v in translated.items() if k.startswith('seo.')})
            collection['seo']=seo
        if 'handle' in translated: collection['redirectNewHandle']=True
        mutation='''mutation UpdateCollection($input:CollectionInput!) { collectionUpdate(input:$input) { userErrors { field message } } }'''
        result=shopify_graphql(target['domain'],target['token'],mutation,{'input':collection}).get('collectionUpdate') or {}
        mode='base'
    else:
        res=shopify_graphql(target['domain'],target['token'],'query($id:ID!){translatableResource(resourceId:$id){translatableContent{key digest}}}',{'id':gid}).get('translatableResource') or {}
        digests={i['key']:i['digest'] for i in res.get('translatableContent',[])}
        items=[]
        for key,value in translated.items():
            tk=COLLECTION_TRANSLATION_KEY[key]
            if tk not in digests: raise HTTPException(409,f'Shopify no ofrece un digest traducible para «{COLLECTION_FIELDS[key]}» en la colección destino')
            items.append({'locale':target_locale,'key':tk,'value':value,'translatableContentDigest':digests[tk]})
        mutation='''mutation RegisterTranslation($id:ID!,$translations:[TranslationInput!]!) { translationsRegister(resourceId:$id,translations:$translations) { userErrors { field message } } }'''
        result=shopify_graphql(target['domain'],target['token'],mutation,{'id':gid,'translations':items}).get('translationsRegister') or {}
        mode='translation'
    errors=result.get('userErrors',[])
    if errors: raise HTTPException(400,'Shopify: '+'; '.join(e.get('message','error') for e in errors)[:500])
    logging.getLogger('hub.collections').info('Colección %s/%s -> %s/%s (%s, %s): %s',x.source_store,x.source_collection_id,x.target_store,x.target_collection_id,target_locale,mode,list(translated))
    return {'saved':True,'mode':mode,'target_locale':target_locale,'source_title':src['title'],'target_title_before':dst['title'],
        'fields':[{'key':k,'label':COLLECTION_FIELDS[k],'before':collection_value(dst,k),'source':collection_value(src,k),'after':v} for k,v in translated.items()],
        'skipped':[COLLECTION_FIELDS[k] for k in skipped]}

@app.get('/collections',response_class=HTMLResponse)
def collections_page():
    body='''<main class="wrap"><h1>Traducir colecciones</h1><p class="lede">Elige una colección de una tienda y la colección equivalente de otra tienda (las dos ya deben existir). El contenido de texto de la segunda se sustituye por la traducción de la primera; reglas, productos, orden e imágenes de la colección destino no se tocan. Los cambios se escriben directamente en Shopify.</p>
<section class="card"><form id="coll-form" class="grid" style="grid-template-columns:repeat(auto-fit,minmax(18rem,1fr))">
<fieldset class="field"><legend>Origen</legend><label for="s-store">Tienda</label><select id="s-store"></select><label for="s-coll" style="margin-top:.5rem">Colección</label><input id="s-coll" list="s-list" autocomplete="off" placeholder="Escribe para buscar…"><datalist id="s-list"></datalist></fieldset>
<fieldset class="field"><legend>Destino (se sobrescribe)</legend><label for="t-store">Tienda</label><select id="t-store"></select><label for="t-coll" style="margin-top:.5rem">Colección equivalente</label><input id="t-coll" list="t-list" autocomplete="off" placeholder="Escribe para buscar…"><datalist id="t-list"></datalist><label for="t-locale" style="margin-top:.5rem">Idioma destino</label><select id="t-locale"></select></fieldset>
<fieldset class="field"><legend>Campos a traducir</legend><div id="fields" style="display:flex;flex-direction:column;gap:.35rem"></div></fieldset>
<div class="field"><label>&nbsp;</label><button class="button primary" id="go">Traducir y sustituir</button></div></form><p class="muted" id="status" role="status" aria-live="polite"></p></section>
<section class="card" id="result" hidden></section></main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function fetchJSON(url,options){const r=await fetch(url,options);let data={};try{data=await r.json()}catch{}if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'Error al procesar la solicitud');return data}
const $=s=>document.querySelector(s);
const FIELDS=[['title','Título',1],['descriptionHtml','Descripción',1],['seo.title','Título SEO',1],['seo.description','Descripción SEO',1],['handle','URL (handle): cambia la dirección de la colección',0]];
$('#fields').innerHTML=FIELDS.map(f=>'<label style="display:inline-flex;gap:.4rem;align-items:center"><input type="checkbox" class="fld" value="'+f[0]+'" style="width:auto"'+(f[2]?' checked':'')+'> '+esc(f[1])+'</label>').join('');
const sides={s:{map:new Map(),seq:0,timer:null},t:{map:new Map(),seq:0,timer:null}};
async function loadColl(k,q){const side=sides[k],n=++side.seq,store=$('#'+k+'-store').value;if(!store)return;try{const d=await fetchJSON('/api/collections/search?store='+encodeURIComponent(store)+'&q='+encodeURIComponent(q||''));if(n!==side.seq)return;const dup=new Set(d.collections.map(c=>c.title).filter((t,i,a)=>a.indexOf(t)!==i));side.map=new Map(d.collections.map(c=>[dup.has(c.title)?c.title+' ('+c.handle+')':c.title,c.id]));$('#'+k+'-list').innerHTML=[...side.map.keys()].map(v=>'<option value="'+esc(v)+'"></option>').join('')}catch(e){$('#status').textContent='No se pudieron cargar las colecciones: '+e.message}}
for(const k of['s','t']){$('#'+k+'-coll').addEventListener('input',()=>{clearTimeout(sides[k].timer);sides[k].timer=setTimeout(()=>loadColl(k,$('#'+k+'-coll').value),250)});$('#'+k+'-store').addEventListener('change',()=>{$('#'+k+'-coll').value='';loadColl(k,'');if(k==='t')loadLocales()})}
async function loadLocales(){const st=$('#t-store').value;if(!st)return;try{const d=await fetchJSON('/api/stores/'+encodeURIComponent(st)+'/shop-locales');$('#t-locale').innerHTML=d.map(l=>'<option value="'+esc(l.locale)+'">'+esc(l.name||l.locale)+' ('+esc(l.locale)+')'+(l.primary?' · principal':'')+'</option>').join('')}catch(e){$('#t-locale').innerHTML='';$('#status').textContent=e.message}}
fetchJSON('/api/stores').then(d=>{const s=(Array.isArray(d)?d:(d.stores||[])).filter(x=>x.active&&x.has_token),o=s.map(x=>'<option value="'+esc(x.code)+'">'+esc(x.code)+'</option>').join('');$('#s-store').innerHTML=o;$('#t-store').innerHTML=o;if(s.length>1)$('#t-store').selectedIndex=1;loadColl('s','');loadColl('t','');loadLocales()}).catch(e=>$('#status').textContent=e.message);
$('#coll-form').addEventListener('submit',async e=>{e.preventDefault();const st=$('#status'),res=$('#result'),sid=sides.s.map.get($('#s-coll').value.trim()),tid=sides.t.map.get($('#t-coll').value.trim()),fields=[...document.querySelectorAll('.fld:checked')].map(x=>x.value);
if(!sid||!tid){st.textContent='Elige las dos colecciones de las listas.';return}if(!fields.length){st.textContent='Elige al menos un campo.';return}if($('#s-store').value===$('#t-store').value){st.textContent='Origen y destino deben ser tiendas distintas.';return}
const b=$('#go');b.disabled=true;res.hidden=true;st.textContent='Traduciendo y escribiendo en Shopify…';
try{const d=await fetchJSON('/api/collections/translate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_store:$('#s-store').value,source_collection_id:sid,target_store:$('#t-store').value,target_collection_id:tid,target_locale:$('#t-locale').value||null,fields})});
st.textContent='Hecho: «'+d.target_title_before+'» ('+$('#t-store').value+', '+d.target_locale+') actualizada desde «'+d.source_title+'»'+(d.skipped.length?'. Sin contenido en origen: '+d.skipped.join(', '):'')+'.';
res.innerHTML='<div class="table-wrap"><table><thead><tr><th>Campo</th><th>Antes (destino)</th><th>Origen</th><th>Ahora (destino)</th></tr></thead><tbody>'+d.fields.map(f=>'<tr><td>'+esc(f.label)+'</td><td style="white-space:normal;max-width:22rem">'+esc(f.before)+'</td><td style="white-space:normal;max-width:22rem">'+esc(f.source)+'</td><td style="white-space:normal;max-width:22rem">'+esc(f.after)+'</td></tr>').join('')+'</tbody></table></div>';res.hidden=false}
catch(err){st.textContent=err.message}finally{b.disabled=false}});
</script>'''
    return shell('Traducir colecciones',body,'collections')


# ---------------------------------------------------------------------------
# Panel de administración multitienda (/admin) — fase 1: lectura
# ---------------------------------------------------------------------------
from .multistore import run_per_store, graphql_with_retry, definition_matrix

def admin_store(code):
    st=shopify_store_by_code((code or '').strip().upper())
    if not st or not st['domain'] or not st['token']: raise HTTPException(404,'Tienda inactiva o sin credenciales')
    return st

def admin_gql(st,query,variables=None):
    return graphql_with_retry(shopify_graphql_payload,st['domain'],st['token'],query,variables)

ADMIN_PRODUCTS_QUERY='''query AdminProducts($query:String,$after:String) { productsCount(query:$query,limit:10000) { count } products(first:25,query:$query,after:$after,sortKey:UPDATED_AT,reverse:true) { nodes { id title handle status vendor productType tags totalInventory variants(first:50) { nodes { sku } } } pageInfo { hasNextPage endCursor } } }'''
ADMIN_COLLECTIONS_QUERY='''query AdminCollections($query:String,$after:String) { collections(first:25,query:$query,after:$after,sortKey:TITLE) { nodes { id title handle productsCount { count } updatedAt } pageInfo { hasNextPage endCursor } } }'''

def admin_search_term(q):
    q=(q or '').strip()
    if not q: return None
    safe=q.replace('\\','\\\\').replace('"','\\"')
    words=' '.join(f'title:{w}*' for w in re.findall(r'[\w-]+',q))
    return f'(sku:"{safe}") OR ({words})' if words else f'sku:"{safe}"'

@app.get('/api/admin/stores')
def admin_stores():
    return {'stores':[{'code':s['code'],'domain':s['domain'],'locales':s['locales']} for s in shopify_stores() if s['domain'] and s['token']]}

@app.get('/api/admin/products')
def admin_products(store:str,q:str='',after:str|None=None):
    st=admin_store(store)
    data=admin_gql(st,ADMIN_PRODUCTS_QUERY,{'query':admin_search_term(q),'after':after})
    page=(data.get('products') or {}).get('pageInfo') or {}
    return {'store':st['code'],'total':(data.get('productsCount') or {}).get('count'),'has_next':bool(page.get('hasNextPage')),'cursor':page.get('endCursor'),
            'products':[{'id':n['id'],'title':n['title'],'handle':n['handle'],'status':n['status'],'vendor':n.get('vendor'),'type':n.get('productType'),'tags':n.get('tags') or [],'inventory':n.get('totalInventory'),'skus':[v['sku'] for v in n['variants']['nodes'] if v.get('sku')]} for n in (data.get('products') or {}).get('nodes',[])]}

@app.get('/api/admin/product')
def admin_product(store:str,id:str):
    st=admin_store(store)
    if not id.startswith('gid://shopify/Product/'): raise HTTPException(400,'Identificador de producto no válido')
    product=shopify_product(st,id,[])
    if not product: raise HTTPException(404,'Producto no encontrado en la tienda')
    for key in ('translation_error','translations_by_locale'): product.pop(key,None)
    # Datos comerciales de las variantes (precio, coste, peso…): si faltan permisos, el resto del detalle sigue funcionando.
    try:
        extras={}; after=None
        while True:
            page=((admin_gql(st,ADMIN_VARIANT_EXTRA,{'id':id,'after':after}).get('product') or {}).get('variants')) or {}
            for n in page.get('nodes',[]): extras[n['id']]=admin_variant_flat(n)
            info=page.get('pageInfo') or {}
            if not info.get('hasNextPage') or not info.get('endCursor'): break
            after=info['endCursor']
        for v in product.get('variants',{}).get('nodes',[]): v['extra']=extras.get(v['id'])
    except HTTPException as exc:
        product['variant_extra_error']=str(exc.detail)[:300]
    return {'store':st['code'],'product':product}

@app.get('/api/admin/collections')
def admin_collections(store:str,q:str='',after:str|None=None):
    st=admin_store(store)
    words=' '.join(f'title:{w}*' for w in re.findall(r'[\w-]+',q)) or None
    data=(admin_gql(st,ADMIN_COLLECTIONS_QUERY,{'query':words,'after':after}).get('collections') or {})
    page=data.get('pageInfo') or {}
    return {'store':st['code'],'has_next':bool(page.get('hasNextPage')),'cursor':page.get('endCursor'),
            'collections':[{'id':n['id'],'title':n['title'],'handle':n['handle'],'products':(n.get('productsCount') or {}).get('count'),'updated':n.get('updatedAt')} for n in data.get('nodes',[])]}

@app.get('/api/admin/metafield-definitions')
def admin_metafield_definitions(stores:str=''):
    wanted=[c.strip().upper() for c in stores.split(',') if c.strip()]
    available={s['code']:s for s in shopify_stores() if s['domain'] and s['token']}
    unknown=[c for c in wanted if c not in available]
    chosen=[available[c] for c in (wanted or available) if c in available]
    results=run_per_store(chosen,lambda s:metafield_definitions(s['code']))
    per_store={r['store']:r['data'] for r in results if r['ok']}
    return {'stores':[s['code'] for s in chosen],'rows':definition_matrix(per_store),
            'errors':[{'store':r['store'],'error':r['error']} for r in results if not r['ok']]+[{'store':c,'error':'Tienda inactiva o sin credenciales'} for c in unknown]}

# --- Fase 2: edición en una sola tienda, con auditoría ---------------------------------
def admin_audit(kind,store_code,gid,status,error=None,before=None,after=None,payload=None):
    """Registra una operación de escritura. Un fallo de auditoría nunca debe ocultar el resultado de Shopify."""
    try:
        with pool.connection() as c:
            op=c.execute("INSERT INTO admin_operations(operator,kind,mode,payload) VALUES(%s,%s,'single',%s) RETURNING id",(operator(),kind,Jsonb(payload or {}))).fetchone()[0]
            c.execute("INSERT INTO admin_operation_items(op_id,store_code,entity_gid,status,error,before,after) VALUES(%s,%s,%s,%s,%s,%s,%s)",(op,store_code,gid,status,error,Jsonb(before) if before is not None else None,Jsonb(after) if after is not None else None))
    except Exception:
        logging.getLogger('hub.admin').exception('No se pudo registrar la auditoría')

def admin_mutate(st,kind,gid,query,variables,root,before,after,payload=None):
    """Ejecuta una mutación, lanza 400 con los userErrors y deja rastro en la auditoría en ambos casos."""
    try:
        result=admin_exec(st,query,variables,root)
    except HTTPException as exc:
        admin_audit(kind,st['code'],gid,'FAILED',str(exc.detail)[:500],before,None,payload)
        raise
    admin_audit(kind,st['code'],gid,'OK',None,before,after,payload)
    logging.getLogger('hub.admin').info('%s %s %s',kind,st['code'],gid)
    return result

def _text(value): return value if isinstance(value,str) else ('' if value is None else str(value))

def _flat_seo(node):
    seo=node.get('seo') or {}
    return {'seo_title':seo.get('title') or '','seo_description':seo.get('description') or ''}

def _changed(current,fields,allowed):
    unknown=[k for k in fields if k not in allowed]
    if unknown: raise HTTPException(400,'Campos no editables: '+', '.join(unknown))
    return {k:v for k,v in fields.items() if v!=current.get(k)}

def _seo_input(current,changes):
    if not any(k.startswith('seo_') for k in changes): return None
    return {'title':changes.get('seo_title',current['seo_title']),'description':changes.get('seo_description',current['seo_description'])}

class AdminEntityUpdate(BaseModel):
    store:str
    id:str
    fields:dict

ADMIN_PRODUCT_READ='query($id:ID!){product(id:$id){title descriptionHtml handle vendor productType status tags seo{title description}}}'
ADMIN_PRODUCT_FIELDS={'title','descriptionHtml','handle','vendor','productType','status','tags','seo_title','seo_description'}

@app.post('/api/admin/product/update')
def admin_product_update(x:AdminEntityUpdate):
    st=admin_store(x.store)
    if not x.id.startswith('gid://shopify/Product/'): raise HTTPException(400,'Identificador de producto no válido')
    node=admin_gql(st,ADMIN_PRODUCT_READ,{'id':x.id}).get('product')
    if not node: raise HTTPException(404,'Producto no encontrado en la tienda')
    current={**{k:_text(node.get(k)) for k in ('title','descriptionHtml','handle','vendor','productType','status')},'tags':sorted(node.get('tags') or []),**_flat_seo(node)}
    fields=dict(x.fields)
    if 'tags' in fields:
        raw=fields['tags']; fields['tags']=sorted({t.strip() for t in (raw.split(',') if isinstance(raw,str) else raw) if str(t).strip()})
    if 'status' in fields and fields['status'] not in ('ACTIVE','ARCHIVED','DRAFT'): raise HTTPException(400,'Estado no válido')
    changes=_changed(current,fields,ADMIN_PRODUCT_FIELDS)
    if not changes: return {'store':st['code'],'changed':[],'saved':False}
    product={'id':x.id,**{k:v for k,v in changes.items() if not k.startswith('seo_')}}
    seo=_seo_input(current,changes)
    if seo is not None: product['seo']=seo  # ProductUpdateInput.seo sustituye el objeto: se reenvía el campo hermano
    if 'handle' in changes: product['redirectNewHandle']=True
    admin_mutate(st,'product.update',x.id,'mutation($product:ProductUpdateInput!){productUpdate(product:$product){product{id} userErrors{field message}}}',{'product':product},'productUpdate',{k:current[k] for k in changes},changes)
    return {'store':st['code'],'changed':list(changes),'saved':True}

ADMIN_COLLECTION_READ='query($id:ID!){collection(id:$id){title descriptionHtml handle seo{title description}}}'
ADMIN_COLLECTION_FIELDS={'title','descriptionHtml','handle','seo_title','seo_description'}

@app.get('/api/admin/collection')
def admin_collection(store:str,id:str):
    st=admin_store(store)
    if not id.startswith('gid://shopify/Collection/'): raise HTTPException(400,'Identificador de colección no válido')
    q='query($id:ID!){collection(id:$id){id title descriptionHtml handle seo{title description} metafields(first:100){nodes{id namespace key type value compareDigest}}}}'
    node=admin_gql(st,q,{'id':id}).get('collection')
    if not node: raise HTTPException(404,'Colección no encontrada en la tienda')
    return {'store':st['code'],'collection':node}

@app.post('/api/admin/collection/update')
def admin_collection_update(x:AdminEntityUpdate):
    st=admin_store(x.store)
    if not x.id.startswith('gid://shopify/Collection/'): raise HTTPException(400,'Identificador de colección no válido')
    node=admin_gql(st,ADMIN_COLLECTION_READ,{'id':x.id}).get('collection')
    if not node: raise HTTPException(404,'Colección no encontrada en la tienda')
    current={**{k:_text(node.get(k)) for k in ('title','descriptionHtml','handle')},**_flat_seo(node)}
    changes=_changed(current,dict(x.fields),ADMIN_COLLECTION_FIELDS)
    if not changes: return {'store':st['code'],'changed':[],'saved':False}
    collection={'id':x.id,**{k:v for k,v in changes.items() if not k.startswith('seo_')}}
    seo=_seo_input(current,changes)
    if seo is not None: collection['seo']=seo
    if 'handle' in changes: collection['redirectNewHandle']=True
    admin_mutate(st,'collection.update',x.id,'mutation($input:CollectionInput!){collectionUpdate(input:$input){collection{id} userErrors{field message}}}',{'input':collection},'collectionUpdate',{k:current[k] for k in changes},changes)
    return {'store':st['code'],'changed':list(changes),'saved':True}

ADMIN_VARIANT_FIELDS_GQL='price compareAtPrice taxable inventoryPolicy inventoryItem { tracked requiresShipping unitCost { amount currencyCode } measurement { weight { value unit } } }'
ADMIN_VARIANT_EXTRA='query($id:ID!,$after:String){product(id:$id){variants(first:100,after:$after){nodes{id '+ADMIN_VARIANT_FIELDS_GQL+'} pageInfo{hasNextPage endCursor}}}}'
ADMIN_VARIANT_UNITS={'GRAMS','KILOGRAMS','OUNCES','POUNDS'}

def _dec(value):
    """Normaliza un número decimal para compararlo ('10', '10.00' y '10.0' son iguales). Vacío → ''."""
    if value is None or str(value).strip()=='' : return ''
    try: d=Decimal(str(value).strip().replace(',','.'))
    except Exception: raise HTTPException(400,f'Número no válido: {value}')
    if not d.is_finite() or d<0: raise HTTPException(400,f'Número no válido: {value}')
    return format(d.normalize(),'f') if d!=0 else '0'

def admin_variant_flat(node):
    item=node.get('inventoryItem') or {}; weight=((item.get('measurement') or {}).get('weight')) or {}; cost=item.get('unitCost') or {}
    return {'price':_dec(node.get('price')),'compareAtPrice':_dec(node.get('compareAtPrice')),'cost':_dec(cost.get('amount')),'currency':cost.get('currencyCode') or '',
            'weight':_dec(weight.get('value')),'weightUnit':weight.get('unit') or 'KILOGRAMS','taxable':bool(node.get('taxable')),'inventoryPolicy':node.get('inventoryPolicy') or 'DENY',
            'tracked':bool(item.get('tracked')),'requiresShipping':bool(item.get('requiresShipping'))}

def admin_variant_input(vid,changes,current):
    """ProductVariantsBulkInput a partir de los campos cambiados (peso y unidad se combinan con el valor actual)."""
    variant={'id':vid}
    if 'barcode' in changes: variant['barcode']=changes['barcode']
    if 'price' in changes: variant['price']=changes['price']
    if 'compareAtPrice' in changes: variant['compareAtPrice']=changes['compareAtPrice'] or None
    if 'taxable' in changes: variant['taxable']=changes['taxable']
    if 'inventoryPolicy' in changes: variant['inventoryPolicy']=changes['inventoryPolicy']
    item={}
    if 'sku' in changes: item['sku']=changes['sku']
    if 'tracked' in changes: item['tracked']=changes['tracked']
    if 'requiresShipping' in changes: item['requiresShipping']=changes['requiresShipping']
    if 'cost' in changes: item['cost']=changes['cost']
    if 'weight' in changes or 'weightUnit' in changes:
        weight=changes.get('weight',current['weight'])
        if weight=='': raise HTTPException(400,'Indica el peso para cambiar su unidad')
        item['measurement']={'weight':{'value':float(weight),'unit':changes.get('weightUnit',current['weightUnit'])}}
    if item: variant['inventoryItem']=item
    return variant

class AdminVariantUpdate(BaseModel):
    store:str
    id:str
    fields:dict

ADMIN_VARIANT_TEXT={'sku','barcode'}
ADMIN_VARIANT_DECIMAL={'price','compareAtPrice','cost','weight'}
ADMIN_VARIANT_BOOL={'taxable','tracked','requiresShipping'}
ADMIN_VARIANT_ALLOWED=ADMIN_VARIANT_TEXT|ADMIN_VARIANT_DECIMAL|ADMIN_VARIANT_BOOL|{'inventoryPolicy','weightUnit'}

@app.post('/api/admin/variant/update')
def admin_variant_update(x:AdminVariantUpdate):
    st=admin_store(x.store)
    if not x.id.startswith('gid://shopify/ProductVariant/'): raise HTTPException(400,'Identificador de variante no válido')
    node=admin_gql(st,'query($id:ID!){productVariant(id:$id){sku barcode product{id} '+ADMIN_VARIANT_FIELDS_GQL+'}}',{'id':x.id}).get('productVariant')
    if not node: raise HTTPException(404,'Variante no encontrada en la tienda')
    current={'sku':_text(node.get('sku')),'barcode':_text(node.get('barcode')),**admin_variant_flat(node)}
    fields={}
    for k,v in x.fields.items():
        if k in ADMIN_VARIANT_TEXT: fields[k]=_text(v).strip()
        elif k in ADMIN_VARIANT_DECIMAL: fields[k]=_dec(v)
        elif k in ADMIN_VARIANT_BOOL:
            if not isinstance(v,bool): raise HTTPException(400,f'{k} debe ser verdadero o falso')
            fields[k]=v
        else: fields[k]=v
    if fields.get('inventoryPolicy',current['inventoryPolicy']) not in ('DENY','CONTINUE'): raise HTTPException(400,'Política de inventario no válida')
    if fields.get('weightUnit',current['weightUnit']) not in ADMIN_VARIANT_UNITS: raise HTTPException(400,'Unidad de peso no válida')
    changes=_changed(current,fields,ADMIN_VARIANT_ALLOWED)
    if not changes: return {'store':st['code'],'changed':[],'saved':False}
    if 'price' in changes and changes['price']=='': raise HTTPException(400,'El precio no puede quedar vacío')
    if 'cost' in changes and changes['cost']=='': raise HTTPException(400,'Shopify no permite vaciar el coste desde aquí')
    variant=admin_variant_input(x.id,changes,current)
    admin_mutate(st,'variant.update',x.id,'mutation($productId:ID!,$variants:[ProductVariantsBulkInput!]!){productVariantsBulkUpdate(productId:$productId,variants:$variants){productVariants{id} userErrors{field message}}}',{'productId':node['product']['id'],'variants':[variant]},'productVariantsBulkUpdate',{k:current[k] for k in changes},changes)
    return {'store':st['code'],'changed':list(changes),'saved':True}

def admin_metafield_value(mtype,value):
    """Valida/convierte el valor según el tipo del metacampo. El texto enriquecido admite texto plano (un párrafo por línea)."""
    if mtype=='rich_text_field':
        try:
            doc=json.loads(value)
            if isinstance(doc,dict) and doc.get('type')=='root': return value
        except ValueError: pass
        paragraphs=[{'type':'paragraph','children':[{'type':'text','value':line}]} for line in value.split(chr(10)) if line.strip()]
        if not paragraphs: raise HTTPException(400,'El texto enriquecido no puede quedar vacío')
        return json.dumps({'type':'root','children':paragraphs},ensure_ascii=False,separators=(',',':'))
    if mtype=='json' or mtype.startswith('list.'):
        try: json.loads(value)
        except ValueError: raise HTTPException(400,f'El valor de tipo {mtype} debe ser JSON válido (por ejemplo ["a","b"])')
    elif mtype=='number_integer':
        if not re.fullmatch(r'-?\d+',value.strip()): raise HTTPException(400,'Debe ser un número entero')
        return value.strip()
    elif mtype=='number_decimal':
        try: float(value.strip().replace(',','.'))
        except ValueError: raise HTTPException(400,'Debe ser un número')
        return value.strip().replace(',','.')
    elif mtype=='boolean':
        if value.strip().lower() not in ('true','false'): raise HTTPException(400,'Debe ser true o false')
        return value.strip().lower()
    return value

class AdminMetafieldSet(BaseModel):
    store:str
    owner_id:str
    namespace:str=Field(pattern=r'^[A-Za-z0-9_-]{2,255}$')
    key:str=Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    type:str=Field(min_length=3,max_length=80)
    value:str
    compare_digest:str|None=None

ADMIN_MF_READ='query($id:ID!,$ns:String!,$key:String!){node(id:$id){... on Product{metafield(namespace:$ns,key:$key){value type}} ... on ProductVariant{metafield(namespace:$ns,key:$key){value type}} ... on Collection{metafield(namespace:$ns,key:$key){value type}}}}'

@app.post('/api/admin/metafield/set')
def admin_metafield_set(x:AdminMetafieldSet):
    st=admin_store(x.store)
    if not x.owner_id.startswith(('gid://shopify/Product/','gid://shopify/ProductVariant/','gid://shopify/Collection/')): raise HTTPException(400,'Propietario de metacampo no admitido')
    x.value=admin_metafield_value(x.type,x.value)
    current=((admin_gql(st,ADMIN_MF_READ,{'id':x.owner_id,'ns':x.namespace,'key':x.key}).get('node') or {}).get('metafield')) or {}
    if current.get('value')==x.value: return {'store':st['code'],'saved':False}
    entry={'ownerId':x.owner_id,'namespace':x.namespace,'key':x.key,'type':x.type,'value':x.value}
    if x.compare_digest: entry['compareDigest']=x.compare_digest
    admin_mutate(st,'metafield.set',x.owner_id,'mutation($metafields:[MetafieldsSetInput!]!){metafieldsSet(metafields:$metafields){metafields{id} userErrors{field message}}}',{'metafields':[entry]},'metafieldsSet',{f'{x.namespace}.{x.key}':current.get('value')},{f'{x.namespace}.{x.key}':x.value})
    return {'store':st['code'],'saved':True}

class AdminMetafieldDelete(BaseModel):
    store:str
    owner_id:str
    namespace:str=Field(pattern=r'^[A-Za-z0-9_-]{2,255}$')
    key:str=Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')

@app.post('/api/admin/metafield/delete')
def admin_metafield_delete(x:AdminMetafieldDelete):
    """Vacía un metacampo (lo elimina del producto, variante o colección). El valor anterior queda en la auditoría."""
    st=admin_store(x.store)
    if not x.owner_id.startswith(('gid://shopify/Product/','gid://shopify/ProductVariant/','gid://shopify/Collection/')): raise HTTPException(400,'Propietario de metacampo no admitido')
    current=((admin_gql(st,ADMIN_MF_READ,{'id':x.owner_id,'ns':x.namespace,'key':x.key}).get('node') or {}).get('metafield')) or {}
    if not current: return {'store':st['code'],'saved':False}
    admin_mutate(st,'metafield.delete',x.owner_id,'mutation($metafields:[MetafieldIdentifierInput!]!){metafieldsDelete(metafields:$metafields){deletedMetafields{key} userErrors{field message}}}',{'metafields':[{'ownerId':x.owner_id,'namespace':x.namespace,'key':x.key}]},'metafieldsDelete',{f'{x.namespace}.{x.key}':current.get('value')},{f'{x.namespace}.{x.key}':None})
    return {'store':st['code'],'saved':True}

@app.get('/api/admin/operations')
def admin_operations(limit:int=50):
    with pool.connection() as c:
        rows=c.execute("SELECT o.id,o.created_at,o.kind,i.store_code,i.entity_gid,i.status,i.error FROM admin_operations o JOIN admin_operation_items i ON i.op_id=o.id ORDER BY o.id DESC LIMIT %s",(max(1,min(limit,200)),)).fetchall()
    return {'operations':[{'id':r[0],'at':r[1].isoformat(),'kind':r[2],'store':r[3],'entity':r[4],'status':r[5],'error':r[6]} for r in rows]}

# --- Fase 3: modo Global (varias tiendas, emparejado por SKU, con preflight) ---------------
GLOBAL_FIELD_LABELS={'status':'Estado','vendor':'Proveedor','productType':'Tipo de producto','weight':'Peso','weightUnit':'Unidad de peso','taxable':'Cobrar impuestos','inventoryPolicy':'Si no hay stock','tracked':'Seguimiento de stock','requiresShipping':'Requiere envío'}
# Precios y costes quedan fuera a propósito: cada tienda puede tener otra moneda.
_GLOBAL_VARIANT_FIELDS={'weight','weightUnit','taxable','inventoryPolicy','tracked','requiresShipping'}
GLOBAL_FIELDS={'product':{'status','vendor','productType'},'variant':_GLOBAL_VARIANT_FIELDS,'product_variants':_GLOBAL_VARIANT_FIELDS}
GLOBAL_MAX_SKUS=200

class AdminGlobalChange(BaseModel):
    kind:str
    field:str|None=None
    namespace:str|None=Field(None,pattern=r'^[A-Za-z0-9_-]{2,255}$')
    key:str|None=Field(None,pattern=r'^[A-Za-z0-9_-]{1,64}$')
    type:str|None=Field(None,max_length=80)
    value:Any=None
    clear:bool=False

class AdminGlobalRequest(BaseModel):
    scope:str
    stores:list[str]=[]
    skus:list[str]
    changes:list[AdminGlobalChange]

def admin_global_change(scope,c):
    if c.kind=='metafield':
        if not (c.namespace and c.key and c.type): raise HTTPException(400,'Metacampo incompleto: faltan namespace, clave o tipo')
        if c.clear: return {'kind':'metafield','label':f'{c.namespace}.{c.key}','namespace':c.namespace,'key':c.key,'type':c.type,'value':'','clear':True}
        if not isinstance(c.value,str): raise HTTPException(400,f'Valor no válido para {c.namespace}.{c.key}')
        return {'kind':'metafield','label':f'{c.namespace}.{c.key}','namespace':c.namespace,'key':c.key,'type':c.type,'value':admin_metafield_value(c.type,c.value)}
    if c.kind!='field' or c.field not in GLOBAL_FIELDS[scope]: raise HTTPException(400,f'Campo no editable en modo global: {c.field}')
    v=c.value
    if c.field=='status' and v not in ('ACTIVE','DRAFT','ARCHIVED'): raise HTTPException(400,'Estado no válido')
    if c.field=='inventoryPolicy' and v not in ('DENY','CONTINUE'): raise HTTPException(400,'Política de inventario no válida')
    if c.field=='weightUnit' and v not in ADMIN_VARIANT_UNITS: raise HTTPException(400,'Unidad de peso no válida')
    if c.field=='weight':
        v=_dec(v)
        if v=='': raise HTTPException(400,'Indica el peso')
    if c.field in ADMIN_VARIANT_BOOL and not isinstance(v,bool): raise HTTPException(400,f'{c.field} debe ser verdadero o falso')
    if c.field in ('vendor','productType'): v=_text(v).strip()
    return {'kind':'field','label':GLOBAL_FIELD_LABELS[c.field],'field':c.field,'value':v}

def admin_global_prepare(x):
    if x.scope not in ('product','variant','product_variants'): raise HTTPException(400,'Ámbito no válido')
    skus=list(dict.fromkeys(s.strip() for s in x.skus if s.strip()))
    if not skus: raise HTTPException(400,'Indica al menos un SKU')
    if len(skus)>GLOBAL_MAX_SKUS: raise HTTPException(400,f'Máximo {GLOBAL_MAX_SKUS} SKU por operación')
    if not x.changes or len(x.changes)>20: raise HTTPException(400,'Indica entre 1 y 20 cambios')
    changes=[admin_global_change(x.scope,c) for c in x.changes]
    targets=[(c['kind'],c.get('field') or c['label']) for c in changes]
    if len(set(targets))!=len(targets): raise HTTPException(400,'Hay cambios repetidos sobre el mismo campo')
    available={s['code']:s for s in shopify_stores() if s['domain'] and s['token']}
    wanted=list(dict.fromkeys(c.strip().upper() for c in x.stores if c.strip())) or list(available)
    unknown=[{'store':c,'blocked':True,'exceptions':[{'code':'STORE_NO_CREDENTIALS','message':f'La tienda {c} no está activa o no tiene credenciales'}],'items':[]} for c in wanted if c not in available]
    return skus,changes,[available[c] for c in wanted if c in available],unknown

def admin_resolve_skus(st,skus):
    """{sku_en_minúsculas: [variantes]} con coincidencia exacta de SKU en la tienda."""
    found={}
    query='query($q:String!,$after:String){productVariants(first:100,query:$q,after:$after){nodes{id sku product{id title}} pageInfo{hasNextPage endCursor}}}'
    for i in range(0,len(skus),10):
        q=' OR '.join('sku:"%s"'%s.replace('\\','\\\\').replace('"','\\"') for s in skus[i:i+10]); after=None
        while True:
            data=admin_gql(st,query,{'q':q,'after':after}).get('productVariants') or {}
            for n in data.get('nodes',[]):
                if n.get('sku'): found.setdefault(n['sku'].strip().lower(),[]).append(n)
            info=data.get('pageInfo') or {}
            if not info.get('hasNextPage') or not info.get('endCursor'): break
            after=info['endCursor']
    return found

def admin_global_fetch(st,scope,ids,changes):
    aliases=' '.join('m%d:metafield(namespace:%s,key:%s){ value }'%(i,json.dumps(c['namespace']),json.dumps(c['key'])) for i,c in enumerate(changes) if c['kind']=='metafield')
    if scope=='product': frag,sel='Product',f'id title status vendor productType {aliases}'
    else: frag,sel='ProductVariant',f'id title sku product {{ id title }} {ADMIN_VARIANT_FIELDS_GQL} {aliases}'
    nodes={}
    for i in range(0,len(ids),50):
        data=admin_gql(st,'query($ids:[ID!]!){ nodes(ids:$ids){ ... on %s { %s } } }'%(frag,sel),{'ids':ids[i:i+50]})
        for n in data.get('nodes') or []:
            if n: nodes[n['id']]=n
    return nodes

ADMIN_PV_FIELDS='id title sku product { id title } '
def admin_global_fetch_product_variants(st,product_ids,changes):
    """{id_variante: nodo} con TODAS las variantes de los productos indicados (con los campos y metacampos de los cambios)."""
    aliases=' '.join('m%d:metafield(namespace:%s,key:%s){ value }'%(i,json.dumps(c['namespace']),json.dumps(c['key'])) for i,c in enumerate(changes) if c['kind']=='metafield')
    sel=f'{ADMIN_PV_FIELDS}{ADMIN_VARIANT_FIELDS_GQL} {aliases}'
    page_q='query($id:ID!,$after:String){ product(id:$id){ id title variants(first:100,after:$after){ nodes{ %s } pageInfo{ hasNextPage endCursor } } } }'%sel
    out={}
    def add(product_title,nodes):
        for n in nodes:
            out[n['id']]=n
    for i in range(0,len(product_ids),5):
        data=admin_gql(st,'query($ids:[ID!]!){ nodes(ids:$ids){ ... on Product { id title variants(first:100){ nodes{ %s } pageInfo{ hasNextPage endCursor } } } } }'%sel,{'ids':product_ids[i:i+5]})
        for p in data.get('nodes') or []:
            if not p: continue
            add(p['title'],p['variants']['nodes']); info=p['variants']['pageInfo']
            while info.get('hasNextPage') and info.get('endCursor'):  # productos con más de 100 variantes
                more=(admin_gql(st,page_q,{'id':p['id'],'after':info['endCursor']}).get('product') or {}).get('variants') or {}
                add(p['title'],more.get('nodes',[])); info=more.get('pageInfo') or {}
    return out

def admin_global_plan_store(st,scope,skus,changes):
    """Preflight de una tienda: no escribe. Las excepciones de tienda la bloquean; las de SKU solo omiten ese elemento."""
    plan={'store':st['code'],'blocked':False,'exceptions':[],'duplicates':[],'items':[]}
    def block(code,message,**extra):
        plan['blocked']=True; plan['exceptions'].append({'code':code,'message':message,**extra})
    owner='PRODUCT' if scope=='product' else 'PRODUCTVARIANT'
    mfs=[c for c in changes if c['kind']=='metafield']
    if mfs:
        defs=metafield_definitions(st['code'])
        for c in mfs:
            d=defs.get((owner,c['namespace'],c['key']))
            if not d: block('METAFIELD_DEFINITION_MISSING',f'El metacampo {c["label"]} no existe en {st["code"]}',owner=owner,namespace=c['namespace'],key=c['key'],type=c['type']); continue
            if d['type']!=c['type']: block('METAFIELD_TYPE_MISMATCH',f'{c["label"]} es de tipo {d["type"]} en {st["code"]} (se esperaba {c["type"]})'); continue
            if d.get('choices') and not c.get('clear'):
                try: values=json.loads(c['value']) if c['type'].startswith('list.') else [c['value']]
                except ValueError: values=[None]
                bad=[v for v in values if v not in d['choices']]
                if bad: block('INVALID_CHOICE',f'El valor de {c["label"]} no está entre las opciones de {st["code"]}: {", ".join(d["choices"])}'[:400])
    if plan['blocked']: return plan
    resolved=admin_resolve_skus(st,skus); entities={}
    for sku in skus:
        matches=resolved.get(sku.lower(),[])
        if not matches:
            plan['exceptions'].append({'code':'SKU_NOT_FOUND','sku':sku,'message':f'El SKU {sku} no existe en {st["code"]}'}); continue
        if len(matches)>1: plan['duplicates'].append(admin_dup_entry(sku,matches))
        for m in matches:  # si un SKU está repetido, se aplica a todas sus instancias
            eid=m['id'] if scope=='variant' else m['product']['id']
            e=entities.setdefault(eid,{'id':eid,'skus':[],'product_id':m['product']['id']})
            if sku not in e['skus']: e['skus'].append(sku)
    if scope=='product_variants':
        nodes=admin_global_fetch_product_variants(st,list(entities),changes) if entities else {}
        by_product={}
        for vid,n in nodes.items(): by_product.setdefault(n['product']['id'],[]).append(n)
        for pid,e in entities.items():
            if not by_product.get(pid): plan['exceptions'].append({'code':'ENTITY_UNREADABLE','sku':e['skus'][0],'message':f'No se pudieron leer las variantes de {e["skus"][0]} en {st["code"]}'})
        work=[(n['id'],n,[n.get('sku') or '(sin SKU)'],n['product']['id']) for pid in entities for n in by_product.get(pid,[])]
    else:
        nodes=admin_global_fetch(st,scope,list(entities),changes) if entities else {}
        work=[]
        for eid,e in entities.items():
            node=nodes.get(eid)
            if not node: plan['exceptions'].append({'code':'ENTITY_UNREADABLE','sku':e['skus'][0],'message':f'No se pudo leer {e["skus"][0]} en {st["code"]}'}); continue
            work.append((eid,node,e['skus'],e['product_id']))
    is_variant=scope in ('variant','product_variants')
    for eid,node,item_skus,product_id in work:
        flat=admin_variant_flat(node) if is_variant else None
        diff=[]
        for i,c in enumerate(changes):
            if c['kind']=='metafield': before=(node.get(f'm{i}') or {}).get('value') or ''
            elif not is_variant: before=_text(node.get(c['field']))
            else: before=flat[c['field']]
            if before!=c['value']: diff.append({'i':i,'label':c['label'],'before':before,'after':c['value']})
        title=node['title'] if scope=='product' else f"{(node.get('product') or {}).get('title','')} · {node.get('title','')}"
        item={'id':eid,'skus':item_skus,'product_id':product_id,'title':title,'changes':diff}
        if is_variant: item['current']={'weight':flat['weight'],'weightUnit':flat['weightUnit']}
        plan['items'].append(item)
    return plan

def admin_exec(st,query,variables,root):
    result=(admin_gql(st,query,variables).get(root) or {})
    errors=result.get('userErrors') or []
    if errors: raise HTTPException(400,'Shopify: '+'; '.join(e.get('message','error') for e in errors)[:500])
    return result

def admin_global_apply_store(st,scope,plan,changes):
    """Aplica el plan de una tienda elemento a elemento: un fallo no detiene los demás."""
    results=[]
    for item in plan['items']:
        if not item['changes']:
            results.append({**item,'status':'SKIPPED'}); continue
        try:
            fields={changes[d['i']]['field']:changes[d['i']]['value'] for d in item['changes'] if changes[d['i']]['kind']=='field'}
            entries=[{'ownerId':item['id'],'namespace':changes[d['i']]['namespace'],'key':changes[d['i']]['key'],'type':changes[d['i']]['type'],'value':changes[d['i']]['value']} for d in item['changes'] if changes[d['i']]['kind']=='metafield' and not changes[d['i']].get('clear')]
            clears=[{'ownerId':item['id'],'namespace':changes[d['i']]['namespace'],'key':changes[d['i']]['key']} for d in item['changes'] if changes[d['i']]['kind']=='metafield' and changes[d['i']].get('clear')]
            if fields and scope=='product':
                admin_exec(st,'mutation($product:ProductUpdateInput!){productUpdate(product:$product){product{id} userErrors{field message}}}',{'product':{'id':item['id'],**fields}},'productUpdate')
            elif fields:
                variant=admin_variant_input(item['id'],fields,item['current'])
                admin_exec(st,'mutation($productId:ID!,$variants:[ProductVariantsBulkInput!]!){productVariantsBulkUpdate(productId:$productId,variants:$variants){productVariants{id} userErrors{field message}}}',{'productId':item['product_id'],'variants':[variant]},'productVariantsBulkUpdate')
            if entries:
                admin_exec(st,'mutation($metafields:[MetafieldsSetInput!]!){metafieldsSet(metafields:$metafields){metafields{id} userErrors{field message}}}',{'metafields':entries},'metafieldsSet')
            if clears:
                admin_exec(st,'mutation($metafields:[MetafieldIdentifierInput!]!){metafieldsDelete(metafields:$metafields){deletedMetafields{key} userErrors{field message}}}',{'metafields':clears},'metafieldsDelete')
            results.append({**item,'status':'OK'})
        except HTTPException as exc:
            results.append({**item,'status':'FAILED','error':str(exc.detail)[:400]})
        except Exception as exc:
            results.append({**item,'status':'FAILED','error':f'{type(exc).__name__}: {exc}'[:400]})
    return {**plan,'results':results,'items':[]}

def admin_audit_global(kind,payload,stores_out):
    try:
        with pool.connection() as c:
            op=c.execute("INSERT INTO admin_operations(operator,kind,mode,payload) VALUES(%s,%s,'global',%s) RETURNING id",(operator(),kind,Jsonb(payload))).fetchone()[0]
            for s in stores_out:
                if not s.get('results'):
                    c.execute("INSERT INTO admin_operation_items(op_id,store_code,entity_gid,status,error) VALUES(%s,%s,NULL,%s,%s)",(op,s['store'],'SKIPPED' if s.get('blocked') else 'FAILED',(s.get('error') or '; '.join(e['message'] for e in s.get('exceptions',[])))[:500]))
                for r in s.get('results',[]):
                    c.execute("INSERT INTO admin_operation_items(op_id,store_code,entity_gid,status,error,before,after) VALUES(%s,%s,%s,%s,%s,%s,%s)",(op,s['store'],r['id'],r['status'],r.get('error'),Jsonb({d['label']:d['before'] for d in r['changes']}),Jsonb({d['label']:d['after'] for d in r['changes']})))
            return op
    except Exception:
        logging.getLogger('hub.admin').exception('No se pudo registrar la auditoría global')

def admin_global_collect(results,unknown):
    out=list(unknown)
    for r in results:
        out.append(r['data'] if r['ok'] else {'store':r['store'],'blocked':True,'error':r['error'],'exceptions':[{'code':'STORE_ERROR','message':r['error']}],'items':[]})
    return out

@app.post('/api/admin/global/preview')
def admin_global_preview(x:AdminGlobalRequest):
    skus,changes,stores,unknown=admin_global_prepare(x)
    results=run_per_store(stores,lambda st:admin_global_plan_store(st,x.scope,skus,changes))
    return {'stores':admin_global_collect(results,unknown)}

def admin_global_execute(scope,skus,changes,stores,only=None):
    """Plan + aplicación por tienda. `only` ({tienda: {ids}|None}) limita a lo indicado (None = toda la tienda)."""
    def run(st):
        plan=admin_global_plan_store(st,scope,skus,changes)  # se recalcula: lo aplicado coincide con el estado real de ahora
        if plan['blocked']: return plan
        keep=(only or {}).get(st['code'])
        if keep is not None: plan['items']=[i for i in plan['items'] if i['id'] in keep]
        return admin_global_apply_store(st,scope,plan,changes)
    return run_per_store(stores,run)

@app.post('/api/admin/global/apply')
def admin_global_apply(x:AdminGlobalRequest):
    skus,changes,stores,unknown=admin_global_prepare(x)
    stores_out=admin_global_collect(admin_global_execute(x.scope,skus,changes,stores),unknown)
    op=admin_audit_global('global.apply',{'scope':x.scope,'skus':skus,'stores':[s['store'] for s in stores_out],'changes':changes},stores_out)
    logging.getLogger('hub.admin').info('global.apply %s tiendas=%s skus=%d',x.scope,[s['store'] for s in stores_out],len(skus))
    return {'operation_id':op,'stores':stores_out}

def admin_dup_entry(sku,matches):
    return {'sku':sku,'count':len(matches),'items':[f"{(m.get('product') or {}).get('title','')} · {m.get('title','')}".strip(' ·') for m in matches[:6]]}

def admin_dup_report(st,skus):
    """SKU repetidos (varias variantes con el mismo SKU exacto) y SKU inexistentes de una tienda."""
    resolved=admin_resolve_skus(st,skus) if skus else {}; dups=[]; missing=0
    for sku in skus:
        m=resolved.get(sku.lower(),[])
        if not m: missing+=1
        elif len(m)>1: dups.append(admin_dup_entry(sku,m))
    return {'duplicates':dups,'missing':missing,'found':len(skus)-missing}

def admin_dup_summary(results):
    stores=[]
    for r in results:
        if r['ok']:
            d=r['data']; stores.append({'store':r['store'],**d,'dup_skus':len(d['duplicates']),'dup_variants':sum(x['count'] for x in d['duplicates'])})
        else: stores.append({'store':r['store'],'error':r['error'],'duplicates':[],'missing':0,'found':0,'dup_skus':0,'dup_variants':0})
    return stores

def admin_stores_from(codes):
    available={s['code']:s for s in shopify_stores() if s['domain'] and s['token']}
    wanted=list(dict.fromkeys(c.strip().upper() for c in codes if c.strip())) or list(available)
    chosen=[available[c] for c in wanted if c in available]
    if not chosen: raise HTTPException(400,'Elige al menos una tienda activa')
    return chosen

class AdminSkuCheck(BaseModel):
    skus:list[str]
    stores:list[str]=[]

@app.post('/api/admin/global/sku-check')
def admin_sku_check(x:AdminSkuCheck):
    skus=list(dict.fromkeys(s.strip() for s in x.skus if s.strip()))
    if not skus: raise HTTPException(400,'Indica al menos un SKU')
    if len(skus)>GLOBAL_MAX_SKUS: raise HTTPException(400,f'Máximo {GLOBAL_MAX_SKUS} SKU por comprobación')
    stores=admin_stores_from(x.stores)
    return {'skus':len(skus),'stores':admin_dup_summary(run_per_store(stores,lambda st:admin_dup_report(st,skus)))}

class AdminSkuFind(BaseModel):
    q:str=Field(min_length=2,max_length=120)
    stores:list[str]=[]

ADMIN_FIND_QUERY='query($q:String,$after:String){products(first:50,query:$q,after:$after){nodes{id title variants(first:100){nodes{sku title}} } pageInfo{hasNextPage endCursor}}}'
ADMIN_FIND_PAGES=4

def admin_find_store(st,term):
    found=[]; after=None; truncated=False
    for page_no in range(ADMIN_FIND_PAGES):
        data=admin_gql(st,ADMIN_FIND_QUERY,{'q':term,'after':after}).get('products') or {}
        for p in data.get('nodes',[]):
            for v in (p.get('variants') or {}).get('nodes',[]):
                if (v.get('sku') or '').strip(): found.append({'sku':v['sku'].strip(),'product':p['title'],'variant':v.get('title') or ''})
        info=data.get('pageInfo') or {}
        if not info.get('hasNextPage') or not info.get('endCursor'): break
        after=info['endCursor']
        if page_no==ADMIN_FIND_PAGES-1: truncated=True
    return {'found':found,'truncated':truncated}

@app.post('/api/admin/global/find')
def admin_sku_find(x:AdminSkuFind):
    """Busca productos por nombre en las tiendas elegidas y devuelve sus SKU, con el aviso de SKU repetidos."""
    stores=admin_stores_from(x.stores)
    term=' '.join(f'title:{w}*' for w in re.findall(r'[\w-]+',x.q))
    if not term: raise HTTPException(400,'Escribe parte del nombre del producto')
    results=run_per_store(stores,lambda st:admin_find_store(st,term))
    by_sku={}; errors=[]; truncated=[]
    for r in results:
        if not r['ok']: errors.append({'store':r['store'],'error':r['error']}); continue
        if r['data']['truncated']: truncated.append(r['store'])
        for f in r['data']['found']:
            e=by_sku.setdefault(f['sku'].lower(),{'sku':f['sku'],'title':f['product'],'variant':f['variant'],'stores':[]})
            if r['store'] not in e['stores']: e['stores'].append(r['store'])
    skus=sorted(by_sku.values(),key=lambda e:(e['title'].lower(),e['sku'].lower()))
    limited=len(skus)>GLOBAL_MAX_SKUS
    check=[e['sku'] for e in skus[:GLOBAL_MAX_SKUS]]
    summary=admin_dup_summary(run_per_store(stores,lambda st:admin_dup_report(st,check))) if check else []
    return {'skus':skus,'limited':limited,'max':GLOBAL_MAX_SKUS,'truncated':truncated,'errors':errors,'stores':summary}

# --- Fase 4: crear definiciones ausentes, historial y reintento de fallidos ---------------
class AdminDefinitionCreate(BaseModel):
    owner:str
    namespace:str=Field(pattern=r'^[A-Za-z0-9_-]{2,255}$')
    key:str=Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    stores:list[str]

ADMIN_DEFINITION_READ='query($owner:MetafieldOwnerType!,$ns:String!,$key:String!){metafieldDefinitions(ownerType:$owner,namespace:$ns,key:$key,first:1){nodes{name description type{name} validations{name value}}}}'
ADMIN_DEFINITION_CREATE='mutation($definition:MetafieldDefinitionInput!){metafieldDefinitionCreate(definition:$definition){createdDefinition{id} userErrors{field message}}}'

def admin_definition_read(st,owner,namespace,key):
    nodes=(admin_gql(st,ADMIN_DEFINITION_READ,{'owner':owner,'ns':namespace,'key':key}).get('metafieldDefinitions') or {}).get('nodes') or []
    return nodes[0] if nodes else None

# --- Duplicados: instancias de un SKU en todas las tiendas, con edición de SKU y título ---
DUP_MAX_CHANGES=500
ADMIN_DUP_QUERY='query($q:String!,$after:String){productVariants(first:100,query:$q,after:$after){nodes{id sku title selectedOptions{name value} product{id title status handle}} pageInfo{hasNextPage endCursor}}}'

def admin_dup_instances(st,skus):
    """Todas las variantes de la tienda cuyo SKU coincide exactamente (sin distinguir mayúsculas) con alguno de `skus`."""
    wanted={s.lower() for s in skus}; out=[]
    for i in range(0,len(skus),10):
        q=' OR '.join('sku:"%s"'%s.replace('\\','\\\\').replace('"','\\"') for s in skus[i:i+10]); after=None
        while True:
            data=admin_gql(st,ADMIN_DUP_QUERY,{'q':q,'after':after}).get('productVariants') or {}
            for n in data.get('nodes',[]):
                sku=(n.get('sku') or '').strip()
                if sku.lower() in wanted:
                    p=n.get('product') or {}
                    out.append({'variant_id':n['id'],'product_id':p.get('id'),'sku':sku,'variant_title':n.get('title') or '','options':[{'name':o['name'],'value':o['value']} for o in n.get('selectedOptions') or []],'product_title':p.get('title') or '','status':p.get('status') or '','handle':p.get('handle') or ''})
            info=data.get('pageInfo') or {}
            if not info.get('hasNextPage') or not info.get('endCursor'): break
            after=info['endCursor']
    return out

@app.post('/api/admin/duplicates/search')
def admin_duplicates_search(x:AdminSkuCheck):
    skus=list(dict.fromkeys(s.strip() for s in x.skus if s.strip()))
    if not skus: raise HTTPException(400,'Indica al menos un SKU')
    if len(skus)>GLOBAL_MAX_SKUS: raise HTTPException(400,f'Máximo {GLOBAL_MAX_SKUS} SKU por búsqueda')
    stores=admin_stores_from(x.stores)
    results=run_per_store(stores,lambda st:admin_dup_instances(st,skus))
    return {'skus':skus,'stores':[{'store':r['store'],'items':r['data']} if r['ok'] else {'store':r['store'],'items':[],'error':r['error']} for r in results]}

class AdminDupChange(BaseModel):
    store:str
    variant_id:str
    sku:str|None=None
    variant_title:str|None=None
    product_id:str|None=None
    product_title:str|None=None

class AdminDupApply(BaseModel):
    changes:list[AdminDupChange]

ADMIN_VARIANT_TITLE_READ='query($id:ID!){productVariant(id:$id){title product{id} selectedOptions{name value}}}'

def admin_variant_title_update(st,vid,new_title):
    """Cambia el título de una variante. En Shopify el título es la unión de los valores de sus opciones con ' / ',
    así que se reparte el texto entre las opciones (debe haber tantos tramos como opciones)."""
    node=admin_gql(st,ADMIN_VARIANT_TITLE_READ,{'id':vid}).get('productVariant')
    if not node: raise HTTPException(404,'Variante no encontrada en la tienda')
    opts=node.get('selectedOptions') or []
    parts=[p.strip() for p in new_title.split(' / ')] if len(opts)>1 else [new_title.strip()]
    if len(parts)!=len(opts) or not all(parts): raise HTTPException(400,f'El título debe tener {len(opts)} valores separados por " / " ({" / ".join(o["name"] for o in opts)})')
    changed={o['name']:p for o,p in zip(opts,parts) if p!=o['value']}
    if not changed: return []
    values=[{'optionName':o['name'],'name':p} for o,p in zip(opts,parts) if o['name'] in changed]
    admin_mutate(st,'variant.title',vid,'mutation($productId:ID!,$variants:[ProductVariantsBulkInput!]!){productVariantsBulkUpdate(productId:$productId,variants:$variants){productVariants{id} userErrors{field message}}}',{'productId':node['product']['id'],'variants':[{'id':vid,'optionValues':values}]},'productVariantsBulkUpdate',{o['name']:o['value'] for o in opts if o['name'] in changed},changed)
    return ['variant_title']

def admin_dup_apply_store(st,rows):
    """Aplica en una tienda los cambios de SKU y título de variante. Un fallo no detiene el resto."""
    out=[]; products_done={}
    for r in rows:
        try:
            changed=[]
            if r.product_title is not None:
                if r.product_id not in products_done: products_done[r.product_id]=admin_product_update(AdminEntityUpdate(store=st['code'],id=r.product_id,fields={'title':r.product_title}))['changed']
                if products_done[r.product_id]: changed.append('product_title')
            if r.sku is not None:
                changed+=admin_variant_update(AdminVariantUpdate(store=st['code'],id=r.variant_id,fields={'sku':r.sku}))['changed']
            if r.variant_title is not None:
                changed+=admin_variant_title_update(st,r.variant_id,r.variant_title)
            out.append({'store':st['code'],'variant_id':r.variant_id,'status':'OK' if changed else 'UNCHANGED','changed':changed})
        except HTTPException as exc:
            out.append({'store':st['code'],'variant_id':r.variant_id,'status':'FAILED','error':str(exc.detail)[:300]})
    return out

@app.post('/api/admin/duplicates/apply')
def admin_duplicates_apply(x:AdminDupApply):
    if not x.changes: raise HTTPException(400,'No hay cambios que guardar')
    if len(x.changes)>DUP_MAX_CHANGES: raise HTTPException(400,f'Máximo {DUP_MAX_CHANGES} cambios por operación')
    for r in x.changes:
        if r.sku is None and r.variant_title is None and r.product_title is None: raise HTTPException(400,'Cambio vacío')
        if r.product_title is not None and not (r.product_id or '').startswith('gid://shopify/Product/'): raise HTTPException(400,'Identificador de producto no válido')
        if r.product_title is not None and not r.product_title.strip(): raise HTTPException(400,'El título del producto no puede estar vacío')
        if not r.variant_id.startswith('gid://shopify/ProductVariant/'): raise HTTPException(400,'Identificador no válido')
    by_store={}
    for r in x.changes: by_store.setdefault(r.store.strip().upper(),[]).append(r)
    stores=admin_stores_from(list(by_store))
    missing=[c for c in by_store if c not in {s['code'] for s in stores}]
    if missing: raise HTTPException(400,'Tienda inactiva o sin credenciales: '+', '.join(missing))
    results=[]
    for r in run_per_store(stores,lambda st:admin_dup_apply_store(st,by_store[st['code']])):
        if r['ok']: results+=r['data']
        else: results+=[{'store':r['store'],'variant_id':c.variant_id,'status':'FAILED','error':r['error']} for c in by_store[r['store']]]
    return {'results':results,'ok':sum(1 for r in results if r['status']=='OK'),'failed':sum(1 for r in results if r['status']=='FAILED')}

# --- Precios: variantes de un producto en todas las tiendas, precio base y tarifas (listas de precios por mercado/catálogo) ---
PRICE_MAX_CHANGES=2000
PRICE_CHUNK=100
ADMIN_PRICE_FIND='query($q:String!){productVariants(first:20,query:$q){nodes{sku product{id}}}}'
ADMIN_PRICE_PRODUCT='query($id:ID!){product(id:$id){id title status handle variants(first:250){nodes{id sku title price compareAtPrice}}}}'
ADMIN_PRICE_LISTS='query($after:String){catalogs(first:100,after:$after){nodes{__typename title status ... on MarketCatalog{markets(first:5){nodes{name type conditions{regionsCondition{regions(first:15){nodes{... on MarketRegionCountry{code}}}}}}}} priceList{id name currency fixedPricesCount}} pageInfo{hasNextPage endCursor}}}'
ADMIN_PRICE_FIXED='query($id:ID!,$q:String,$after:String){priceList(id:$id){prices(first:250,originType:FIXED,query:$q,after:$after){edges{node{variant{id} price{amount} compareAtPrice{amount}}} pageInfo{hasNextPage endCursor}}}}'

def admin_price_lists(st):
    """Listas de precios de los mercados de la tienda (catálogos de mercado de tipo región). Se ignoran las tarifas B2B por empresa."""
    out={}; after=None
    while True:
        data=admin_gql(st,ADMIN_PRICE_LISTS,{'after':after}).get('catalogs') or {}
        for n in data.get('nodes',[]):
            pl=n.get('priceList'); markets=((n.get('markets') or {}).get('nodes')) or []
            if n.get('__typename')!='MarketCatalog' or not pl or not pl.get('currency') or pl['id'] in out: continue
            if not markets or any(m.get('type')=='COMPANY_LOCATION' for m in markets): continue
            countries=[r['code'] for m in markets for r in ((((m.get('conditions') or {}).get('regionsCondition') or {}).get('regions') or {}).get('nodes') or []) if r.get('code')]
            out[pl['id']]={'id':pl['id'],'name':' + '.join(m['name'] for m in markets),'currency':pl['currency'],'fixed':pl.get('fixedPricesCount') or 0,'countries':countries,'status':n.get('status') or ''}
        info=data.get('pageInfo') or {}
        if not info.get('hasNextPage'): break
        after=info.get('endCursor')
    lists=list(out.values()); names=[l['name'] for l in lists]
    for l in lists:
        if names.count(l['name'])>1: l['name']+=' #'+l['id'].rsplit('/',1)[-1][-4:]   # mercados con el mismo nombre: se distinguen por la lista
    return lists

def admin_price_store(st,sku):
    esc_sku=sku.replace('\\','\\\\').replace('"','\\"')
    nodes=(admin_gql(st,ADMIN_PRICE_FIND,{'q':'sku:"%s"'%esc_sku}).get('productVariants') or {}).get('nodes',[])
    pids=list(dict.fromkeys(n['product']['id'] for n in nodes if (n.get('sku') or '').strip().lower()==sku.lower() and n.get('product')))
    if not pids: return {'products':[],'lists':[]}
    products=[]
    for pid in pids:
        p=admin_gql(st,ADMIN_PRICE_PRODUCT,{'id':pid}).get('product')
        if p: products.append({'id':p['id'],'title':p['title'],'status':p['status'],'variants':[{'id':v['id'],'sku':(v.get('sku') or '').strip(),'title':v.get('title') or '','price':_dec(v.get('price')),'compareAtPrice':_dec(v.get('compareAtPrice'))} for v in p['variants']['nodes']]})
    lists=admin_price_lists(st)
    for pl in lists:
        pl['prices']={}
        if not pl['fixed']: continue
        for p in products:
            pnum=p['id'].rsplit('/',1)[-1]; after=None
            while True:
                data=((admin_gql(st,ADMIN_PRICE_FIXED,{'id':pl['id'],'q':'product_id:'+pnum,'after':after}).get('priceList') or {}).get('prices')) or {}
                for e in data.get('edges',[]):
                    n=e['node']; pl['prices'][n['variant']['id']]={'price':_dec(n['price']['amount']),'compareAtPrice':_dec((n.get('compareAtPrice') or {}).get('amount'))}
                info=data.get('pageInfo') or {}
                if not info.get('hasNextPage'): break
                after=info.get('endCursor')
    return {'products':products,'lists':lists}

class AdminPriceFind(BaseModel):
    q:str
    stores:list[str]=[]

@app.post('/api/admin/prices/find')
def admin_prices_find(x:AdminPriceFind):
    """Busca productos por nombre o SKU (en la primera tienda elegida, prefiriendo ES) y devuelve un SKU de variante por producto para cargar sus precios."""
    q=x.q.strip()
    if not q: raise HTTPException(400,'Escribe un SKU o parte del nombre del producto')
    stores=admin_stores_from(x.stores); st=next((s for s in stores if s['code']=='ES'),stores[0])
    data=admin_gql(st,ADMIN_PRODUCTS_QUERY,{'query':admin_search_term(q),'after':None})
    nodes=(data.get('products') or {}).get('nodes',[])
    exact=any((v.get('sku') or '').strip().lower()==q.lower() for n in nodes for v in n['variants']['nodes'])
    return {'store':st['code'],'exact':exact,'has_next':bool(((data.get('products') or {}).get('pageInfo') or {}).get('hasNextPage')),
            'products':[{'title':n['title'],'status':n['status'],'sku':next((v['sku'] for v in n['variants']['nodes'] if v.get('sku')),None),'variants':len(n['variants']['nodes'])} for n in nodes if any(v.get('sku') for v in n['variants']['nodes'])]}

class AdminPriceSearch(BaseModel):
    sku:str
    stores:list[str]=[]

@app.post('/api/admin/prices/search')
def admin_prices_search(x:AdminPriceSearch):
    sku=x.sku.strip()
    if not sku: raise HTTPException(400,'Indica el SKU de una variante del producto')
    stores=admin_stores_from(x.stores)
    results=run_per_store(stores,lambda st:admin_price_store(st,sku))
    return {'sku':sku,'stores':[{'store':r['store'],**r['data']} if r['ok'] else {'store':r['store'],'products':[],'lists':[],'error':r['error']} for r in results]}

class AdminPriceChange(BaseModel):
    store:str
    product_id:str
    variant_id:str
    list_id:str|None=None   # None = precio base de la tienda
    price:str
    compareAtPrice:str=''

class AdminPriceApply(BaseModel):
    changes:list[AdminPriceChange]

def _price_chunks(items,n=PRICE_CHUNK):
    for i in range(0,len(items),n): yield items[i:i+n]

def admin_price_apply_store(st,rows):
    """Aplica los cambios de precio de una tienda por grupos (producto o lista de precios). Un grupo fallido no detiene el resto."""
    out=[]
    groups={}
    for r in rows: groups.setdefault((r.list_id,r.product_id if not r.list_id else None),[]).append(r)
    for (list_id,product_id),items in groups.items():
        def done(status,error=None):
            out.extend({'store':st['code'],'variant_id':r.variant_id,'list_id':list_id,'status':status,**({'error':error} if error else {})} for r in items)
        try:
            if not list_id:
                for chunk in _price_chunks(items):
                    variants=[{'id':r.variant_id,'price':_dec(r.price),'compareAtPrice':_dec(r.compareAtPrice) or None} for r in chunk]
                    admin_mutate(st,'price.update',product_id,'mutation($productId:ID!,$variants:[ProductVariantsBulkInput!]!){productVariantsBulkUpdate(productId:$productId,variants:$variants){productVariants{id} userErrors{field message}}}',{'productId':product_id,'variants':variants},'productVariantsBulkUpdate',None,{'base':{r.variant_id:{'price':_dec(r.price),'compareAtPrice':_dec(r.compareAtPrice)} for r in chunk}})
            else:
                currency=(admin_gql(st,'query($id:ID!){priceList(id:$id){currency}}',{'id':list_id}).get('priceList') or {}).get('currency')
                if not currency: raise HTTPException(404,'Lista de precios no encontrada en la tienda')
                adds=[r for r in items if _dec(r.price)!='']; dels=[r for r in items if _dec(r.price)=='']
                if any(_dec(r.compareAtPrice)!='' for r in dels): raise HTTPException(400,'No se puede dejar precio de comparación sin precio: vacía también el de comparación')
                for chunk in _price_chunks(adds):
                    prices=[{'variantId':r.variant_id,'price':{'amount':_dec(r.price),'currencyCode':currency},'compareAtPrice':({'amount':_dec(r.compareAtPrice),'currencyCode':currency} if _dec(r.compareAtPrice)!='' else None)} for r in chunk]
                    admin_mutate(st,'price.update',list_id,'mutation($id:ID!,$prices:[PriceListPriceInput!]!){priceListFixedPricesAdd(priceListId:$id,prices:$prices){prices{variant{id}} userErrors{field message}}}',{'id':list_id,'prices':prices},'priceListFixedPricesAdd',None,{'list':{r.variant_id:{'price':_dec(r.price),'compareAtPrice':_dec(r.compareAtPrice)} for r in chunk}})
                for chunk in _price_chunks(dels):
                    admin_mutate(st,'price.update',list_id,'mutation($id:ID!,$v:[ID!]!){priceListFixedPricesDelete(priceListId:$id,variantIds:$v){deletedFixedPriceVariantIds userErrors{field message}}}',{'id':list_id,'v':[r.variant_id for r in chunk]},'priceListFixedPricesDelete',None,{'list':{r.variant_id:'sin precio fijo (hereda el base)' for r in chunk}})
            done('OK')
        except HTTPException as exc: done('FAILED',str(exc.detail)[:300])
    return out

@app.post('/api/admin/prices/apply')
def admin_prices_apply(x:AdminPriceApply):
    if not x.changes: raise HTTPException(400,'No hay cambios que guardar')
    if len(x.changes)>PRICE_MAX_CHANGES: raise HTTPException(400,f'Máximo {PRICE_MAX_CHANGES} cambios por operación')
    for r in x.changes:
        if not r.variant_id.startswith('gid://shopify/ProductVariant/') or not r.product_id.startswith('gid://shopify/Product/'): raise HTTPException(400,'Identificador no válido')
        if r.list_id is not None and not r.list_id.startswith('gid://shopify/PriceList/'): raise HTTPException(400,'Lista de precios no válida')
        if r.list_id is None and _dec(r.price)=='': raise HTTPException(400,'El precio base no puede quedar vacío')
        _dec(r.compareAtPrice)
    by_store={}
    for r in x.changes: by_store.setdefault(r.store.strip().upper(),[]).append(r)
    stores=admin_stores_from(list(by_store))
    missing=[c for c in by_store if c not in {s['code'] for s in stores}]
    if missing: raise HTTPException(400,'Tienda inactiva o sin credenciales: '+', '.join(missing))
    results=[]
    for r in run_per_store(stores,lambda st:admin_price_apply_store(st,by_store[st['code']])):
        if r['ok']: results+=r['data']
        else: results+=[{'store':r['store'],'variant_id':c.variant_id,'list_id':c.list_id,'status':'FAILED','error':r['error']} for c in by_store[r['store']]]
    return {'results':results,'ok':sum(1 for r in results if r['status']=='OK'),'failed':sum(1 for r in results if r['status']=='FAILED')}

@app.post('/api/admin/definitions/create')
def admin_definitions_create(x:AdminDefinitionCreate):
    """Copia la definición de un metacampo desde una tienda que la tiene a las tiendas indicadas que no la tienen."""
    if x.owner not in ('PRODUCT','PRODUCTVARIANT'): raise HTTPException(400,'Propietario no válido')
    available={s['code']:s for s in shopify_stores() if s['domain'] and s['token']}
    wanted=list(dict.fromkeys(c.strip().upper() for c in x.stores if c.strip()))
    if not wanted: raise HTTPException(400,'Indica al menos una tienda')
    unknown=[c for c in wanted if c not in available]
    if unknown: raise HTTPException(404,'Tiendas inactivas o sin credenciales: '+', '.join(unknown))
    source_code=source=None
    for code,st in available.items():
        if code in wanted: continue
        try: found=admin_definition_read(st,x.owner,x.namespace,x.key)
        except HTTPException: continue
        if found: source_code,source=code,found; break
    if not source: raise HTTPException(404,f'Ninguna otra tienda tiene la definición {x.namespace}.{x.key}')
    mtype=(source.get('type') or {}).get('name') or ''
    if 'reference' in mtype: raise HTTPException(400,f'No se copian definiciones de tipo {mtype}: dependen de objetos propios de cada tienda')
    definition={'name':source['name'],'namespace':x.namespace,'key':x.key,'type':mtype,'ownerType':x.owner,'validations':[{'name':v['name'],'value':v['value']} for v in source.get('validations') or []]}
    if source.get('description'): definition['description']=source['description']
    def create(st):
        if admin_definition_read(st,x.owner,x.namespace,x.key): return {'created':False,'reason':'Ya existía'}
        admin_exec(st,ADMIN_DEFINITION_CREATE,{'definition':definition},'metafieldDefinitionCreate')
        _DEFINITIONS_CACHE.pop(st['code'],None)
        return {'created':True}
    results=run_per_store([available[c] for c in wanted],create)
    for r in results:
        admin_audit('definition.create',r['store'],f'{x.owner}:{x.namespace}.{x.key}','OK' if r['ok'] else 'FAILED',r.get('error'),None,{'type':mtype,'source':source_code},{'owner':x.owner,'namespace':x.namespace,'key':x.key})
    return {'source':source_code,'type':mtype,'results':results}

@app.get('/api/admin/operations/summary')
def admin_operations_summary(limit:int=30):
    with pool.connection() as c:
        rows=c.execute("""SELECT o.id,o.created_at,o.kind,o.mode,
            count(*) FILTER (WHERE i.status='OK'),count(*) FILTER (WHERE i.status='FAILED'),count(*) FILTER (WHERE i.status='SKIPPED'),
            count(*) FILTER (WHERE i.status='FAILED' OR (i.entity_gid IS NULL AND i.status='SKIPPED')),
            COALESCE(array_agg(DISTINCT i.store_code) FILTER (WHERE i.store_code IS NOT NULL),'{}')
            FROM admin_operations o LEFT JOIN admin_operation_items i ON i.op_id=o.id GROUP BY o.id ORDER BY o.id DESC LIMIT %s""",(max(1,min(limit,100)),)).fetchall()
    return {'operations':[{'id':r[0],'at':r[1].isoformat(),'kind':r[2],'mode':r[3],'ok':r[4],'failed':r[5],'skipped':r[6],'retryable':r[2] in ('global.apply','global.retry') and r[7]>0,'stores':r[8]} for r in rows]}

@app.get('/api/admin/operations/{op_id}')
def admin_operation(op_id:int):
    with pool.connection() as c:
        op=c.execute("SELECT id,created_at,kind,mode FROM admin_operations WHERE id=%s",(op_id,)).fetchone()
        rows=c.execute("SELECT store_code,entity_gid,status,error,before,after FROM admin_operation_items WHERE op_id=%s ORDER BY id",(op_id,)).fetchall()
    if not op: raise HTTPException(404,'Operación no encontrada')
    return {'id':op[0],'at':op[1].isoformat(),'kind':op[2],'mode':op[3],'items':[{'store':r[0],'entity':r[1],'status':r[2],'error':r[3],'before':r[4],'after':r[5]} for r in rows]}

@app.post('/api/admin/operations/{op_id}/retry-failed')
def admin_retry_failed(op_id:int):
    """Reintenta solo lo que falló (o las tiendas omitidas). Se recalcula el plan, así que lo ya aplicado no se repite."""
    with pool.connection() as c:
        op=c.execute("SELECT kind,payload FROM admin_operations WHERE id=%s",(op_id,)).fetchone()
        rows=c.execute("SELECT store_code,entity_gid,status FROM admin_operation_items WHERE op_id=%s",(op_id,)).fetchall()
    if not op or op[0] not in ('global.apply','global.retry'): raise HTTPException(404,'Operación global no encontrada')
    payload=op[1]; only={}
    for store,entity,status in rows:
        if entity is None and status in ('FAILED','SKIPPED'): only[store]=None
        elif entity and status=='FAILED' and only.get(store,set()) is not None: only.setdefault(store,set()).add(entity)
    if not only: raise HTTPException(400,'No hay nada que reintentar en esta operación')
    available={s['code']:s for s in shopify_stores() if s['domain'] and s['token']}
    unknown=[{'store':c,'blocked':True,'exceptions':[{'code':'STORE_NO_CREDENTIALS','message':f'La tienda {c} no está activa o no tiene credenciales'}],'items':[]} for c in only if c not in available]
    skus=list(dict.fromkeys(payload['skus']))
    results=admin_global_execute(payload['scope'],skus,payload['changes'],[available[c] for c in only if c in available],only)
    stores_out=admin_global_collect(results,unknown)
    op_new=admin_audit_global('global.retry',{**payload,'stores':list(only),'retry_of':op_id},stores_out)
    logging.getLogger('hub.admin').info('global.retry de #%s tiendas=%s',op_id,list(only))
    return {'operation_id':op_new,'stores':stores_out}

# --- Valores de un metacampo de variante (p. ej. tiempo de entrega): listar, filtrar y cambiar en bloque ---
_MF_NS=re.compile(r'^[A-Za-z0-9_-]{2,255}$'); _MF_KEY=re.compile(r'^[A-Za-z0-9_-]{1,64}$')

def _mf_ref(namespace,key):
    if not _MF_NS.fullmatch(namespace or '') or not _MF_KEY.fullmatch(key or ''): raise HTTPException(400,'Metacampo no válido')

VALUES_PAGE_QUERY='query($after:String,$ns:String!,$key:String!){ productVariantsCount{count} productVariants(first:250,after:$after){ nodes{ id sku title product{id title status} metafield(namespace:$ns,key:$key){value} } pageInfo{hasNextPage endCursor} } }'
# Con idioma: se devuelve también la traducción del metacampo en ese idioma (y si está desactualizada).
VALUES_PAGE_QUERY_T='query($after:String,$ns:String!,$key:String!,$loc:String!){ productVariantsCount{count} productVariants(first:250,after:$after){ nodes{ id sku title product{id title status} metafield(namespace:$ns,key:$key){ id value translations(locale:$loc){ key value outdated } } } pageInfo{hasNextPage endCursor} } }'

_LOCALES_CACHE={}
def admin_store_locales(st):
    """Idiomas activos de una tienda (en caché 10 minutos)."""
    cached=_LOCALES_CACHE.get(st['code'])
    if cached and time.time()-cached[0]<600: return cached[1]
    locales=shopify_shop_locales(st); _LOCALES_CACHE[st['code']]=(time.time(),locales); return locales

def admin_translation_locale(st,locale):
    for l in admin_store_locales(st):
        if l['locale']==locale and not l.get('primary'): return l['locale']
    raise HTTPException(400,f'El idioma {locale} no está activo como traducción en {st["code"]}')

@app.get('/api/admin/translation-locales')
def admin_translation_locales():
    """Idiomas de traducción holandés/portugués activos por tienda (p. ej. ES → nl, pt-PT), para editar sus plazos aparte del valor base."""
    stores=[s for s in shopify_stores() if s['domain'] and s['token']]
    res=run_per_store(stores,lambda s:[{'locale':l['locale'],'name':l['name']} for l in admin_store_locales(s) if not l.get('primary') and is_nl_pt_locale(l['locale'])])
    return {'locales':{r['store']:r['data'] for r in res if r['ok'] and r['data']}}

@app.get('/api/admin/values/page')
def admin_values_page(store:str,namespace:str,key:str,after:str|None=None,locale:str|None=None):
    """Una página (250) de variantes de la tienda con el valor actual del metacampo (o de su traducción si se indica idioma)."""
    st=admin_store(store); _mf_ref(namespace,key)
    loc=admin_translation_locale(st,locale) if locale else None
    data=admin_gql(st,VALUES_PAGE_QUERY_T if loc else VALUES_PAGE_QUERY,{'after':after,'ns':namespace,'key':key,**({'loc':loc} if loc else {})})
    page=data.get('productVariants') or {}; info=page.get('pageInfo') or {}
    def row(n):
        mf=n.get('metafield') or {}
        out={'id':n['id'],'sku':n.get('sku') or '','title':n.get('title') or '','product_id':n['product']['id'],'product':n['product']['title'],'product_status':n['product'].get('status') or '','value':mf.get('value') or ''}
        if loc:
            tr=next((t for t in mf.get('translations') or [] if t.get('key')=='value'),None)
            out.update({'base':mf.get('value') or '','value':(tr or {}).get('value') or '','outdated':bool(tr and tr.get('outdated')),'nobase':not mf.get('id')})
        return out
    out={'store':st['code'],'locale':loc,'total':(data.get('productVariantsCount') or {}).get('count'),'has_next':bool(info.get('hasNextPage')),'cursor':info.get('endCursor'),'variants':[row(n) for n in page.get('nodes',[])]}
    if after is None:
        try: defn=metafield_definitions(st['code']).get(('PRODUCTVARIANT',namespace,key))
        except HTTPException: defn=None
        out['definition']={'type':defn['type']} if defn else None
    return out

@app.get('/api/admin/collection/product-ids')
def admin_collection_product_ids(store:str,id:str):
    st=admin_store(store)
    if not id.startswith('gid://shopify/Collection/'): raise HTTPException(400,'Identificador de colección no válido')
    ids=[]; after=None
    for _ in range(60):
        page=((admin_gql(st,'query($id:ID!,$after:String){collection(id:$id){products(first:250,after:$after){nodes{id} pageInfo{hasNextPage endCursor}}}}',{'id':id,'after':after}).get('collection') or {}).get('products')) or {}
        ids+=[n['id'] for n in page.get('nodes',[])]; info=page.get('pageInfo') or {}
        if not info.get('hasNextPage') or not info.get('endCursor'): break
        after=info['endCursor']
    return {'ids':ids}

class AdminValuesApply(BaseModel):
    store:str
    namespace:str
    key:str
    value:str=''
    ids:list[str]
    operation_id:int|None=None
    clear:bool=False
    exact:bool=False  # al deshacer: se restaura el valor tal cual, sin recortar espacios
    locale:str|None=None  # si se indica, se edita la traducción del metacampo en ese idioma y no el valor base

VALUES_CURRENT_QUERY='query($ids:[ID!]!,$ns:String!,$key:String!){ nodes(ids:$ids){ ... on ProductVariant { id metafield(namespace:$ns,key:$key){ value } } } }'
VALUES_CURRENT_QUERY_T='query($ids:[ID!]!,$ns:String!,$key:String!,$loc:String!){ nodes(ids:$ids){ ... on ProductVariant { id metafield(namespace:$ns,key:$key){ id value translations(locale:$loc){ key value } } } } }'
VALUES_DIGEST_QUERY='query($ids:[ID!]!){ translatableResourcesByIds(resourceIds:$ids,first:50){ nodes{ resourceId translatableContent{ key digest } } } }'
VALUES_DELETE_MUTATION='mutation($metafields:[MetafieldIdentifierInput!]!){metafieldsDelete(metafields:$metafields){deletedMetafields{key namespace ownerId} userErrors{field message}}}'
VALUES_SET_MUTATION='mutation($metafields:[MetafieldsSetInput!]!){metafieldsSet(metafields:$metafields){metafields{id} userErrors{field message}}}'

def _values_finish(st,x,label,value,results):
    """Registra la auditoría y devuelve el resumen común. results = [(id, estado, error, valor_anterior)]."""
    op=x.operation_id
    try:
        with pool.connection() as c:
            if op is None or not c.execute("SELECT 1 FROM admin_operations WHERE id=%s AND kind='values.apply'",(op,)).fetchone():
                op=c.execute("INSERT INTO admin_operations(operator,kind,mode,payload) VALUES(%s,'values.apply','single',%s) RETURNING id",(operator(),Jsonb({'store':st['code'],'metafield':label,'value':value}))).fetchone()[0]
            for vid,status,error,before in results:
                c.execute("INSERT INTO admin_operation_items(op_id,store_code,entity_gid,status,error,before,after) VALUES(%s,%s,%s,%s,%s,%s,%s)",(op,st['code'],vid,status,error,Jsonb({label:before}) if before is not None else None,Jsonb({label:value}) if status=='OK' else None))
    except Exception:
        logging.getLogger('hub.admin').exception('No se pudo registrar la auditoría')
    logging.getLogger('hub.admin').info('values.apply %s %s variantes=%d',st['code'],label,len(results))
    return {'operation_id':op,'applied':sum(1 for r in results if r[1]=='OK'),'skipped':sum(1 for r in results if r[1]=='SKIPPED'),'failed':[{'id':r[0],'error':r[2]} for r in results if r[1]=='FAILED']}

def _values_apply_translation(st,x,ids,value):
    """Escribe (o quita) la TRADUCCIÓN del metacampo en un idioma. Requiere que la variante tenga valor base."""
    loc=admin_translation_locale(st,x.locale); label=f'{x.namespace}.{x.key} [{loc}]'
    current={}
    for i in range(0,len(ids),50):
        for n in admin_gql(st,VALUES_CURRENT_QUERY_T,{'ids':ids[i:i+50],'ns':x.namespace,'key':x.key,'loc':loc}).get('nodes') or []:
            if not n: continue
            mf=n.get('metafield') or {}; tr=next((t for t in mf.get('translations') or [] if t.get('key')=='value'),None)
            current[n['id']]=(mf.get('id'),(tr or {}).get('value') or '')
    results=[]; todo=[]
    for vid in ids:
        if vid not in current: results.append((vid,'FAILED','La variante ya no existe',None))
        elif not current[vid][0]: results.append((vid,'FAILED','Sin valor base: no hay metacampo que traducir',None))
        elif current[vid][1]==value: results.append((vid,'SKIPPED',None,current[vid][1]))
        else: todo.append(vid)
    digests={}
    if not x.clear and todo:
        mids=list(dict.fromkeys(current[v][0] for v in todo))
        for i in range(0,len(mids),50):
            for n in (admin_gql(st,VALUES_DIGEST_QUERY,{'ids':mids[i:i+50]}).get('translatableResourcesByIds') or {}).get('nodes',[]):
                d=next((c['digest'] for c in n.get('translatableContent') or [] if c.get('key')=='value'),None)
                if d: digests[n['resourceId']]=d
    def run_batch(batch):
        out=[]; decl=[]; body=[]; var={}; idx=[]
        for vid in batch:
            mid=current[vid][0]; i=len(idx)
            if x.clear:
                decl.append(f'$r{i}:ID!'); var[f'r{i}']=mid
                body.append(f'a{i}:translationsRemove(resourceId:$r{i},translationKeys:["value"],locales:[{json.dumps(loc)}]){{ userErrors{{ field message }} }}')
            else:
                if mid not in digests: out.append((vid,'FAILED','Shopify no ofrece contenido traducible para esta variante',current[vid][1])); continue
                decl.append(f'$r{i}:ID!,$t{i}:[TranslationInput!]!'); var[f'r{i}']=mid
                var[f't{i}']=[{'locale':loc,'key':'value','value':value,'translatableContentDigest':digests[mid]}]
                body.append(f'a{i}:translationsRegister(resourceId:$r{i},translations:$t{i}){{ userErrors{{ field message }} }}')
            idx.append(vid)
        if not body: return out
        try:
            data=admin_gql(st,'mutation('+','.join(decl)+'){ '+' '.join(body)+' }',var)
            for i,vid in enumerate(idx):
                errs=((data.get(f'a{i}') or {}).get('userErrors')) or []
                out.append((vid,'FAILED' if errs else 'OK','; '.join(e.get('message','error') for e in errs)[:300] if errs else None,current[vid][1]))
        except HTTPException as exc:
            out+=[(vid,'FAILED',str(exc.detail)[:300],current[vid][1]) for vid in idx]
        return out
    batches=[todo[i:i+10] for i in range(0,len(todo),10)]
    if batches:
        with ThreadPoolExecutor(max_workers=min(4,len(batches))) as ex:
            for res in ex.map(run_batch,batches): results+=res
    return _values_finish(st,x,label,value,results)

@app.post('/api/admin/values/apply')
def admin_values_apply(x:AdminValuesApply):
    """Asigna un valor a una lista de variantes (máx. 250 por llamada). Las que ya lo tienen se omiten; un fallo no detiene al resto."""
    st=admin_store(x.store); _mf_ref(x.namespace,x.key)
    ids=list(dict.fromkeys(x.ids))
    if not ids or len(ids)>250: raise HTTPException(400,'Indica entre 1 y 250 variantes por llamada')
    if any(not i.startswith('gid://shopify/ProductVariant/') for i in ids): raise HTTPException(400,'Identificador de variante no válido')
    if not x.clear and not x.value.strip(): raise HTTPException(400,'Indica el nuevo valor')
    defn=metafield_definitions(st['code']).get(('PRODUCTVARIANT',x.namespace,x.key))
    if not defn: raise HTTPException(409,f'El metacampo {x.namespace}.{x.key} no existe en {st["code"]}')
    value='' if x.clear else admin_metafield_value(defn['type'],x.value if x.exact else x.value.strip())
    if x.locale: return _values_apply_translation(st,x,ids,value)
    current={}
    for i in range(0,len(ids),50):
        for n in admin_gql(st,VALUES_CURRENT_QUERY,{'ids':ids[i:i+50],'ns':x.namespace,'key':x.key}).get('nodes') or []:
            if n: current[n['id']]=(n.get('metafield') or {}).get('value') or ''
    label=f'{x.namespace}.{x.key}'; results=[]; todo=[]
    for vid in ids:
        if vid not in current: results.append((vid,'FAILED','La variante ya no existe',None))
        elif current[vid]==value: results.append((vid,'SKIPPED',None,current[vid]))
        else: todo.append(vid)
    def entry(vid): return {'ownerId':vid,'namespace':x.namespace,'key':x.key} if x.clear else {'ownerId':vid,'namespace':x.namespace,'key':x.key,'type':defn['type'],'value':value}
    mutation,root=(VALUES_DELETE_MUTATION,'metafieldsDelete') if x.clear else (VALUES_SET_MUTATION,'metafieldsSet')
    def apply_batch(batch):
        try:
            admin_exec(st,mutation,{'metafields':[entry(v) for v in batch]},root)
            return [(v,'OK',None) for v in batch]
        except HTTPException:
            out=[]  # el lote es atómico: se repite uno a uno para saber cuáles fallan
            for v in batch:
                try: admin_exec(st,mutation,{'metafields':[entry(v)]},root); out.append((v,'OK',None))
                except HTTPException as exc: out.append((v,'FAILED',str(exc.detail)[:300]))
            return out
    batches=[todo[i:i+25] for i in range(0,len(todo),25)]
    if batches:
        with ThreadPoolExecutor(max_workers=min(4,len(batches))) as ex:
            for res in ex.map(apply_batch,batches):
                for vid,status,error in res: results.append((vid,status,error,current.get(vid)))
    return _values_finish(st,x,label,value,results)

# --- Categorías: vínculo permanente entre colecciones homólogas y control de catálogo (fase 1, solo lectura en Shopify) ---
CATEGORY_MASTER='ES'
CATEGORY_GID_RE=re.compile(r'^gid://shopify/Collection/(\d{1,20})$')
CATEGORY_SUGGEST_SKUS=25

def category_gid(value,label='colección'):
    m=CATEGORY_GID_RE.match((value or '').strip())
    if not m: raise HTTPException(400,f'Identificador de {label} no válido')
    return m.group(0),m.group(1)

# Tiendas que de momento no entran en la gestión de categorías (no figuran en el briefing).
CATEGORY_EXCLUDED_STORES={'B2B','EU','WEB'}

def category_other_stores():
    return [s for s in shopify_stores() if s['domain'] and s['token'] and s['code']!=CATEGORY_MASTER and s['code'] not in CATEGORY_EXCLUDED_STORES]

def category_target_store(code):
    code=(code or '').strip().upper()
    if code==CATEGORY_MASTER: raise HTTPException(400,'La tienda maestra no tiene homóloga')
    if code in CATEGORY_EXCLUDED_STORES: raise HTTPException(400,f'La tienda {code} no está incluida en la gestión de categorías')
    return admin_store(code)

def category_counts_query(numeric_ids):
    """Un solo viaje por tienda: total y activos de varias colecciones (productos activos, no borradores ni archivados)."""
    parts=[]
    for n in numeric_ids:
        parts.append(f'a{n}:productsCount(query:"collection_id:{n} AND status:active",limit:10000){{count precision}} t{n}:productsCount(query:"collection_id:{n}",limit:10000){{count}}')
    return 'query{'+' '.join(parts)+'}'

def category_counts(st,numeric_ids):
    if not numeric_ids: return {}
    data=admin_gql(st,category_counts_query(numeric_ids))
    return {n:{'active':(data.get(f'a{n}') or {}).get('count'),'total':(data.get(f't{n}') or {}).get('count')} for n in numeric_ids}

@app.get('/api/admin/categories/masters')
def category_masters(q:str='',after:str|None=None):
    """Colecciones de la tienda maestra con el estado de sus vínculos (confirmados / sin homóloga / pendientes)."""
    base=admin_collections(CATEGORY_MASTER,q,after)
    total=len(category_other_stores())
    gids=[c['id'] for c in base['collections']]
    stats={}
    if gids:
        with pool.connection() as c:
            for gid,confirmed,none in c.execute("SELECT master_gid,count(*) FILTER (WHERE status='CONFIRMED'),count(*) FILTER (WHERE status='NONE') FROM category_links WHERE master_gid=ANY(%s) GROUP BY master_gid",(gids,)).fetchall():
                stats[gid]=(confirmed,none)
    for col in base['collections']:
        confirmed,none=stats.get(col['id'],(0,0))
        col.update({'confirmed':confirmed,'none':none,'pending':max(0,total-confirmed-none),'stores':total})
    return base

@app.get('/api/admin/categories/group')
def category_group(master:str):
    """Una categoría maestra con su homóloga por tienda, estado del vínculo y productos activos frente a la maestra."""
    master_gid,master_id=category_gid(master,'colección maestra')
    ms=admin_store(CATEGORY_MASTER)
    node=admin_gql(ms,'query($id:ID!){collection(id:$id){id title handle}}',{'id':master_gid}).get('collection')
    if not node: raise HTTPException(404,'La colección maestra no existe en '+CATEGORY_MASTER)
    with pool.connection() as c:
        links={r[0]:{'collection':r[1],'status':r[2],'by':r[3],'at':r[4].isoformat()} for r in c.execute("SELECT store_code,collection_gid,status,linked_by,linked_at FROM category_links WHERE master_gid=%s",(master_gid,)).fetchall()}
    def row(st):
        if st['code']==CATEGORY_MASTER:
            counts=category_counts(st,[master_id])[master_id]
            return {'status':'MASTER','collection':node,**counts}
        link=links.get(st['code'])
        if not link: return {'status':'PENDING'}
        if link['status']=='NONE': return {'status':'NONE','by':link['by'],'at':link['at']}
        gid,numeric=category_gid(link['collection'])
        found=admin_gql(st,'query($id:ID!){collection(id:$id){id title handle}}',{'id':gid}).get('collection')
        if not found: return {'status':'BROKEN','collection':{'id':gid,'title':'','handle':''},'by':link['by'],'at':link['at']}
        return {'status':'LINKED','collection':found,**category_counts(st,[numeric])[numeric],'by':link['by'],'at':link['at']}
    stores=[ms]+category_other_stores()
    results=run_per_store(stores,row)
    rows=[]; base=None
    for r in results:
        item={'store':r['store'],**(r['data'] if r['ok'] else {'status':'ERROR','error':r['error']})}
        if item['store']==CATEGORY_MASTER: base=item.get('active')
        rows.append(item)
    for item in rows:
        if item.get('active') is not None and base is not None and item['status']!='MASTER': item['diff']=item['active']-base
    return {'master':node,'stores':rows}

@app.get('/api/admin/categories/suggest')
def category_suggest(master:str,store:str):
    """Candidatas a homóloga: colecciones de la tienda destino que contienen los mismos SKU que la maestra. Solo orienta; nunca vincula."""
    master_gid,_=category_gid(master,'colección maestra')
    ms=admin_store(CATEGORY_MASTER); st=category_target_store(store); store=st['code']
    data=admin_gql(ms,'query($id:ID!){collection(id:$id){products(first:%d){nodes{variants(first:1){nodes{sku}}}}}}'%CATEGORY_SUGGEST_SKUS,{'id':master_gid}).get('collection')
    if data is None: raise HTTPException(404,'La colección maestra no existe en '+CATEGORY_MASTER)
    skus=[v['sku'] for p in data['products']['nodes'] for v in p['variants']['nodes'] if (v.get('sku') or '').strip()]
    if not skus: return {'sampled':0,'matched':0,'candidates':[]}
    term=' OR '.join('sku:"%s"'%s.replace('\\','\\\\').replace('"','\\"') for s in skus)
    nodes=(admin_gql(st,'query($q:String){products(first:50,query:$q){nodes{collections(first:30){nodes{id title handle productsCount{count}}}}}}',{'q':term}).get('products') or {}).get('nodes',[])
    tally={}
    for p in nodes:
        for col in p['collections']['nodes']:
            t=tally.setdefault(col['id'],{'id':col['id'],'title':col['title'],'handle':col['handle'],'total':(col.get('productsCount') or {}).get('count'),'hits':0}); t['hits']+=1
    with pool.connection() as c:
        taken={r[0]:r[1] for r in c.execute("SELECT collection_gid,master_gid FROM category_links WHERE store_code=%s AND collection_gid IS NOT NULL",(store,)).fetchall()}
    matched=len(nodes)
    # Cobertura x precisión: una colección comodín ("Todos", "Bestseller") cubre todo pero tiene demasiados productos, y sale peor.
    for t in tally.values(): t['score']=round((t['hits']/matched)*(t['hits']/t['total']),3) if matched and t['total'] else 0
    cands=sorted(tally.values(),key=lambda t:(-t['score'],-t['hits']))[:6]
    for t in cands: t['taken']=taken.get(t['id'])!=None and taken.get(t['id'])!=master_gid
    return {'sampled':len(skus),'matched':matched,'candidates':cands}

# --- Categorías, fase 2: comparador de SKU frente a la maestra (solo lectura) ---
CATEGORY_SKU_PRODUCTS='query($q:String!,$after:String){products(first:50,query:$q,after:$after){nodes{id variants(first:100){nodes{sku} pageInfo{hasNextPage endCursor}}} pageInfo{hasNextPage endCursor}}}'
CATEGORY_SKU_MAX_PAGES=40
CATEGORY_SKU_RESOLVE_MAX=200
CATEGORY_DETAIL={'NOT_IN_STORE':'No existe en la tienda (posible ausencia comercial)','OUTSIDE_COLLECTION':'Existe y está activo, pero fuera de la colección','NOT_ACTIVE':'Existe en la tienda, pero no está activo','ELSEWHERE_IN_ES':'Existe en ES, pero fuera de esta categoría','NOT_IN_ES':'No existe en ES','UNCHECKED':'Sin comprobar (demasiados SKU)'}

def category_active_skus(st,numeric_id):
    """({sku_en_minúsculas: sku}, truncado) de todas las variantes de los productos ACTIVOS de una colección."""
    out={}; after=None; truncated=False
    for page_no in range(CATEGORY_SKU_MAX_PAGES):
        data=admin_gql(st,CATEGORY_SKU_PRODUCTS,{'q':f'collection_id:{numeric_id} AND status:active','after':after}).get('products') or {}
        for p in data.get('nodes',[]):
            variants=p.get('variants') or {}
            for v in variants.get('nodes',[]):
                if (v.get('sku') or '').strip(): out.setdefault(v['sku'].strip().lower(),v['sku'].strip())
            info=variants.get('pageInfo') or {}
            while info.get('hasNextPage') and info.get('endCursor'):  # producto con más de 100 variantes
                more=((admin_gql(st,'query($id:ID!,$after:String){product(id:$id){variants(first:100,after:$after){nodes{sku} pageInfo{hasNextPage endCursor}}}}',{'id':p['id'],'after':info['endCursor']}).get('product') or {}).get('variants')) or {}
                for v in more.get('nodes',[]):
                    if (v.get('sku') or '').strip(): out.setdefault(v['sku'].strip().lower(),v['sku'].strip())
                info=more.get('pageInfo') or {}
        page=data.get('pageInfo') or {}
        if not page.get('hasNextPage') or not page.get('endCursor'): break
        after=page['endCursor']
        if page_no==CATEGORY_SKU_MAX_PAGES-1: truncated=True
    return out,truncated

def category_sku_status(st,skus):
    """{sku_en_minúsculas: estado del producto} para los SKU que existen en la tienda (el mejor estado si se repite)."""
    rank={'ACTIVE':0,'DRAFT':1,'ARCHIVED':2}; found={}
    query='query($q:String!,$after:String){productVariants(first:100,query:$q,after:$after){nodes{sku product{status}} pageInfo{hasNextPage endCursor}}}'
    for i in range(0,len(skus),10):
        q=' OR '.join('sku:"%s"'%x.replace('\\','\\\\').replace('"','\\"') for x in skus[i:i+10]); after=None
        while True:
            data=admin_gql(st,query,{'q':q,'after':after}).get('productVariants') or {}
            for n in data.get('nodes',[]):
                key=(n.get('sku') or '').strip().lower(); status=(n.get('product') or {}).get('status') or 'DRAFT'
                if key and (key not in found or rank.get(status,9)<rank.get(found[key],9)): found[key]=status
            info=data.get('pageInfo') or {}
            if not info.get('hasNextPage') or not info.get('endCursor'): break
            after=info['endCursor']
    return found

def category_sku_diff(master_gid,store_code):
    """Compara los SKU de los productos activos de la categoría maestra con los de su homóloga vinculada."""
    master_gid,master_id=category_gid(master_gid,'colección maestra')
    st=category_target_store(store_code)
    with pool.connection() as c:
        link=c.execute("SELECT collection_gid,status FROM category_links WHERE master_gid=%s AND store_code=%s",(master_gid,st['code'])).fetchone()
    if not link: raise HTTPException(400,f'La categoría no está vinculada en {st["code"]}')
    if link[1]=='NONE': raise HTTPException(400,f'{st["code"]} está marcada sin homóloga')
    _,target_id=category_gid(link[0])
    ms=admin_store(CATEGORY_MASTER)
    (m,m_trunc),(t,t_trunc)=category_active_skus(ms,master_id),category_active_skus(st,target_id)
    missing=sorted(set(m)-set(t)); extra=sorted(set(t)-set(m)); match=sorted(set(m)&set(t))
    rows=[]; unchecked=0
    in_store=category_sku_status(st,[m[k] for k in missing[:CATEGORY_SKU_RESOLVE_MAX]])
    for i,k in enumerate(missing):
        if i>=CATEGORY_SKU_RESOLVE_MAX: code='UNCHECKED'; unchecked+=1
        elif k not in in_store: code='NOT_IN_STORE'
        else: code='OUTSIDE_COLLECTION' if in_store[k]=='ACTIVE' else 'NOT_ACTIVE'
        rows.append({'sku':m[k],'result':'MISSING','code':code,'detail':CATEGORY_DETAIL[code]})
    in_es=category_sku_status(ms,[t[k] for k in extra[:CATEGORY_SKU_RESOLVE_MAX]])
    for i,k in enumerate(extra):
        if i>=CATEGORY_SKU_RESOLVE_MAX: code='UNCHECKED'; unchecked+=1
        else: code='ELSEWHERE_IN_ES' if k in in_es else 'NOT_IN_ES'
        rows.append({'sku':t[k],'result':'EXTRA','code':code,'detail':CATEGORY_DETAIL[code]})
    rows+=[{'sku':m[k],'result':'MATCH','code':'','detail':''} for k in match]
    return {'store':st['code'],'summary':{'master':len(m),'target':len(t),'match':len(match),'missing':len(missing),'extra':len(extra)},'rows':rows,'truncated':m_trunc or t_trunc,'unchecked':unchecked}

@app.get('/api/admin/categories/skus')
def category_skus(master:str,store:str):
    return category_sku_diff(master,store)

CATEGORY_CSV_RESULT={'MISSING':'Falta en destino','EXTRA':'Solo en destino'}

def category_csv_cell(value):
    value=str(value or '')
    return "'"+value if value[:1] in ('=','+','-','@','\t','\r') else value  # evita que Excel interprete un SKU como fórmula

@app.get('/api/admin/categories/skus.csv')
def category_skus_csv(master:str,store:str=''):
    """Incidencias (faltan y sobran) de una tienda o de todas las vinculadas, en CSV para Excel (UTF-8 con BOM, separador ;)."""
    import csv, io
    master_gid,_=category_gid(master,'colección maestra')
    title=(admin_gql(admin_store(CATEGORY_MASTER),'query($id:ID!){collection(id:$id){title}}',{'id':master_gid}).get('collection') or {}).get('title','')
    if store.strip(): codes=[store]
    else:
        with pool.connection() as c:
            codes=[r[0] for r in c.execute("SELECT store_code FROM category_links WHERE master_gid=%s AND status='CONFIRMED' ORDER BY store_code",(master_gid,)).fetchall() if r[0] not in CATEGORY_EXCLUDED_STORES]
        if not codes: raise HTTPException(400,'La categoría no tiene ninguna homóloga vinculada')
    results=run_per_store([{'code':code.strip().upper()} for code in codes],lambda s:category_sku_diff(master_gid,s['code']))
    if store.strip() and not results[0]['ok']: raise HTTPException(400,results[0]['error'])
    out=io.StringIO(); w=csv.writer(out,delimiter=';',lineterminator='\r\n'); w.writerow(['Categoría ES','Tienda','SKU','Resultado','Detalle'])
    for r in results:
        if not r['ok']: w.writerow([category_csv_cell(title),r['store'],'','Error',category_csv_cell(r['error'])]); continue
        for row in r['data']['rows']:
            if row['result']!='MATCH': w.writerow([category_csv_cell(title),r['store'],category_csv_cell(row['sku']),CATEGORY_CSV_RESULT[row['result']],row['detail']])
    import unicodedata
    name=re.sub(r'[^A-Za-z0-9_-]+','_',unicodedata.normalize('NFKD',title).encode('ascii','ignore').decode()).strip('_') or 'categoria'
    return Response('\ufeff'+out.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="incidencias_sku_{name}.csv"'})

# --- Categorías, fase 3: edición de campos en las homólogas (vista previa, confirmación y auditoría) ---
CATEGORY_EDIT_FIELDS={
    'image':{'label':'Imagen de portada'},
    'icon':{'label':'Icono de categoría','key':'icono_navegacion','type':'file_reference'},
    'parent':{'label':'Miga de pan / colección padre','key':'miga_de_pan_colecci_n_padre','type':'collection_reference'},
    'related':{'label':'Categorías relacionadas','key':'colecciones_relacionadas','type':'list.collection_reference'},
    'inspiration':{'label':'«Necesitas más inspiración»','key':'necesitas_m_s_inspiraci_n','type':'list.collection_reference'},
    'rules':{'label':'Condiciones (se añaden reglas de etiqueta y tipo)'},
}
CATEGORY_REF_FIELDS=('parent','related','inspiration')
CATEGORY_RULE_COLUMNS={'TAG','TYPE'}  # las reglas por título dependen del idioma y no se copian
CATEGORY_NS='custom'
CATEGORY_EDIT_READ='''query($id:ID!){collection(id:$id){id title handle image{url altText}
 ruleSet{appliedDisjunctively rules{column relation condition conditionObject{__typename ... on CollectionRuleMetafieldCondition{metafieldDefinition{id}}}}}
 icon:metafield(namespace:"custom",key:"icono_navegacion"){value reference{... on MediaImage{id image{url}}}}
 parent:metafield(namespace:"custom",key:"miga_de_pan_colecci_n_padre"){value}
 related:metafield(namespace:"custom",key:"colecciones_relacionadas"){value}
 inspiration:metafield(namespace:"custom",key:"necesitas_m_s_inspiraci_n"){value}}
 metafieldDefinitions(ownerType:COLLECTION,first:100){nodes{namespace key type{name}}}}'''

class CategoryEdit(BaseModel):
    master:str
    stores:list[str]
    fields:list[str]
    partial_lists:bool=False   # en listas, aplicar solo las colecciones con homóloga (el resto se omite con aviso)
    confirm:bool=False
    plan_hash:str|None=None

def _basename(url):
    # Shopify añade «_<uuid>» al nombre cuando el archivo ya existía: se ignora para que reaplicar no cuente como cambio.
    name=(url or '').split('?')[0].rsplit('/',1)[-1].lower()
    return re.sub(r'_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=\.[a-z0-9]+$)','',name)

def _gid_list(value):
    try: data=json.loads(value) if value else []
    except ValueError: return []
    return [str(x) for x in data] if isinstance(data,list) else []

def category_titles(st,gids):
    out={}; gids=list(dict.fromkeys(g for g in gids if g))
    for i in range(0,len(gids),50):
        for n in admin_gql(st,'query($ids:[ID!]!){nodes(ids:$ids){... on Collection{id title}}}',{'ids':gids[i:i+50]}).get('nodes') or []:
            if n: out[n['id']]=n['title']
    return out

def category_rule_key(r):
    return (r['column'],r['relation'],(r.get('condition') or '').strip().lower())

def category_rule_text(r):
    return f"{r['column']} {r['relation']} «{r.get('condition') or ''}»"

def category_match_count(st,rules):
    """Productos que cumplen A LA VEZ todas las reglas positivas (etiqueta o tipo igual a...). Cota superior de lo que quedaría en la colección."""
    parts=[]
    for r in rules:
        if r['relation']!='EQUALS' or r['column'] not in ('TAG','TYPE'): continue
        safe=(r.get('condition') or '').replace('\\','\\\\').replace('"','\\"')
        parts.append(('tag' if r['column']=='TAG' else 'product_type')+f':"{safe}"')
    if not parts: return None
    return (admin_gql(st,'query($q:String){productsCount(query:$q,limit:10000){count}}',{'q':' AND '.join(parts)}).get('productsCount') or {}).get('count') or 0

def category_edit_plan_store(st,master_gid,src,src_titles,fields,partial):
    """Qué cambiaría en una tienda (solo lectura). Cada campo queda CHANGE (se aplicará), SAME (ya igual) o BLOCKED (con el motivo)."""
    with pool.connection() as c:
        link=c.execute("SELECT collection_gid,status FROM category_links WHERE master_gid=%s AND store_code=%s",(master_gid,st['code'])).fetchone()
        links={r[0]:(r[1],r[2]) for r in c.execute("SELECT master_gid,collection_gid,status FROM category_links WHERE store_code=%s",(st['code'],)).fetchall()}
    if not link or link[1]!='CONFIRMED':
        return {'store':st['code'],'status':'NO_LINK','reason':'Sin homóloga vinculada' if not link else 'Marcada sin homóloga','fields':[]}
    gid,_=category_gid(link[0])
    data=admin_gql(st,CATEGORY_EDIT_READ,{'id':gid}); tgt=data.get('collection')
    if not tgt: return {'store':st['code'],'status':'BROKEN','reason':'La colección vinculada ya no existe en la tienda','fields':[]}
    defs={(d['namespace'],d['key']):(d.get('type') or {}).get('name') for d in (data.get('metafieldDefinitions') or {}).get('nodes',[])}
    out=[]
    def add(field,state,before=None,after=None,warnings=None,reason=None,apply=None):
        out.append({'field':field,'label':CATEGORY_EDIT_FIELDS[field]['label'],'state':state,'before':before,'after':after,'warnings':warnings or [],'reason':reason,'apply':apply})
    for field in fields:
        spec=CATEGORY_EDIT_FIELDS[field]
        if field=='image':
            url=(src.get('image') or {}).get('url'); cur=(tgt.get('image') or {}).get('url')
            if not url: add(field,'BLOCKED',reason='La categoría de España no tiene imagen'); continue
            if _basename(url)==_basename(cur): add(field,'SAME',_basename(cur),_basename(url)); continue
            add(field,'CHANGE',_basename(cur) or '(sin imagen)',_basename(url),apply={'src':url,'alt':(tgt.get('image') or {}).get('altText')})
        elif field=='icon':
            ref=(src.get('icon') or {}).get('reference') or {}; url=(ref.get('image') or {}).get('url')
            if not url: add(field,'BLOCKED',reason='La categoría de España no tiene icono (imagen)'); continue
            if defs.get((CATEGORY_NS,spec['key']))!=spec['type']: add(field,'BLOCKED',reason=f'La tienda no tiene el metacampo {CATEGORY_NS}.{spec["key"]} ({spec["type"]})'); continue
            cur=((((tgt.get('icon') or {}).get('reference') or {}).get('image')) or {}).get('url')
            if _basename(url)==_basename(cur): add(field,'SAME',_basename(cur),_basename(url)); continue
            add(field,'CHANGE',_basename(cur) or '(sin icono)',_basename(url),apply={'src':url})
        elif field in CATEGORY_REF_FIELDS:
            raw=(src.get(field) or {}).get('value')
            is_list=spec['type'].startswith('list.'); wanted=_gid_list(raw) if is_list else ([raw] if raw else [])
            if not wanted: add(field,'BLOCKED',reason='La categoría de España no tiene valor (no se vacía el destino)'); continue
            if defs.get((CATEGORY_NS,spec['key']))!=spec['type']: add(field,'BLOCKED',reason=f'La tienda no tiene el metacampo {CATEGORY_NS}.{spec["key"]} ({spec["type"]})'); continue
            mapped=[]; missing=[]
            for m in wanted:
                l=links.get(m)
                if l and l[1]=='CONFIRMED' and l[0]: mapped.append(l[0])
                else: missing.append(f"{src_titles.get(m,m)} ({'marcada sin homóloga' if l else 'sin vincular'})")
            cur_raw=(tgt.get(field) or {}).get('value'); cur=_gid_list(cur_raw) if is_list else ([cur_raw] if cur_raw else [])
            titles=category_titles(st,mapped+cur)
            show=lambda ids:[titles.get(g,g) for g in ids]
            warn=[f'Sin homóloga en {st["code"]}: '+m for m in missing]
            if missing and not (partial and is_list and mapped):
                add(field,'BLOCKED',show(cur),show(mapped),warn,'Faltan homólogas vinculadas; vincúlalas o usa «aplicar solo las vinculadas» en listas'); continue
            if mapped==cur: add(field,'SAME',show(cur),show(mapped),warn); continue
            add(field,'CHANGE',show(cur),show(mapped),warn,apply={'value':json.dumps(mapped) if is_list else mapped[0],'type':spec['type'],'key':spec['key']})
        elif field=='rules':
            if not src.get('ruleSet'): add(field,'BLOCKED',reason='La categoría de España es manual (no tiene condiciones)'); continue
            if not tgt.get('ruleSet'): add(field,'BLOCKED',reason='La homóloga es una colección manual (no admite condiciones)'); continue
            trules=tgt['ruleSet']['rules']; have={category_rule_key(r) for r in trules}
            copyable=[r for r in src['ruleSet']['rules'] if r['column'] in CATEGORY_RULE_COLUMNS and (r.get('conditionObject') or {}).get('__typename')=='CollectionRuleTextCondition']
            skipped=[category_rule_text(r) for r in src['ruleSet']['rules'] if r not in copyable]
            warn=['No se copia (depende del idioma): '+t for t in skipped]
            if src['ruleSet']['appliedDisjunctively']!=tgt['ruleSet']['appliedDisjunctively']: warn.append('ES y la homóloga combinan las reglas de forma distinta (todas / cualquiera); no se cambia')
            new=[r for r in copyable if category_rule_key(r) not in have]
            if not new: add(field,'SAME',[category_rule_text(r) for r in trules],[],warn); continue
            before_txt=[category_rule_text(r) for r in trules]
            disj=tgt['ruleSet']['appliedDisjunctively']
            positives=[r for r in new if r['relation']=='EQUALS' and r['column'] in ('TAG','TYPE')]
            if positives and disj:
                n=category_match_count(st,positives)
                if n==0: add(field,'BLOCKED',before_txt,[category_rule_text(r) for r in new],warn,'Ningún producto de la tienda cumple la regla nueva: '+', '.join(category_rule_text(r) for r in positives)); continue
                warn.append(f'Las reglas se combinan con «cualquiera»: la nueva añadiría hasta {n} productos a la colección')
            elif positives:  # «todas»: cada regla positiva nueva restringe la colección
                after_rules=[r for r in trules]+positives
                n0=category_match_count(st,trules); n1=category_match_count(st,after_rules)
                if n1==0: add(field,'BLOCKED',before_txt,[category_rule_text(r) for r in new],warn,f'La colección quedaría vacía: ningún producto cumple a la vez todas las etiquetas/tipos requeridos (con las reglas nuevas: {", ".join(category_rule_text(r) for r in positives)}). Probablemente las etiquetas aún no están unificadas en esta tienda'); continue
                if n0 is not None and n1<n0: warn.append(f'Las reglas se combinan con «todas»: la colección se reduciría de {n0} a {n1} productos (sin contar las exclusiones)')
            keep=[]; unsupported=False
            for r in trules:
                item={'column':r['column'],'relation':r['relation'],'condition':r.get('condition') or ''}
                kind=(r.get('conditionObject') or {}).get('__typename')
                if kind=='CollectionRuleMetafieldCondition': item['conditionObjectId']=((r['conditionObject'].get('metafieldDefinition')) or {}).get('id')
                elif kind!='CollectionRuleTextCondition': unsupported=True
                keep.append(item)
            if unsupported: add(field,'BLOCKED',reason='La homóloga tiene condiciones de un tipo que esta herramienta no puede conservar'); continue
            merged=keep+[{'column':r['column'],'relation':r['relation'],'condition':r['condition']} for r in new]
            add(field,'CHANGE',[category_rule_text(r) for r in trules],[category_rule_text(r) for r in trules]+['+ '+category_rule_text(r) for r in new],warn,apply={'rules':merged,'disjunctive':tgt['ruleSet']['appliedDisjunctively']})
    return {'store':st['code'],'status':'READY','collection':{'id':tgt['id'],'title':tgt['title'],'handle':tgt['handle']},'fields':out}

def category_edit_plan(x):
    master_gid,_=category_gid(x.master,'colección maestra')
    fields=list(dict.fromkeys(x.fields)); unknown=[f for f in fields if f not in CATEGORY_EDIT_FIELDS]
    if unknown or not fields: raise HTTPException(400,'Elige al menos un campo válido'+(': '+', '.join(unknown) if unknown else ''))
    codes=list(dict.fromkeys(c.strip().upper() for c in x.stores if c.strip()))
    if not codes: raise HTTPException(400,'Elige al menos un país')
    stores=[category_target_store(c) for c in codes]
    ms=admin_store(CATEGORY_MASTER)
    src=admin_gql(ms,CATEGORY_EDIT_READ,{'id':master_gid}).get('collection')
    if not src: raise HTTPException(404,'La colección maestra no existe en '+CATEGORY_MASTER)
    refs=[g for f in CATEGORY_REF_FIELDS if f in fields for g in (_gid_list((src.get(f) or {}).get('value')) if CATEGORY_EDIT_FIELDS[f]['type'].startswith('list.') else [(src.get(f) or {}).get('value')])]
    titles=category_titles(ms,refs)
    results=run_per_store(stores,lambda st:category_edit_plan_store(st,master_gid,src,titles,fields,x.partial_lists))
    plan=[r['data'] if r['ok'] else {'store':r['store'],'status':'ERROR','reason':r['error'],'fields':[]} for r in results]
    digest=hashlib.sha256(json.dumps([(p['store'],p['status'],[(f['field'],f['state'],f['after']) for f in p['fields']]) for p in plan],sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
    return master_gid,src,plan,digest

@app.post('/api/admin/categories/edit/preview')
def category_edit_preview(x:CategoryEdit):
    """Vista previa: valores anteriores y nuevos por país y campo, y avisos. No escribe nada."""
    _,src,plan,digest=category_edit_plan(x)
    for p in plan:
        for f in p['fields']: f.pop('apply',None)
    return {'master':{'id':src['id'],'title':src['title']},'plan_hash':digest,'stores':plan}

def category_apply_field(st,gid,f):
    a=f['apply']; field=f['field']
    if field=='image':
        image={'src':a['src']}
        if a.get('alt'): image['altText']=a['alt']
        admin_exec(st,'mutation($input:CollectionInput!){collectionUpdate(input:$input){collection{id} userErrors{field message}}}',{'input':{'id':gid,'image':image}},'collectionUpdate')
    elif field=='icon':
        created=admin_exec(st,'mutation($files:[FileCreateInput!]!){fileCreate(files:$files){files{id} userErrors{field message}}}',{'files':[{'originalSource':a['src'],'contentType':'IMAGE'}]},'fileCreate')
        file_id=((created.get('files') or [{}])[0]).get('id')
        if not file_id: raise HTTPException(502,'Shopify no devolvió el archivo creado')
        for _ in range(30):  # el archivo se procesa de forma asíncrona
            status=((admin_gql(st,'query($id:ID!){node(id:$id){... on MediaImage{fileStatus}}}',{'id':file_id}).get('node')) or {}).get('fileStatus')
            if status=='READY': break
            if status=='FAILED': raise HTTPException(502,'Shopify no pudo procesar la imagen del icono')
            time.sleep(1)
        else: raise HTTPException(502,'La imagen del icono sigue procesándose; reintenta en un momento')
        admin_exec(st,'mutation($m:[MetafieldsSetInput!]!){metafieldsSet(metafields:$m){metafields{id} userErrors{field message}}}',{'m':[{'ownerId':gid,'namespace':CATEGORY_NS,'key':CATEGORY_EDIT_FIELDS['icon']['key'],'type':'file_reference','value':file_id}]},'metafieldsSet')
    elif field in CATEGORY_REF_FIELDS:
        admin_exec(st,'mutation($m:[MetafieldsSetInput!]!){metafieldsSet(metafields:$m){metafields{id} userErrors{field message}}}',{'m':[{'ownerId':gid,'namespace':CATEGORY_NS,'key':a['key'],'type':a['type'],'value':a['value']}]},'metafieldsSet')
    elif field=='rules':
        admin_exec(st,'mutation($input:CollectionInput!){collectionUpdate(input:$input){collection{id} userErrors{field message}}}',{'input':{'id':gid,'ruleSet':{'appliedDisjunctively':a['disjunctive'],'rules':a['rules']}}},'collectionUpdate')

@app.post('/api/admin/categories/edit/apply')
def category_edit_apply(x:CategoryEdit):
    """Aplica lo previsualizado. Exige confirmación expresa y que nada haya cambiado desde la vista previa."""
    if not x.confirm: raise HTTPException(400,'Falta la confirmación expresa')
    master_gid,src,plan,digest=category_edit_plan(x)
    if not x.plan_hash or x.plan_hash!=digest: raise HTTPException(409,'Los datos han cambiado desde la vista previa. Vuelve a previsualizar y confirma de nuevo.')
    def run(p):
        st=admin_store(p['store']); out=[]
        for f in p['fields']:
            if f['state']!='CHANGE': out.append({'field':f['field'],'label':f['label'],'status':'SKIPPED','reason':f.get('reason') or ('Ya estaba igual' if f['state']=='SAME' else '')}); continue
            try: category_apply_field(st,p['collection']['id'],f); out.append({'field':f['field'],'label':f['label'],'status':'OK','before':f['before'],'after':f['after']})
            except HTTPException as exc: out.append({'field':f['field'],'label':f['label'],'status':'FAILED','error':str(exc.detail)[:500],'before':f['before'],'after':f['after']})
            except Exception as exc: out.append({'field':f['field'],'label':f['label'],'status':'FAILED','error':f'{type(exc).__name__}: {exc}'[:500],'before':f['before'],'after':f['after']})
        return {'store':p['store'],'collection':p['collection'],'results':out}
    ready={p['store']:p for p in plan if p['status']=='READY'}
    done={r['store']:(r['data'] if r['ok'] else {'store':r['store'],'results':[],'error':r['error']}) for r in run_per_store([{'code':c} for c in ready],lambda s:run(ready[s['code']]))}
    stores_out=[done.get(p['store'],{'store':p['store'],'results':[],'skipped':p.get('reason') or p['status']}) for p in plan]
    op=None
    try:
        with pool.connection() as c:
            op=c.execute("INSERT INTO admin_operations(operator,kind,mode,payload) VALUES(%s,'category.edit','global',%s) RETURNING id",(operator(),Jsonb({'master':master_gid,'fields':x.fields,'stores':[p['store'] for p in plan],'partial_lists':x.partial_lists}))).fetchone()[0]
            for s in stores_out:
                if not s['results']: c.execute("INSERT INTO admin_operation_items(op_id,store_code,entity_gid,status,error) VALUES(%s,%s,NULL,%s,%s)",(op,s['store'],'SKIPPED' if s.get('skipped') else 'FAILED',(s.get('skipped') or s.get('error') or '')[:500]))
                for r in s['results']:
                    c.execute("INSERT INTO admin_operation_items(op_id,store_code,entity_gid,status,error,before,after) VALUES(%s,%s,%s,%s,%s,%s,%s)",(op,s['store'],s['collection']['id'],r['status'],r.get('error') or r.get('reason'),Jsonb({r['label']:r.get('before')}),Jsonb({r['label']:r.get('after')})))
    except Exception:
        logging.getLogger('hub.admin').exception('No se pudo registrar la auditoría de categorías')
    logging.getLogger('hub.admin').info('category.edit %s tiendas=%s campos=%s',master_gid,[p['store'] for p in plan],x.fields)
    return {'operation_id':op,'stores':stores_out}

class CategoryLink(BaseModel):
    master:str
    store:str
    collection:str|None=None   # gid de la homóloga; vacío = desvincular
    none:bool=False            # confirma que ese mercado no tiene colección equivalente

@app.post('/api/admin/categories/link')
def category_link(x:CategoryLink):
    """Guarda, cambia o elimina el vínculo de una categoría en una tienda. Solo escribe en la base de datos del hub, no en Shopify."""
    master_gid,_=category_gid(x.master,'colección maestra')
    st=category_target_store(x.store); store=st['code']
    if x.none and x.collection: raise HTTPException(400,'Elige una colección o "sin homóloga", no ambas')
    with pool.connection() as c:
        prev=c.execute("SELECT collection_gid,status FROM category_links WHERE master_gid=%s AND store_code=%s",(master_gid,store)).fetchone()
    before={'collection':prev[0],'status':prev[1]} if prev else None
    title=None
    if x.collection:
        gid,_=category_gid(x.collection)
        node=admin_gql(st,'query($id:ID!){collection(id:$id){id title}}',{'id':gid}).get('collection')
        if not node: raise HTTPException(404,f'La colección no existe en {store}')
        title=node['title']
        after={'collection':gid,'status':'CONFIRMED'}
        try:
            with pool.connection() as c:
                c.execute("INSERT INTO category_links(master_gid,store_code,collection_gid,status,linked_by) VALUES(%s,%s,%s,'CONFIRMED',%s) ON CONFLICT(master_gid,store_code) DO UPDATE SET collection_gid=EXCLUDED.collection_gid,status='CONFIRMED',linked_by=EXCLUDED.linked_by,linked_at=now()",(master_gid,store,gid,operator()))
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409,f'Esa colección de {store} ya es la homóloga de otra categoría maestra')
    elif x.none:
        after={'collection':None,'status':'NONE'}
        with pool.connection() as c:
            c.execute("INSERT INTO category_links(master_gid,store_code,collection_gid,status,linked_by) VALUES(%s,%s,NULL,'NONE',%s) ON CONFLICT(master_gid,store_code) DO UPDATE SET collection_gid=NULL,status='NONE',linked_by=EXCLUDED.linked_by,linked_at=now()",(master_gid,store,operator()))
    else:
        after=None
        with pool.connection() as c:
            c.execute("DELETE FROM category_links WHERE master_gid=%s AND store_code=%s",(master_gid,store))
    admin_audit('category.link',store,master_gid,'OK',None,before,after,{'master':master_gid,'store':store,'title':title})
    return {'store':store,'link':after,'title':title}

ADMIN_ASSETS=Path(__file__).parent
ADMIN_BODY='<style>'+(ADMIN_ASSETS/'admin.css').read_text(encoding='utf-8')+'</style>'+(ADMIN_ASSETS/'admin.html').read_text(encoding='utf-8')+'<script>'+(ADMIN_ASSETS/'admin.js').read_text(encoding='utf-8')+'</script>'

@app.get('/admin',response_class=HTMLResponse)
def admin_page():
    return shell('Administración',ADMIN_BODY,'admin')
