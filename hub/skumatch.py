"""Propuestas de emparejamiento de variantes MX con variantes de ES (maestra).

Lógica pura (sin red ni base de datos): recibe listas de variantes ya descargadas y devuelve
un candidato por variante MX. La descarga y el guardado en `sku_link_candidates` están en hub.app.

Cada variante es un dict: {product_gid, handle, product_title, status, variant_gid, sku, barcode,
variant_title, options: [valores]}.
"""
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher

HIGH = 0.9      # reglas exactas: se pueden aprobar en bloque
MEDIUM = 0.6

_PACK = re.compile(r'^\s*(\d+)\s*[xX]\s*(\S+)\s*$')
_PART = re.compile(r'^\s*(?:[xX]\s*(\d+)\s+|(\d+)\s*[xX]\s*)?([A-Za-z0-9][A-Za-z0-9_.-]{5,})\s*$')


def norm(text):
    text = unicodedata.normalize('NFKD', str(text or '').lower())
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9ø ]+', ' ', text)).strip()


def _usable(code):
    """Descarta marcadores vacíos ('0', '-', '00000'): casarían productos sin relación."""
    code = (code or '').strip()
    return len(code) >= 4 and bool(code.strip('0-_. '))


def _tokens(text):
    return {t for t in norm(text).split() if t}


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def _product_similarity(mx, es):
    if mx['handle'] and mx['handle'] == es['handle']:
        return 1.0
    title = SequenceMatcher(None, norm(mx['product_title']), norm(es['product_title'])).ratio()
    tokens = _jaccard(_tokens(mx['product_title']), _tokens(es['product_title']))
    handle = SequenceMatcher(None, mx['handle'] or '', es['handle'] or '').ratio()
    return round(max(title, tokens, handle * 0.95), 3)


def _variant_similarity(mx, es):
    mo, eo = [norm(o) for o in mx['options']], [norm(o) for o in es['options']]
    if len(mx['options']) == 1 and len(es['options']) == 1 and norm(mx['variant_title']) in ('default title', ''):
        return 1.0
    if not mo or not eo:
        return _jaccard(_tokens(mx['variant_title']), _tokens(es['variant_title']))
    exact = len(set(mo) & set(eo)) / max(len(set(mo)), len(set(eo)))
    fuzzy = _jaccard(_tokens(' '.join(mo)), _tokens(' '.join(eo)))
    return round(max(exact, fuzzy), 3)


def _label(v):
    return (v['product_title'] + ((' · ' + v['variant_title']) if v['variant_title'] and norm(v['variant_title']) != 'default title' else ''))[:240]


def _candidate_view(v, score):
    return {'sku': v['sku'], 'variant_gid': v['variant_gid'], 'title': _label(v), 'score': round(score, 3)}


