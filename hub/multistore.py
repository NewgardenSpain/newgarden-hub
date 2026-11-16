"""Utilidades para el panel de administración multitienda (/admin).

Sin dependencias de hub.app para evitar importaciones circulares: las funciones
reciben el ejecutor GraphQL como argumento.
"""
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException


def run_per_store(stores, fn, max_workers=4):
    """Ejecuta fn(store) por tienda aislando errores: una tienda fallida no detiene al resto.

    Devuelve [{store, ok, data|error}] en el orden de `stores`.
    """
    def one(store):
        try:
            return {'store': store['code'], 'ok': True, 'data': fn(store)}
        except HTTPException as exc:
            return {'store': store['code'], 'ok': False, 'error': str(exc.detail)[:500]}
        except Exception as exc:  # noqa: BLE001 - se informa por tienda
            return {'store': store['code'], 'ok': False, 'error': f'{type(exc).__name__}: {exc}'[:500]}
    if not stores:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(stores)))) as pool:
        return list(pool.map(one, stores))


def graphql_with_retry(graphql_payload, domain, token, query, variables=None, attempts=4):
    """Llama a `graphql_payload` (shopify_graphql_payload) con backoff ante THROTTLED/5xx.

    Devuelve `data`; lanza HTTPException(502) con el primer error GraphQL si persiste.
    """
    delay = 1.0
    for attempt in range(attempts):
        try:
            payload = graphql_payload(domain, token, query, variables)
        except HTTPException as exc:
            transient = any(code in str(exc.detail) for code in ('HTTP 429', 'HTTP 500', 'HTTP 502', 'HTTP 503', 'HTTP 504', 'conectar'))
            if transient and attempt < attempts - 1:
                time.sleep(delay); delay *= 2; continue
            raise
        errors = payload.get('errors') or []
        throttled = any((e.get('extensions') or {}).get('code') == 'THROTTLED' for e in errors)
        if throttled and attempt < attempts - 1:
            time.sleep(delay); delay *= 2; continue
        if errors:
            raise HTTPException(502, 'Shopify GraphQL: ' + str(errors[0].get('message', 'error'))[:250])
        return payload.get('data', {})
    raise HTTPException(502, 'Shopify GraphQL: sin respuesta tras varios intentos')


def definition_matrix(per_store):
    """Combina definiciones por tienda en una matriz.

    per_store: {code: {(owner, namespace, key): {'type', 'choices'}}}
    Devuelve [{owner, namespace, key, stores:{code:{type,choices}|None}, status}] donde
    status es 'ok', 'missing' (falta en alguna tienda) o 'type_mismatch'.
    """
    keys = sorted({k for defs in per_store.values() for k in defs})
    rows = []
    for owner, namespace, key in keys:
        cells = {code: defs.get((owner, namespace, key)) for code, defs in per_store.items()}
        present = [c for c in cells.values() if c]
        status = 'ok'
        if len(present) < len(cells):
            status = 'missing'
        elif len({c['type'] for c in present}) > 1:
            status = 'type_mismatch'
        name = next((c.get('name') for c in present if c.get('name')), '')
        rows.append({'owner': owner, 'namespace': namespace, 'key': key, 'name': name, 'stores': cells, 'status': status})
    return rows