def match(es_variants, mx_variants):
    """Devuelve una lista de candidatos (uno por variante MX)."""
    es_variants = [v for v in es_variants if (v['sku'] or '').strip()]
    by_sku, by_barcode = {}, {}
    es_by_product = defaultdict(list)
    for v in es_variants:
        if v['sku']:
            by_sku.setdefault(v['sku'].strip(), v)
        if v['barcode']:
            by_barcode.setdefault(v['barcode'].strip(), v)
        es_by_product[v['product_gid']].append(v)
    es_products = {}
    for v in es_variants:
        es_products.setdefault(v['product_gid'], v)
    mx_by_sku = {}
    for v in mx_variants:
        if v['sku']:
            mx_by_sku.setdefault(v['sku'].strip(), v)

    def exact(sku, barcode):
        sku, barcode = (sku or '').strip(), (barcode or '').strip()
        sku, barcode = (sku if _usable(sku) else ''), (barcode if _usable(barcode) else '')
        if sku and sku in by_sku:
            return by_sku[sku], 0.99, 'SKU idéntico en ES'
        if sku and sku in by_barcode:
            return by_barcode[sku], 0.97, 'El SKU de MX es el EAN de ES'
        if barcode and barcode in by_barcode:
            return by_barcode[barcode], 0.97, 'EAN idéntico en ES'
        if barcode and barcode in by_sku:
            return by_sku[barcode], 0.95, 'El EAN de MX es el SKU de ES'
        return None

    es_norm = {pgid: (norm(ep['product_title']), _tokens(ep['product_title']), ep['handle'] or '') for pgid, ep in es_products.items()}
    product_cache = {}

    def top_products(v):
        key = v['product_gid']
        if key not in product_cache:
            title, toks, handle = norm(v['product_title']), _tokens(v['product_title']), v['handle'] or ''
            ranked = []
            for pgid, (et, etoks, eh) in es_norm.items():
                if handle and handle == eh:
                    sim = 1.0
                else:
                    if not (toks & etoks) and SequenceMatcher(None, handle, eh).quick_ratio() < 0.6:
                        continue
                    sim = round(max(SequenceMatcher(None, title, et).ratio(), _jaccard(toks, etoks),
                                    SequenceMatcher(None, handle, eh).ratio() * 0.95), 3)
                ranked.append((sim, pgid))
            product_cache[key] = sorted(ranked, reverse=True)[:3]
        return product_cache[key]

    def fuzzy(v):
        scored = []
        for psim, pgid in top_products(v):
            if psim < 0.5:
                continue
            for ev in es_by_product[pgid]:
                vsim = _variant_similarity(v, ev)
                scored.append((round(min(0.89, 0.55 * psim + 0.45 * vsim), 3), psim, vsim, ev))
        scored.sort(key=lambda s: s[0], reverse=True)
        return scored

    base = {}   # variant_gid MX -> (es_variant, score, razón) para resolver packs que citan SKU de MX
    for v in mx_variants:
        hit = exact(v['sku'], v['barcode'])
        if hit:
            base[v['variant_gid']] = hit

    def resolve_code(code):
        """SKU/EAN citado dentro de un pack o kit -> (variante ES, score) o None."""
        code = code.strip()
        hit = exact(code, None)
        if hit:
            return hit[0], hit[1]
        mxv = mx_by_sku.get(code)
        if mxv and mxv['variant_gid'] in base:
            return base[mxv['variant_gid']][0], base[mxv['variant_gid']][1]
        done = resolved.get(code)
        if done and done['es_variant_gid'] and done['kind'] in ('single', 'refurb'):
            return es_by_gid[done['es_variant_gid']], done['score']
        return None

    es_by_gid = {v['variant_gid']: v for v in es_variants}
    resolved = {}   # SKU MX -> candidato ya calculado (para packs y kits que citan SKU de MX)

    def is_composite(v):
        sku = (v['sku'] or '').strip()
        return bool(_PACK.match(sku) or (re.search(r'\s[+-]\s|\+', sku) and len(sku) > 12))

    out = []
    for v in sorted(mx_variants, key=is_composite):
        sku = (v['sku'] or '').strip()
        cand = {'mx_variant_gid': v['variant_gid'], 'mx_product_gid': v['product_gid'], 'mx_sku': v['sku'],
                'mx_barcode': v['barcode'], 'mx_title': v['product_title'], 'mx_variant_title': v['variant_title'],
                'mx_status': v['status'], 'kind': 'single', 'es_sku': None, 'es_variant_gid': None, 'es_title': '',
                'components': [], 'score': 0.0, 'reasons': [], 'alternatives': []}
        hit = base.get(v['variant_gid'])
        pack = _PACK.match(sku)
        if hit:
            ev, score, reason = hit
            cand.update(es_sku=ev['sku'], es_variant_gid=ev['variant_gid'], es_title=_label(ev), score=score, reasons=[reason])
        elif pack:
            qty, code = int(pack.group(1)), pack.group(2)
            res = resolve_code(code)
            if res:
                ev, sc = res
                cand.update(kind='pack', es_sku=ev['sku'], es_variant_gid=ev['variant_gid'], es_title=_label(ev),
                            components=[{'sku_es': ev['sku'], 'qty': qty}], score=min(sc, 0.92), reasons=[f'Pack de {qty} × {code}'])
        elif re.search(r'\s[+-]\s|\+', sku) and len(sku) > 12:
            comps, ok, first, floor = [], True, None, 0.9
            for part in re.split(r'\s*\+\s*|\s+-\s+', sku):
                m = _PART.match(part)
                res = resolve_code(m.group(3)) if m else None
                if not res:
                    ok = False
                    continue
                qty = int(m.group(1) or m.group(2) or 1)
                floor = min(floor, res[1])
                comps.append({'sku_es': res[0]['sku'], 'qty': qty})
                first = first or res[0]
            if comps:
                cand.update(kind='bundle', components=comps, es_sku=comps[0]['sku_es'], es_variant_gid=first['variant_gid'],
                            es_title=_label(first), score=floor if ok else min(floor, 0.55),
                            reasons=['Kit: ' + ' + '.join(f"{c['qty']}×{c['sku_es']}" for c in comps)] + ([] if ok else ['Algún componente no se pudo resolver']))
        elif sku.upper().endswith('-REAC'):
            res = resolve_code(sku[:-5])
            if res:
                ev, sc = res
                cand.update(kind='refurb', es_sku=ev['sku'], es_variant_gid=ev['variant_gid'], es_title=_label(ev),
                            score=min(sc, 0.9), reasons=['Reacondicionado (-REAC) del SKU base'])
        if not cand['es_sku']:
            ranked = fuzzy(v)
            if ranked:
                score, psim, vsim, ev = ranked[0]
                cand.update(es_sku=ev['sku'], es_variant_gid=ev['variant_gid'], es_title=_label(ev), score=score,
                            reasons=[f'Producto parecido ({round(psim * 100)} %)', f'Variante parecida ({round(vsim * 100)} %)'])
                cand['alternatives'] = [_candidate_view(s[3], s[0]) for s in ranked[1:5]]
            else:
                cand['kind'] = 'none'
        elif cand['score'] < 0.99:
            ranked = fuzzy(v)
            cand['alternatives'] = [_candidate_view(s[3], s[0]) for s in ranked[:4] if s[3]['sku'] != cand['es_sku']]
        out.append(cand)
        if sku:
            resolved.setdefault(sku, cand)
    return out
