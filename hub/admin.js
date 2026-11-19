(() => {
'use strict';

/* ---------- utilidades ---------- */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const enc = encodeURIComponent;
const norm = s => String(s ?? '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
async function api(url, opts) {
  const r = await fetch(url, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(Array.isArray(d.detail) ? d.detail.map(e => e.msg).join('; ') : (d.detail || 'Error ' + r.status));
  return d;
}
const post = (url, body) => api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
const mem = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { /* sin almacenamiento */ } return null; };
const tag = (text, cls = '', title = '') => `<span class="tag ${cls}"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
const STATUS_ES = { ACTIVE: 'Activo', DRAFT: 'Borrador', ARCHIVED: 'Archivado' };
const STATUS_CLS = { ACTIVE: 'ok', DRAFT: 'warn', ARCHIVED: 'muted' };
const statusTag = s => tag(STATUS_ES[s] || s, STATUS_CLS[s] || '');
const table = (head, rows, cls = '') => `<div class="table-wrap"><table class="${cls}"><thead><tr>${head.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;
const OWNER_ES = { PRODUCT: 'Producto', PRODUCTVARIANT: 'Variante' };

const state = { stores: [], store: null, kind: 'products', cursor: null, rows: [], seq: 0, defs: null, defsPromise: null, editing: null };

/* ---------- pestañas principales ---------- */
const TABS = ['tienda', 'varias', 'valores', 'metacampos', 'historial'];
const shown = {};
function showTab(name, push = true) {
  if (!TABS.includes(name)) name = 'tienda';
  TABS.forEach(t => {
    const b = $('#tab-' + t), on = t === name;
    b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; $('#panel-' + t).hidden = !on;
  });
  if (push) history.replaceState(null, '', '#' + name);
  if (!shown[name]) { shown[name] = true; ({ tienda: loadList, varias: initMulti, valores: initValues, metacampos: renderMatrix, historial: loadHistory })[name](true); }
}
$$('.adm-tabs [role=tab]').forEach(b => {
  b.onclick = () => showTab(b.dataset.tab);
  b.onkeydown = e => {
    const i = TABS.indexOf(b.dataset.tab);
    const next = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : e.key === 'Home' ? 0 : e.key === 'End' ? TABS.length - 1 : null;
    if (next === null) return;
    e.preventDefault(); const t = TABS[(next + TABS.length) % TABS.length]; showTab(t); $('#tab-' + t).focus();
  };
});

/* ---------- definiciones de metacampos (compartidas) ---------- */
function loadDefs(force) {
  if (state.defsPromise && !force) return state.defsPromise;
  state.defs = force ? null : state.defs;
  state.defsPromise = api('/api/admin/metafield-definitions').then(d => { state.defs = d; return d; }).catch(err => { state.defsPromise = null; throw err; });
  return state.defsPromise;
}

/* ---------- controles de valor según el tipo del metacampo ---------- */
function richPlain(raw) {
  if (raw === '' || raw == null) return '';
  try {
    const d = JSON.parse(raw); if (d.type !== 'root') return null;
    return (d.children || []).map(p => {
      if (p.type !== 'paragraph') throw 0;
      return (p.children || []).map(c => { if (c.type !== 'text' || c.bold || c.italic) throw 0; return c.value; }).join('');
    }).join('\n');
  } catch (e) { return null; }
}
const richText = n => n.type === 'text' ? n.value : (n.children || []).map(richText).join(n.type === 'root' ? ' / ' : '');
function listValues(value) { try { const a = JSON.parse(value); return Array.isArray(a) ? a.map(String) : []; } catch (e) { return []; } }
const JSONISH = new Set(['json', 'dimension', 'weight', 'volume', 'money', 'rating', 'link', 'rich_text_field']);

function makeControl(type, { choices = null, value = '' } = {}) {
  type = type || '';
  const el = document.createElement('div'); el.className = 'ctl';
  const hint = t => `<p class="hint">${esc(t)}</p>`;
  let get;
  if (type === 'boolean') {
    el.innerHTML = '<select><option value="true">Sí</option><option value="false">No</option></select>';
    const s = $('select', el); s.value = String(value === true || value === 'true'); get = () => s.value;
  } else if (choices && choices.length && !type.startsWith('list.')) {
    const opts = choices.includes(value) || !value ? choices : [value, ...choices];
    el.innerHTML = `<select>${value ? '' : '<option value="">Elige una opción…</option>'}${opts.map(o => `<option>${esc(o)}</option>`).join('')}</select>`;
    const s = $('select', el); if (value) s.value = value; get = () => s.value;
  } else if (choices && choices.length) {
    const on = new Set(listValues(value));
    el.innerHTML = `<div class="checks">${choices.map(o => `<label><input type="checkbox" value="${esc(o)}"${on.has(o) ? ' checked' : ''}> ${esc(o)}</label>`).join('')}</div>`;
    get = () => JSON.stringify($$('input:checked', el).map(i => i.value));
  } else if (type === 'rich_text_field') {
    const plain = richPlain(value);
    el.innerHTML = `<textarea rows="4"${plain === null ? ' spellcheck="false"' : ''}>${esc(plain === null ? value : plain)}</textarea>` +
      hint(plain === null ? 'Este texto tiene formato (negritas, enlaces, listas…). Se edita como JSON para no perderlo.' : 'Texto sencillo: cada línea será un párrafo.');
    const t = $('textarea', el); get = () => t.value;
  } else if (type.startsWith('list.')) {
    el.innerHTML = `<textarea rows="4">${esc(listValues(value).join('\n'))}</textarea>${hint('Un valor por línea.')}`;
    const t = $('textarea', el); get = () => JSON.stringify(t.value.split('\n').map(x => x.trim()).filter(Boolean));
  } else if (JSONISH.has(type)) {
    el.innerHTML = `<textarea rows="4" spellcheck="false">${esc(value)}</textarea>${hint('Valor en formato JSON.')}`;
    const t = $('textarea', el); get = () => t.value;
  } else if (type === 'multi_line_text_field') {
    el.innerHTML = `<textarea rows="4">${esc(value)}</textarea>`;
    const t = $('textarea', el); get = () => t.value;
  } else {
    const kind = { url: 'url', number_integer: 'number', number_decimal: 'number', date: 'date' }[type] || 'text';
    const extra = type === 'number_integer' ? ' step="1"' : type === 'number_decimal' ? ' step="any"' : '';
    const ph = type === 'url' ? 'https://' : type === 'color' ? '#RRGGBB' : '';
    el.innerHTML = `<input type="${kind}"${extra} placeholder="${ph}" value="${esc(value)}">` +
      (type.endsWith('_reference') ? hint('Identificador de Shopify (gid://…); es distinto en cada tienda.') : '');
    const i = $('input', el); get = () => i.value;
  }
  return { el, get };
}

/* ---------- UNA TIENDA: listado ---------- */
function renderStoreChips() {
  const box = $('#store-chips');
  box.innerHTML = state.stores.map(s => `<label class="chip"><input type="radio" name="store" value="${esc(s.code)}"${s.code === state.store ? ' checked' : ''}><span title="${esc(s.domain)}">${esc(s.code)}</span></label>`).join('');
  $$('input', box).forEach(i => i.onchange = () => { state.store = i.value; mem('adm.store', i.value); closeEditor(); loadList(true); });
}
$$('[data-kind]').forEach(b => b.onclick = () => {
  state.kind = b.dataset.kind; $$('[data-kind]').forEach(x => x.setAttribute('aria-pressed', x === b));
  $('#q').placeholder = state.kind === 'products' ? 'Buscar por título o SKU' : 'Buscar por título';
  loadList(true);
});
$('#search').onsubmit = e => { e.preventDefault(); loadList(true); };
$('#more').onclick = () => loadList(false);

async function loadList(reset) {
  if (!state.store) return;
  const st = $('#list-status'), seq = ++state.seq;
  if (reset) { state.cursor = null; state.rows = []; $('#list').innerHTML = ''; $('#more').hidden = true; }
  st.textContent = 'Cargando…';
  try {
    const d = await api(`/api/admin/${state.kind}?store=${enc(state.store)}&q=${enc($('#q').value)}${state.cursor ? '&after=' + enc(state.cursor) : ''}`);
    if (seq !== state.seq) return;
    state.cursor = d.cursor; $('#more').hidden = !d.has_next;
    state.rows = state.rows.concat(d[state.kind]);
    st.textContent = state.kind === 'products' && d.total != null ? `${d.total} productos en ${state.store}` : `${state.rows.length} resultados en ${state.store}`;
    $('#list').innerHTML = state.rows.length ? (state.kind === 'products'
      ? table(['Producto', 'Estado', 'SKU', 'Stock'], state.rows.map(p => `<tr><td class="wrap-cell"><button type="button" class="row-link" data-open="${esc(p.id)}">${esc(p.title)}</button><small>${esc(p.handle)}</small></td><td class="nw">${statusTag(p.status)}</td><td class="nw">${esc(p.skus.slice(0, 3).join(', '))}${p.skus.length > 3 ? ` +${p.skus.length - 3}` : ''}</td><td class="nw">${esc(p.inventory ?? '')}</td></tr>`).join(''))
      : table(['Colección', 'Handle', 'Productos'], state.rows.map(c => `<tr><td class="wrap-cell"><button type="button" class="row-link" data-open="${esc(c.id)}">${esc(c.title)}</button></td><td class="nw">${esc(c.handle)}</td><td class="nw">${esc(c.products ?? '')}</td></tr>`).join('')))
      : '<p class="muted">No hay resultados.</p>';
  } catch (err) { if (seq === state.seq) st.textContent = err.message; }
}
$('#list').addEventListener('click', e => {
  const b = e.target.closest('[data-open]'); if (!b) return;
  state.kind === 'products' ? openProduct(b.dataset.open) : openCollection(b.dataset.open);
});

/* ---------- UNA TIENDA: editor ---------- */
const field = (id, label, value, o = {}) => `<div class="field"><label for="${id}">${label}</label>${o.area ? `<textarea id="${id}" rows="${o.rows || 8}"${o.mono ? ' spellcheck="false"' : ''}>${esc(value)}</textarea>` : `<input id="${id}" value="${esc(value)}">`}${o.hint ? `<p class="muted">${o.hint}</p>` : ''}</div>`;
function closeEditor() { state.editing = null; $('#editor-view').hidden = true; $('#list-view').hidden = false; }
$('#back').onclick = closeEditor;

function setEditorTabs(tabs, first) {
  const box = $('#ed-tabs');
  box.innerHTML = tabs.map(([k, l]) => `<button type="button" data-sec="${k}" aria-pressed="${k === first}">${l}</button>`).join('');
  const go = k => {
    $$('button', box).forEach(b => b.setAttribute('aria-pressed', b.dataset.sec === k));
    $$('.ed-sec', $('#ed-body')).forEach(s => s.hidden = s.dataset.sec !== k);
    $('#ed-savebar').hidden = !(state.editing && state.editing.formSecs.includes(k));
  };
  $$('button', box).forEach(b => b.onclick = () => go(b.dataset.sec));
  go(first);
}
function openEditor(title, sub) {
  $('#list-view').hidden = true; $('#editor-view').hidden = false;
  $('#ed-title').textContent = title; $('#ed-sub').innerHTML = sub; $('#ed-status').textContent = '';
  $('#ed-body').innerHTML = '<p class="muted">Cargando…</p>'; $('#ed-tabs').innerHTML = ''; $('#ed-savebar').hidden = true;
  $('#ed-title').focus();
}
// Formulario principal con detección de cambios
function bindForm(ids, endpoint, idKey) {
  const read = () => Object.fromEntries(Object.entries(ids).map(([k, id]) => [k, $('#' + id).value]));
  let snap = read();
  const save = $('#ed-save'), st = $('#ed-status');
  const refresh = () => { const dirty = JSON.stringify(read()) !== JSON.stringify(snap); save.disabled = !dirty; if (dirty) st.textContent = 'Hay cambios sin guardar.'; else if (st.textContent.startsWith('Hay')) st.textContent = ''; };
  Object.values(ids).forEach(id => $('#' + id).addEventListener('input', refresh));
  save.disabled = true;
  save.onclick = async () => {
    save.disabled = true; st.textContent = 'Guardando…';
    try {
      const d = await post(endpoint, { store: state.store, id: state.editing.id, fields: read() });
      snap = read(); st.textContent = d.saved ? `Guardado en ${state.store}.` : 'No había cambios.';
      if (d.saved && ids.title) $('#ed-title').textContent = $('#' + ids.title).value;
    } catch (err) { st.textContent = err.message; refresh(); }
  };
}
const counter = (id, max) => { const i = $('#' + id), p = i.parentElement.querySelector('.muted'); if (!p) return; const u = () => p.textContent = `${i.value.length} caracteres (recomendado hasta ${max})`; i.addEventListener('input', u); u(); };

function mfBlock(nodes, ownerId) {
  const box = document.createElement('div');
  if (!nodes.length) { box.innerHTML = '<p class="muted">Sin metacampos con valor.</p>'; return box; }
  nodes.forEach(m => {
    const row = document.createElement('div'); row.className = 'mf';
    row.innerHTML = `<div><div class="k">${esc(m.namespace + '.' + m.key)}</div>${tag(m.type)}</div><div class="slot"></div><div><button type="button" class="button secondary" disabled>Guardar</button> <button type="button" class="button secondary" data-clear>Vaciar</button> <span class="st muted" role="status"></span></div>`;
    const ctl = makeControl(m.type, { value: m.value }); $('.slot', row).appendChild(ctl.el);
    let initial = ctl.get(), digest = m.compareDigest; const btn = $('button', row), st = $('.st', row);
    const refresh = () => { btn.disabled = ctl.get() === initial; };
    ctl.el.addEventListener('input', refresh); ctl.el.addEventListener('change', refresh);
    btn.onclick = async () => {
      btn.disabled = true; st.textContent = 'Guardando…';
      try {
        const d = await post('/api/admin/metafield/set', { store: state.store, owner_id: ownerId, namespace: m.namespace, key: m.key, type: m.type, value: ctl.get(), compare_digest: digest || null });
        initial = ctl.get(); digest = null; st.textContent = d.saved ? 'Guardado' : 'Sin cambios';
      } catch (err) { st.textContent = err.message; refresh(); }
    };
    const clr = $('[data-clear]', row);
    clr.onclick = async () => {
      clr.disabled = true; btn.disabled = true; st.textContent = 'Vaciando…';
      try {
        await post('/api/admin/metafield/delete', { store: state.store, owner_id: ownerId, namespace: m.namespace, key: m.key });
        row.innerHTML = `<div><div class="k">${esc(m.namespace + '.' + m.key)}</div></div><div class="muted">Vaciado (metacampo eliminado)</div>`;
      } catch (err) { st.textContent = err.message; clr.disabled = false; refresh(); }
    };
    box.appendChild(row);
  });
  return box;
}
const chk = (k, l, v) => `<label><input type="checkbox" data-v="${k}"${v ? ' checked' : ''}> ${l}</label>`;
const vf = (k, l, v) => `<div class="field"><label>${l}</label><input data-v="${k}" value="${esc(v ?? '')}"></div>`;
function variantCard(v, total) {
  const x = v.extra, d = document.createElement('details'); d.className = 'variant'; d.open = total <= 3; d.dataset.id = v.id;
  const units = [['KILOGRAMS', 'Kilogramos'], ['GRAMS', 'Gramos'], ['POUNDS', 'Libras'], ['OUNCES', 'Onzas']];
  d.innerHTML = `<summary><strong>${esc(v.title)}</strong> <span class="muted">· ${esc(v.sku || 'sin SKU')}</span></summary><div>
    <div class="grid">${vf('sku', 'SKU', v.sku)}${vf('barcode', 'Código de barras', v.barcode)}</div>
    ${x ? `<div class="grid">${vf('price', 'Precio', x.price)}${vf('compareAtPrice', 'Precio de comparación', x.compareAtPrice)}${vf('cost', `Coste por artículo${x.currency ? ' (' + esc(x.currency) + ')' : ''}`, x.cost)}${vf('weight', 'Peso', x.weight)}
      <div class="field"><label>Unidad de peso</label><select data-v="weightUnit">${units.map(([u, l]) => `<option value="${u}"${u === x.weightUnit ? ' selected' : ''}>${l}</option>`).join('')}</select></div>
      <div class="field"><label>Si no hay stock</label><select data-v="inventoryPolicy"><option value="DENY"${x.inventoryPolicy === 'DENY' ? ' selected' : ''}>Dejar de vender</option><option value="CONTINUE"${x.inventoryPolicy === 'CONTINUE' ? ' selected' : ''}>Seguir vendiendo</option></select></div></div>
      <div class="checks-row">${chk('taxable', 'Cobrar impuestos', x.taxable)}${chk('tracked', 'Seguimiento de stock', x.tracked)}${chk('requiresShipping', 'Requiere envío', x.requiresShipping)}</div>` : '<p class="muted">No hay permiso para leer precios y costes; solo se pueden editar SKU y código de barras.</p>'}
    <div class="actions"><button type="button" class="button primary" data-save>Guardar variante</button><span class="muted" role="status"></span></div>
    <h4>Metacampos de la variante</h4></div>`;
  $('div', d).appendChild(mfBlock(v.metafields.nodes, v.id));
  const btn = $('[data-save]', d), st = $('[role=status]', d);
  btn.onclick = async () => {
    const fields = {}; $$('[data-v]', d).forEach(e => { fields[e.dataset.v] = e.type === 'checkbox' ? e.checked : e.value; });
    btn.disabled = true; st.textContent = 'Guardando…';
    try { const r = await post('/api/admin/variant/update', { store: state.store, id: v.id, fields }); st.textContent = r.saved ? `Guardado (${r.changed.join(', ')})` : 'Sin cambios'; }
    catch (err) { st.textContent = err.message; } finally { btn.disabled = false; }
  };
  return d;
}
const section = (key, html) => { const s = document.createElement('div'); s.className = 'ed-sec'; s.dataset.sec = key; if (html) s.innerHTML = html; return s; };

async function openProduct(id) {
  openEditor('Cargando…', '');
  try {
    const p = (await api(`/api/admin/product?store=${enc(state.store)}&id=${enc(id)}`)).product;
    state.editing = { kind: 'product', id, formSecs: ['general', 'seo'] };
    $('#ed-title').textContent = p.title; $('#ed-sub').innerHTML = `${statusTag(p.status)} <span>${esc(p.handle)}</span> · tienda ${esc(state.store)}. Los cambios se guardan directamente en Shopify.`;
    const body = $('#ed-body'); body.innerHTML = '';
    const statusOpts = ['ACTIVE', 'DRAFT', 'ARCHIVED'].map(s => `<option value="${s}"${s === p.status ? ' selected' : ''}>${STATUS_ES[s]}</option>`).join('');
    body.appendChild(section('general', `<div class="grid">${field('f-title', 'Título', p.title)}${field('f-handle', 'Handle', p.handle, { hint: 'Si lo cambias se crea una redirección.' })}${field('f-vendor', 'Proveedor', p.vendor)}${field('f-type', 'Tipo de producto', p.productType)}<div class="field"><label for="f-status">Estado</label><select id="f-status">${statusOpts}</select></div>${field('f-tags', 'Etiquetas', (p.tags || []).join(', '), { hint: 'Separadas por comas.' })}</div>${field('f-desc', 'Descripción (HTML)', p.descriptionHtml, { area: true, rows: 12, mono: true })}`));
    body.appendChild(section('seo', `${field('f-seot', 'Título SEO', (p.seo || {}).title || '', { hint: '0 caracteres' })}${field('f-seod', 'Descripción SEO', (p.seo || {}).description || '', { area: true, rows: 4, hint: '0 caracteres' })}`));
    const vs = section('variants', `<p class="muted">Cada variante se guarda por separado.</p>${p.variant_extra_error ? `<div class="callout err">No se pudieron leer precios y costes: ${esc(p.variant_extra_error)}</div>` : ''}`);
    p.variants.nodes.forEach(v => vs.appendChild(variantCard(v, p.variants.nodes.length)));
    body.appendChild(vs);
    const ms = section('meta', '<h3>Metacampos del producto</h3>'); ms.appendChild(mfBlock(p.metafields.nodes, p.id)); body.appendChild(ms);
    setEditorTabs([['general', 'General'], ['seo', 'SEO'], ['variants', `Variantes (${p.variants.nodes.length})`], ['meta', `Metacampos (${p.metafields.nodes.length})`]], 'general');
    bindForm({ title: 'f-title', handle: 'f-handle', vendor: 'f-vendor', productType: 'f-type', status: 'f-status', tags: 'f-tags', descriptionHtml: 'f-desc', seo_title: 'f-seot', seo_description: 'f-seod' }, '/api/admin/product/update');
    counter('f-seot', 60); counter('f-seod', 155);
  } catch (err) { console.error(err.stack); $('#ed-title').textContent = 'No se pudo abrir'; $('#ed-body').innerHTML = `<div class="callout err">${esc(err.message)}</div>`; }
}
async function openCollection(id) {
  openEditor('Cargando…', '');
  try {
    const c = (await api(`/api/admin/collection?store=${enc(state.store)}&id=${enc(id)}`)).collection;
    state.editing = { kind: 'collection', id, formSecs: ['general', 'seo'] };
    $('#ed-title').textContent = c.title; $('#ed-sub').innerHTML = `<span>${esc(c.handle)}</span> · tienda ${esc(state.store)}. Los cambios se guardan directamente en Shopify.`;
    const body = $('#ed-body'); body.innerHTML = '';
    body.appendChild(section('general', `<div class="grid">${field('c-title', 'Título', c.title)}${field('c-handle', 'Handle', c.handle, { hint: 'Si lo cambias se crea una redirección.' })}</div>${field('c-desc', 'Descripción (HTML)', c.descriptionHtml, { area: true, rows: 12, mono: true })}`));
    body.appendChild(section('seo', `${field('c-seot', 'Título SEO', (c.seo || {}).title || '', { hint: '0 caracteres' })}${field('c-seod', 'Descripción SEO', (c.seo || {}).description || '', { area: true, rows: 4, hint: '0 caracteres' })}`));
    const ms = section('meta', '<h3>Metacampos de la colección</h3>'); ms.appendChild(mfBlock(c.metafields.nodes, c.id)); body.appendChild(ms);
    setEditorTabs([['general', 'General'], ['seo', 'SEO'], ['meta', `Metacampos (${c.metafields.nodes.length})`]], 'general');
    bindForm({ title: 'c-title', handle: 'c-handle', descriptionHtml: 'c-desc', seo_title: 'c-seot', seo_description: 'c-seod' }, '/api/admin/collection/update');
    counter('c-seot', 60); counter('c-seod', 155);
  } catch (err) { console.error(err.stack); $('#ed-title').textContent = 'No se pudo abrir'; $('#ed-body').innerHTML = `<div class="callout err">${esc(err.message)}</div>`; }
}

/* ---------- VALORES DE UN METACAMPO ---------- */
const V = { store: null, locale: null, locales: {}, rows: [], byId: new Map(), byProduct: new Map(), bySku: new Map(), group: false, expanded: new Set(), total: 0, definition: null, loaded: false, busy: false, seq: 0, filterValue: null, recent: null, last: null, q: '', collIds: null, collName: '', collMap: {}, sel: new Set(), limit: 200 };
const vMf = () => { const raw = $('#v-mf').value.trim(), i = raw.indexOf('.'); return i > 0 && i < raw.length - 1 ? { ns: raw.slice(0, i), key: raw.slice(i + 1) } : null; };
const vShow = v => v === '' ? '(vacío)' : v;
const skuKey = r => (r && r.sku ? r.sku.trim().toLowerCase() : '');
const canSel = r => !!r && !r.nobase;
// Si un SKU está repetido, seleccionar (o quitar) una variante afecta a todas las que lo comparten.
function selAdd(id) { const r = V.byId.get(id); if (!canSel(r)) return; V.sel.add(id); const k = skuKey(r); if (k) (V.bySku.get(k) || []).forEach(s => { if (canSel(s)) V.sel.add(s.id); }); }
function selDel(id) { const r = V.byId.get(id); V.sel.delete(id); const k = skuKey(r); if (k) (V.bySku.get(k) || []).forEach(s => V.sel.delete(s.id)); }
const withSiblings = ids => { const out = new Set(); ids.forEach(id => { const r = V.byId.get(id); if (!canSel(r)) return; out.add(id); const k = skuKey(r); if (k) (V.bySku.get(k) || []).forEach(s => { if (canSel(s)) out.add(s.id); }); }); return out; };
const dupOthers = r => { const k = skuKey(r); return k ? (V.bySku.get(k) || []).filter(s => s.id !== r.id) : []; };
const dupTitle = sibs => sibs.map(s => `${s.product} (${STATUS_ES[s.product_status] || s.product_status || '?'})`).join(' · ');
const dupTag = r => { const o = dupOthers(r); return o.length ? tag('SKU repetido ×' + (o.length + 1), 'warn', 'También en: ' + dupTitle(o)) : ''; };
const skuCell = r => { const o = dupOthers(r); return esc(r.sku) + (o.length ? `<br>${dupTag(r)}<small>También en: ${esc(dupTitle(o.slice(0, 2)))}${o.length > 2 ? ' …' : ''}</small>` : ''); };
const productCell = r => esc(r.product) + (r.product_status && r.product_status !== 'ACTIVE' ? ' ' + statusTag(r.product_status) : '') + `<small>${esc(r.title)}</small>`;
const valueCell = r => (r.nobase ? '<span class="muted">(sin valor base)</span>' : (r.value === '' ? '<span class="muted">(vacío)</span>' : esc(r.value)) + (r.outdated ? ' ' + tag('desactualizada', 'warn', 'La traducción es anterior al último cambio del valor base') : '')) + (V.locale && !r.nobase ? `<small>Base: ${esc(r.base || '')}</small>` : '');
const vSpaces = v => v !== v.trim() ? 'espacio al inicio o al final' : /\u00a0|\s{2,}/.test(v) ? 'espacios dobles o raros' : '';

const LANG_ES = { nl: 'Holandés', pt: 'Portugués' };
function renderValueChips() {
  const items = [];
  state.stores.forEach(s => {
    items.push({ store: s.code, locale: null, label: s.code, title: s.domain });
    (V.locales[s.code] || []).forEach(l => items.push({ store: s.code, locale: l.locale, label: `${s.code} · ${LANG_ES[l.locale.split('-')[0]] || l.name}`, title: `Traducción ${l.locale} del metacampo en ${s.code}` }));
  });
  $('#v-stores').innerHTML = items.map(c => `<label class="chip"><input type="radio" name="vstore" value="${esc(c.store + '|' + (c.locale || ''))}"${c.store === V.store && (c.locale || null) === V.locale ? ' checked' : ''}><span title="${esc(c.title)}">${esc(c.label)}</span></label>`).join('');
  $$('#v-stores input').forEach(i => i.onchange = () => { const [s, l] = i.value.split('|'); V.store = s; V.locale = l || null; clearColl(); loadValues(); });
}
function initValues() {
  V.store = state.store; V.locale = null; renderValueChips();
  api('/api/admin/translation-locales').then(d => { V.locales = d.locales || {}; renderValueChips(); }).catch(() => {});
  $('#v-load').onclick = loadValues;
  $('#v-mf').addEventListener('change', loadValues);
  let qt; $('#v-q').addEventListener('input', () => { clearTimeout(qt); qt = setTimeout(() => { V.q = norm($('#v-q').value.trim()); V.recent = null; V.limit = 200; renderValues(); }, 200); });
  let ct; $('#v-coll').addEventListener('input', () => {
    clearTimeout(ct); const q = $('#v-coll').value.trim();
    if (!q && V.collIds) { clearColl(); renderValues(); return; }
    if (q.length < 2) return;
    ct = setTimeout(async () => { try { const d = await api(`/api/admin/collections?store=${enc(V.store)}&q=${enc(q)}`); d.collections.forEach(c => V.collMap[c.title] = c.id); $('#v-coll-list').innerHTML = d.collections.map(c => `<option value="${esc(c.title)}">`).join(''); } catch (e) { /* sin sugerencias */ } }, 300);
  });
  $('#v-coll').addEventListener('change', async () => {
    const name = $('#v-coll').value.trim(), id = V.collMap[name]; if (!name || !id) return;
    $('#v-count').textContent = 'Leyendo la colección…';
    try { const d = await api(`/api/admin/collection/product-ids?store=${enc(V.store)}&id=${enc(id)}`); V.collIds = new Set(d.ids); V.collName = name; V.recent = null; V.limit = 200; renderValues(); }
    catch (err) { $('#v-count').textContent = err.message; }
  });
  $('#v-sel-all').onclick = () => { vMatchingIds().forEach(selAdd); updateSelection(); };
  $$('[data-vview]').forEach(b => b.onclick = () => { V.group = b.dataset.vview === 'group'; $$('[data-vview]').forEach(x => x.setAttribute('aria-pressed', x === b)); V.limit = 200; renderValues(); });
  $('#v-table').addEventListener('click', e => { const t = e.target.closest('[data-toggle]'); if (!t) return; const id = t.dataset.toggle; V.expanded.has(id) ? V.expanded.delete(id) : V.expanded.add(id); renderValues(); });
  $('#v-sel-none').onclick = () => { V.sel.clear(); updateSelection(); };
  $('#v-more').onclick = () => { V.limit += 300; renderValues(); };
  $('#v-new').addEventListener('input', updateSelection); $('#v-clear').addEventListener('change', () => { $('#v-new').disabled = $('#v-clear').checked; updateSelection(); });
  $('#v-result').addEventListener('click', e => { if (e.target.closest('#v-undo')) undoLast(); });
  $('#v-apply').onclick = applyValues;
  $('#v-summary').addEventListener('click', e => {
    const f = e.target.closest('[data-vfilter]'), r = e.target.closest('[data-vreplace]');
    if (f) { V.recent = null; V.filterValue = V.filterValue === f.dataset.vfilter ? null : f.dataset.vfilter; V.limit = 200; renderValues(); }
    if (r) { V.recent = null; V.filterValue = r.dataset.vreplace; V.limit = 200; V.sel.clear(); vFiltered().forEach(x => selAdd(x.id)); renderValues(); $('#v-new').focus(); }
  });
  $('#v-filters').addEventListener('click', e => {
    const b = e.target.closest('[data-clear]'); if (!b) return;
    if (b.dataset.clear === 'value') V.filterValue = null; else if (b.dataset.clear === 'recent') V.recent = null; else { clearColl(); }
    V.limit = 200; renderValues();
  });
  $('#v-table').addEventListener('change', e => {
    const one = e.target.closest('[data-vid]');
    if (one) { one.checked ? selAdd(one.dataset.vid) : selDel(one.dataset.vid); updateSelection(); return; }
    const prod = e.target.closest('[data-pid]');
    if (prod) { (V.byProduct.get(prod.dataset.pid) || []).forEach(r => prod.checked ? selAdd(r.id) : selDel(r.id)); updateSelection(); return; }
    if (e.target.id === 'v-all') { vShownIds().forEach(id => e.target.checked ? selAdd(id) : selDel(id)); updateSelection(); }
  });
  loadDefs().then(d => {
    $('#v-mf-list').innerHTML = d.rows.filter(r => r.owner === 'PRODUCTVARIANT' && !((Object.values(r.stores).find(Boolean) || {}).type || '').includes('reference')).map(r => `<option value="${esc(r.namespace + '.' + r.key)}" label="${esc(r.name || '')}">`).join('');
  }).catch(() => {});
  loadValues();
}
function clearColl() { V.collIds = null; V.collName = ''; $('#v-coll').value = ''; }

async function loadValues() {
  const mf = vMf(), st = $('#v-status');
  if (!V.store) { st.textContent = 'No hay tiendas disponibles.'; return; }
  if (!mf) { st.textContent = 'Escribe el metacampo como namespace.clave (por ejemplo custom.tiempo_de_entrega).'; return; }
  const seq = ++V.seq;
  Object.assign(V, { rows: [], byId: new Map(), byProduct: new Map(), bySku: new Map(), expanded: new Set(), filterValue: null, recent: null, last: null, loaded: false, limit: 200 }); V.sel.clear();
  $('#v-main').hidden = true; $('#v-bar').hidden = true; $('#v-result').innerHTML = '';
  const lname = V.locale ? (LANG_ES[V.locale.split('-')[0]] || V.locale) : '';
  $('#v-locale-note').hidden = !V.locale;
  if (V.locale) $('#v-locale-note').innerHTML = `Estás viendo y editando la <strong>traducción al ${esc(lname.toLowerCase())} (${esc(V.locale)})</strong> del metacampo, no su valor base. Las variantes sin valor base no se pueden traducir. Si cambias el valor base, la traducción queda «desactualizada» hasta que la vuelvas a escribir.`;
  $('#v-clear-label').textContent = V.locale ? 'Quitar la traducción' : 'Vaciar el valor';
  $('#v-new').placeholder = V.locale ? `Texto libre en ${lname.toLowerCase()}, por ejemplo 3/5 dagen` : 'Texto libre, por ejemplo 3-5 días';
  let after = null, first = true;
  try {
    do {
      const d = await api(`/api/admin/values/page?store=${enc(V.store)}&namespace=${enc(mf.ns)}&key=${enc(mf.key)}${V.locale ? '&locale=' + enc(V.locale) : ''}${after ? '&after=' + enc(after) : ''}`);
      if (seq !== V.seq) return;
      if (first) { V.total = d.total || 0; V.definition = d.definition; first = false; }
      d.variants.forEach(v => { V.rows.push(v); V.byId.set(v.id, v); (V.byProduct.get(v.product_id) || V.byProduct.set(v.product_id, []).get(v.product_id)).push(v); const k = skuKey(v); if (k) (V.bySku.get(k) || V.bySku.set(k, []).get(k)).push(v); });
      st.textContent = `Cargando variantes… ${V.rows.length}${V.total ? ' de ' + V.total : ''}`;
      after = d.has_next ? d.cursor : null;
    } while (after);
    V.loaded = true;
    st.textContent = `${V.rows.length} variantes en ${V.store}${V.locale ? ' (traducción ' + V.locale + ')' : ''}.` + (V.definition ? '' : ` El metacampo ${mf.ns}.${mf.key} no existe en ${V.store}: solo puedes consultarlo.`);
    $('#v-main').hidden = false; renderValues();
  } catch (err) { if (seq === V.seq) st.textContent = err.message; }
}

function vFiltered() {
  return V.rows.filter(r => (V.filterValue === null || r.value === V.filterValue) && (!V.recent || V.recent.has(r.id)) && (!V.q || norm(`${r.sku} ${r.product} ${r.title}`).includes(V.q)) && (!V.collIds || V.collIds.has(r.product_id)));
}
// Productos con alguna variante que cumple los filtros, con TODAS sus variantes.
function vProducts() { const seen = new Map(); vFiltered().forEach(r => { if (!seen.has(r.product_id)) seen.set(r.product_id, V.byProduct.get(r.product_id) || [r]); }); return [...seen.values()]; }
const vShownIds = () => V.group ? vProducts().slice(0, V.limit).flat().map(r => r.id) : vFiltered().slice(0, V.limit).map(r => r.id);
const vMatchingIds = () => V.group ? vProducts().flat().map(r => r.id) : vFiltered().map(r => r.id);

function productRows(vs) {
  const pid = vs[0].product_id, open = V.expanded.has(pid), counts = new Map();
  vs.forEach(v => counts.set(v.value, (counts.get(v.value) || 0) + 1));
  const sum = [...counts].map(([v, c]) => `${v === '' ? '<em>(vacío)</em>' : esc(v)} <span class="muted">(${c})</span>`).join(', ');
  let h = `<tr data-prod="${esc(pid)}"><td class="chk"><input type="checkbox" data-pid="${esc(pid)}" aria-label="Seleccionar todas las variantes de ${esc(vs[0].product)}"></td><td class="wrap-cell"><button type="button" class="row-link" data-toggle="${esc(pid)}" aria-expanded="${open}">${open ? '▾' : '▸'} ${esc(vs[0].product)}</button>${vs[0].product_status && vs[0].product_status !== 'ACTIVE' ? ' ' + statusTag(vs[0].product_status) : ''}${vs.some(v => dupOthers(v).length) ? ' ' + tag('SKU repetido en otros productos', 'warn') : ''}<small>${vs.length} ${vs.length === 1 ? 'variante' : 'variantes'}</small></td><td class="wrap-cell">${sum}</td><td class="wrap-cell after"></td></tr>`;
  if (open) h += vs.map(r => `<tr class="subrow" data-row="${esc(r.id)}"><td class="chk"><input type="checkbox" data-vid="${esc(r.id)}"${r.nobase ? ' disabled' : ''} aria-label="Seleccionar ${esc(r.sku || r.title)}"></td><td class="wrap-cell"><small>${esc(r.sku)} · ${esc(r.title)}</small>${dupTag(r)}</td><td class="wrap-cell">${valueCell(r)}</td><td class="wrap-cell after"></td></tr>`).join('');
  return h;
}

function renderValues() {
  const counts = new Map(); V.rows.forEach(r => counts.set(r.value, (counts.get(r.value) || 0) + 1));
  const list = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  $('#v-summary').innerHTML = table(['Valor', 'Variantes', ''], list.map(([v, n]) => `<tr${V.filterValue === v ? ' class="vrow-sel"' : ''}><td class="wrap-cell">${v === '' ? '<em>(vacío)</em>' : esc(v)}${vSpaces(v) ? ' ' + tag(vSpaces(v), 'warn') : ''}</td><td class="nw">${n}</td><td class="nw"><button type="button" class="button secondary" data-vfilter="${esc(v)}" aria-pressed="${V.filterValue === v}">${V.filterValue === v ? 'Quitar filtro' : 'Ver'}</button> ${V.definition ? `<button type="button" class="button secondary" data-vreplace="${esc(v)}">Reemplazar…</button>` : ''}</td></tr>`).join(''));
  $('#v-filters').innerHTML = (V.filterValue !== null ? `<span class="vchip">Valor: «${esc(vShow(V.filterValue))}» <button type="button" class="button secondary" data-clear="value" aria-label="Quitar filtro de valor">✕</button></span>` : '') + (V.recent ? `<span class="vchip">Cambiadas ahora: ${V.recent.size} <button type="button" class="button secondary" data-clear="recent" aria-label="Quitar filtro de cambiadas">✕</button></span>` : '') + (V.collIds ? `<span class="vchip">Colección: ${esc(V.collName)} <button type="button" class="button secondary" data-clear="coll" aria-label="Quitar filtro de colección">✕</button></span>` : '');
  $('#v-sel-all').textContent = V.group ? 'Seleccionar los productos que coinciden' : 'Seleccionar las que coinciden';
  $('#v-view-hint').textContent = V.group ? 'Marcar un producto selecciona todas sus variantes, tengan el valor que tengan.' : '';
  const f = vFiltered(); let more = false;
  if (!f.length) $('#v-table').innerHTML = '<p class="muted">Ninguna variante coincide con los filtros.</p>';
  else if (!V.group) {
    $('#v-table').innerHTML = table(['<input type="checkbox" id="v-all" aria-label="Seleccionar las variantes mostradas">', 'SKU', 'Producto', 'Valor actual', 'Después'], f.slice(0, V.limit).map(r => `<tr data-row="${esc(r.id)}"><td class="chk"><input type="checkbox" data-vid="${esc(r.id)}"${r.nobase ? ' disabled' : ''} aria-label="Seleccionar ${esc(r.sku || r.title)}"></td><td class="wrap-cell">${skuCell(r)}</td><td class="wrap-cell">${productCell(r)}</td><td class="wrap-cell">${valueCell(r)}</td><td class="wrap-cell after"></td></tr>`).join(''));
    more = f.length > V.limit;
  } else {
    const prods = vProducts();
    $('#v-table').innerHTML = table(['<input type="checkbox" id="v-all" aria-label="Seleccionar las variantes de los productos mostrados">', 'Producto', 'Valores actuales', 'Después'], prods.slice(0, V.limit).map(productRows).join(''));
    more = prods.length > V.limit;
  }
  $('#v-more').hidden = !more;
  updateSelection();
}

function updateSelection() {
  const f = vFiltered(), clear = $('#v-clear').checked, nv = clear ? '' : $('#v-new').value.trim();
  $$('#v-table tbody tr').forEach(tr => {
    if (tr.dataset.prod) {
      const vs = V.byProduct.get(tr.dataset.prod) || [], n = vs.filter(r => V.sel.has(r.id)).length, cb = $('[data-pid]', tr), todo = vs.filter(r => V.sel.has(r.id) && r.value !== nv).length;
      cb.checked = n > 0 && n === vs.length; cb.indeterminate = n > 0 && n < vs.length; tr.classList.toggle('vrow-sel', n > 0);
      $('.after', tr).innerHTML = n && (nv || clear) ? (todo ? `<span class="vnew">${clear ? '(vacío)' : esc(nv)}</span> <small>${todo} de ${vs.length} se cambian</small>` : '<span class="muted">sin cambios</span>') : '<span class="muted">—</span>';
      return;
    }
    const id = tr.dataset.row, on = V.sel.has(id), r = V.byId.get(id);
    tr.classList.toggle('vrow-sel', on); $('[data-vid]', tr).checked = on;
    $('.after', tr).innerHTML = on && (nv || clear) ? (r && r.value === nv ? '<span class="muted">sin cambios</span>' : `<span class="vnew">${clear ? '(vacío)' : esc(nv)}</span>`) : '<span class="muted">—</span>';
  });
  const all = $('#v-all'), shownIds = vShownIds(), n = shownIds.filter(id => V.sel.has(id)).length;
  if (all) { all.checked = n > 0 && n === shownIds.length; all.indeterminate = n > 0 && n < shownIds.length; }
  const matching = new Set(vMatchingIds()), out = [...V.sel].filter(id => !matching.has(id)).length;
  $('#v-count').textContent = (V.group ? `${vProducts().length} productos (${f.length} variantes) coinciden` : `${f.length} de ${V.rows.length} variantes coinciden`) + ` · ${V.sel.size} variantes seleccionadas` + (out ? ` (${out} fuera del filtro actual)` : '') + (V.group ? '' : (f.length > V.limit ? ` (se muestran ${V.limit})` : ''));
  const groups = new Map(); V.sel.forEach(id => { const r = V.byId.get(id); if (r) groups.set(r.value, (groups.get(r.value) || 0) + 1); });
  const changing = [...V.sel].filter(id => V.byId.get(id) && V.byId.get(id).value !== nv).length;
  $('#v-bar').hidden = !V.loaded;
  $('#v-summary-line').textContent = !V.definition ? 'El metacampo no existe en esta tienda: no se puede cambiar.' : !V.sel.size ? 'Selecciona variantes para cambiarlas.' : `${[...groups].map(([v, c]) => `«${vShow(v)}» (${c})`).join(', ')} → «${clear ? '(vacío)' : (nv || '…')}»`;
  const btn = $('#v-apply'); btn.textContent = V.sel.size ? `Aplicar a ${changing} ${changing === 1 ? 'variante' : 'variantes'}` : 'Aplicar';
  btn.disabled = V.busy || !V.definition || (!nv && !clear) || !changing;
}

// Ejecuta un plan [{ value, clear, ids }] en bloques de 100 y actualiza los datos locales.
async function runPlan(plan, label) {
  const mf = vMf(), st = $('#v-status'), total = plan.reduce((n, g) => n + g.ids.length, 0);
  let op = null, applied = 0, skipped = 0, done = 0; const failed = [], okIds = [];
  for (const g of plan) {
    for (let i = 0; i < g.ids.length; i += 100) {
      const chunk = g.ids.slice(i, i + 100); st.textContent = `${label}… ${done} de ${total}`; done += chunk.length;
      try {
        const r = await post('/api/admin/values/apply', { store: V.store, namespace: mf.ns, key: mf.key, value: g.value, clear: g.clear, exact: !!g.exact, locale: V.locale, ids: chunk, operation_id: op });
        op = r.operation_id; applied += r.applied; skipped += r.skipped; r.failed.forEach(f => failed.push(f));
        const bad = new Set(r.failed.map(f => f.id));
        chunk.forEach(id => { if (!bad.has(id)) { const row = V.byId.get(id); if (row) row.value = g.clear ? '' : g.value; okIds.push(id); } });
      } catch (err) { chunk.forEach(id => failed.push({ id, error: err.message })); }
    }
  }
  return { op, applied, skipped, failed, okIds };
}
function showResult(res, title, undoable) {
  $('#v-status').textContent = `Operación terminada${res.op ? ' (registro #' + res.op + ')' : ''}.`;
  $('#v-result').innerHTML = `<div class="res-summary"><h2>${title}</h2>${tag(res.applied + ' cambiadas', res.applied ? 'ok' : 'muted')}${res.skipped ? tag(res.skipped + ' ya tenían ese valor', 'muted') : ''}${res.failed.length ? tag(res.failed.length + ' con error', 'err') : ''}${undoable && V.last && V.last.items.length ? '<button type="button" class="button secondary" id="v-undo">Deshacer este cambio</button>' : ''}</div>` +
    (res.failed.length ? `<div class="callout err"><strong>No se pudieron cambiar:</strong>${table(['SKU', 'Producto', 'Error'], res.failed.slice(0, 50).map(f => { const r = V.byId.get(f.id) || {}; return `<tr><td class="nw">${esc(r.sku || '')}</td><td class="wrap-cell">${esc(r.product || '')}<small>${esc(r.title || '')}</small></td><td class="wrap-cell">${esc(f.error || '')}</td></tr>`; }).join(''))}${res.failed.length > 50 ? `<p class="muted">…y ${res.failed.length - 50} más (consulta el Historial).</p>` : ''}</div>` : '');
}
async function applyValues() {
  const mf = vMf(), clear = $('#v-clear').checked, nv = clear ? '' : $('#v-new').value.trim();
  const ids = [...withSiblings(V.sel)].filter(id => V.byId.get(id).value !== nv);
  if (!mf || (!nv && !clear) || !ids.length) return;
  const before = new Map(ids.map(id => [id, V.byId.get(id).value]));
  V.busy = true; updateSelection(); $('#v-result').innerHTML = '';
  const res = await runPlan([{ value: nv, clear, ids }], 'Aplicando');
  V.busy = false; shown.historial = false;
  if (clear) { $('#v-clear').checked = false; $('#v-new').disabled = false; $('#v-new').value = ''; }
  // Las variantes cambiadas se quedan visibles y seleccionadas (aunque ya no cumplan el filtro de valor) para poder corregirlas o deshacer.
  V.last = { items: res.okIds.map(id => ({ id, before: before.get(id) })) };
  if (res.okIds.length) { V.recent = new Set(res.okIds); V.filterValue = null; res.okIds.forEach(id => V.sel.add(id)); }
  renderValues(); showResult(res, 'Resultado', true);
}
async function undoLast() {
  if (!V.last || !V.last.items.length || V.busy) return;
  const groups = new Map(); V.last.items.forEach(i => { if (V.byId.get(i.id)) (groups.get(i.before) || groups.set(i.before, []).get(i.before)).push(i.id); });
  const plan = [...groups].map(([value, ids]) => ({ value, clear: value === '', exact: true, ids }));
  V.busy = true; updateSelection(); $('#v-result').innerHTML = '';
  const res = await runPlan(plan, 'Deshaciendo');
  V.busy = false; shown.historial = false; V.last = null;
  if (res.okIds.length) { V.recent = new Set(res.okIds); V.filterValue = null; res.okIds.forEach(id => V.sel.add(id)); }
  renderValues(); showResult(res, 'Cambio deshecho', false);
}

/* ---------- METACAMPOS: matriz ---------- */
let mfOwner = 'PRODUCT';
$$('[data-owner]').forEach(b => b.onclick = () => { mfOwner = b.dataset.owner; $$('[data-owner]').forEach(x => x.setAttribute('aria-pressed', x === b)); renderMatrix(); });
$('#mf-q').oninput = () => renderMatrix(); $('#mf-diff').onchange = () => renderMatrix();
$('#mf-reload').onclick = async () => { state.defs = null; state.defsPromise = null; renderMatrix(); };
const rowStatus = (r, codes) => { const present = codes.map(c => r.stores[c]).filter(Boolean); return present.length < codes.length ? 'missing' : new Set(present.map(p => p.type)).size > 1 ? 'mismatch' : 'ok'; };

async function renderMatrix() {
  const box = $('#mf-matrix'), st = $('#mf-status');
  if (!state.defs) {
    st.textContent = 'Leyendo las definiciones de todas las tiendas…'; box.innerHTML = '';
    try { await loadDefs(); } catch (err) { st.textContent = err.message; return; }
  }
  const d = state.defs, q = norm($('#mf-q').value), diff = $('#mf-diff').checked;
  let rows = d.rows.filter(r => r.owner === mfOwner && (!q || norm(`${r.name || ''} ${r.namespace}.${r.key}`).includes(q)) && (!diff || rowStatus(r, d.stores) !== 'ok'));
  st.textContent = `${rows.length} metacampos de ${OWNER_ES[mfOwner].toLowerCase()}` + (d.errors.length ? ` · No se pudieron leer: ${d.errors.map(e => e.store).join(', ')}` : '');
  if (!rows.length) { box.innerHTML = '<p class="muted">Ningún metacampo coincide con el filtro.</p>'; return; }
  const groups = {}; rows.forEach(r => (groups[r.namespace] = groups[r.namespace] || []).push(r));
  box.innerHTML = Object.entries(groups).map(([ns, list]) => `<details class="ns-group"${(rows.length <= 60 || q || diff) ? ' open' : ''}><summary>${esc(ns)}<span class="muted">${list.length} metacampos</span></summary>${table(['Metacampo', ...d.stores.map(esc), ''], list.map(r => {
    const stat = rowStatus(r, d.stores), type = (Object.values(r.stores).find(Boolean) || {}).type;
    const cells = d.stores.map(c => { const x = r.stores[c]; return `<td class="cell nw">${!x ? tag('falta', 'warn') : (stat === 'mismatch' ? tag(x.type, 'err') : tag('✓', 'ok', x.type))}</td>`; }).join('');
    return `<tr><td class="nw"><strong>${esc(r.name || r.key)}</strong><small>${esc(r.namespace + '.' + r.key)} · ${esc(type || '')}</small></td>${cells}<td class="nw"><button type="button" class="button secondary" data-edit="${esc(r.owner + '|' + r.namespace + '|' + r.key)}">Editar en varias tiendas</button>${stat === 'missing' ? ` <button type="button" class="button secondary" data-create="${esc(r.owner + '|' + r.namespace + '|' + r.key)}">Crear donde falta</button>` : ''}</td></tr>`;
  }).join(''), 'matrix')}</details>`).join('');
}
$('#mf-matrix').addEventListener('click', async e => {
  const ed = e.target.closest('[data-edit]'), cr = e.target.closest('[data-create]');
  if (ed) { const [owner, namespace, key] = ed.dataset.edit.split('|'); goMulti(owner, namespace, key); }
  if (cr) {
    const [owner, namespace, key] = cr.dataset.create.split('|'), r = state.defs.rows.find(x => x.owner === owner && x.namespace === namespace && x.key === key);
    const missing = state.defs.stores.filter(c => !r.stores[c]); cr.disabled = true; $('#mf-status').textContent = `Creando ${namespace}.${key} en ${missing.join(', ')}…`;
    try {
      const res = await post('/api/admin/definitions/create', { owner, namespace, key, stores: missing });
      const bad = res.results.filter(x => !x.ok);
      state.defs = null; state.defsPromise = null; await renderMatrix();
      $('#mf-status').textContent = bad.length ? 'No se pudo en: ' + bad.map(x => `${x.store} (${x.error})`).join('; ') : `Definición creada en ${missing.join(', ')}.`;
    } catch (err) { $('#mf-status').textContent = err.message; cr.disabled = false; }
  }
});

/* ---------- VARIAS TIENDAS ---------- */
const FIELDS = {
  product: [
    { id: 'f:status', key: 'status', label: 'Estado', control: 'enum', values: [['ACTIVE', 'Activo'], ['DRAFT', 'Borrador'], ['ARCHIVED', 'Archivado']] },
    { id: 'f:vendor', key: 'vendor', label: 'Proveedor', control: 'text' },
    { id: 'f:productType', key: 'productType', label: 'Tipo de producto', control: 'text' }],
  variant: [
    { id: 'f:weight', key: 'weight', label: 'Peso', control: 'number' },
    { id: 'f:weightUnit', key: 'weightUnit', label: 'Unidad de peso', control: 'enum', values: [['KILOGRAMS', 'Kilogramos'], ['GRAMS', 'Gramos'], ['POUNDS', 'Libras'], ['OUNCES', 'Onzas']] },
    { id: 'f:taxable', key: 'taxable', label: 'Cobrar impuestos', control: 'bool' },
    { id: 'f:inventoryPolicy', key: 'inventoryPolicy', label: 'Si no hay stock', control: 'enum', values: [['DENY', 'Dejar de vender'], ['CONTINUE', 'Seguir vendiendo']] },
    { id: 'f:tracked', key: 'tracked', label: 'Seguimiento de stock', control: 'bool' },
    { id: 'f:requiresShipping', key: 'requiresShipping', label: 'Requiere envío', control: 'bool' }],
};
FIELDS.product_variants = FIELDS.variant;
const SCOPE_HINT = { product: 'Se modifica el producto que contiene cada SKU.', variant: 'Se modifica solo la variante que tiene ese SKU.', product_variants: 'Se modifican todas las variantes del producto que contiene cada SKU. Si un SKU está repetido, se aplica a todas sus instancias.' };
let scope = 'product', comboSeq = 0;
const OPEN_ROWS = () => $$('#g-changes .change');

function scopeOptions() {
  const out = FIELDS[scope].map(f => ({ ...f, kind: 'field', sub: '', group: scope === 'product' ? 'Campos del producto' : 'Campos de las variantes', tagText: '' }));
  if (state.defs) {
    const owner = scope === 'product' ? 'PRODUCT' : 'PRODUCTVARIANT';
    state.defs.rows.filter(r => r.owner === owner).forEach(r => {
      const any = Object.values(r.stores).filter(Boolean), first = any[0] || {}, withChoices = any.find(x => x.choices && x.choices.length);
      out.push({ id: `m:${r.namespace}.${r.key}`, kind: 'metafield', namespace: r.namespace, key: r.key, label: r.name || r.key, sub: `${r.namespace}.${r.key}`, group: 'Metacampos · ' + r.namespace, tagText: first.type || '', type: first.type, choices: withChoices ? withChoices.choices : null });
    });
  }
  return out;
}
function makeCombo(row, onPick) {
  const wrap = $('.combo', row), input = $('input', wrap), list = $('ul', wrap), id = 'lb' + (++comboSeq);
  list.id = id; input.setAttribute('aria-controls', id);
  let items = [], active = -1;
  const close = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); };
  const setActive = i => {
    active = i; $$('[role=option]', list).forEach(li => li.setAttribute('aria-selected', +li.dataset.i === i));
    const li = $(`[data-i="${i}"]`, list); if (li) { input.setAttribute('aria-activedescendant', li.id); li.scrollIntoView({ block: 'nearest' }); }
  };
  function render() {
    const q = norm(input.value);
    items = scopeOptions().filter(o => !q || norm(`${o.label} ${o.sub} ${o.group}`).includes(q)).slice(0, 80);
    let html = '', last = null;
    items.forEach((o, i) => {
      if (o.group !== last) { html += `<li role="presentation">${esc(o.group)}</li>`; last = o.group; }
      html += `<li role="option" id="${id}-${i}" data-i="${i}" aria-selected="false"><span>${esc(o.label)}</span>${o.sub ? `<small>${esc(o.sub)}</small>` : ''}${o.tagText ? tag(o.tagText) : ''}</li>`;
    });
    if (!items.length) html = '<li class="empty" role="presentation">Ningún campo coincide.</li>';
    if (!state.defs) html += '<li class="empty" role="presentation">Cargando metacampos…</li>';
    list.innerHTML = html; list.hidden = false; input.setAttribute('aria-expanded', 'true'); setActive(items.length ? 0 : -1);
  }
  const pick = o => { row._sel = o; input.value = o.label + (o.sub ? '  ·  ' + o.sub : ''); close(); onPick(o); };
  const open = () => { render(); if (!state.defs) loadDefs().then(() => { if (!list.hidden) render(); }).catch(() => {}); };
  input.addEventListener('focus', open); input.addEventListener('click', () => { if (list.hidden) open(); });
  input.addEventListener('input', () => { if (row._sel) { row._sel = null; row._ctl = null; $('.ctl-slot', row).innerHTML = '<p class="muted">Elige primero el campo.</p>'; } open(); });
  input.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); if (list.hidden) open(); else setActive(Math.min(active + 1, items.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(Math.max(active - 1, 0)); }
    else if (e.key === 'Enter' && !list.hidden && items[active]) { e.preventDefault(); pick(items[active]); }
    else if (e.key === 'Escape' && !list.hidden) { e.preventDefault(); close(); }
  });
  input.addEventListener('blur', close);
  list.addEventListener('mousedown', e => { const li = e.target.closest('[role=option]'); if (!li) return; e.preventDefault(); pick(items[+li.dataset.i]); });
}
function valueControl(o) {
  if (o.kind === 'metafield') return makeControl(o.type, { choices: o.choices });
  const el = document.createElement('div'); el.className = 'ctl'; let get;
  if (o.control === 'enum') { el.innerHTML = `<select>${o.values.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>`; const s = $('select', el); get = () => s.value; }
  else if (o.control === 'bool') { el.innerHTML = '<select><option value="true">Sí</option><option value="false">No</option></select>'; const s = $('select', el); get = () => s.value === 'true'; }
  else { el.innerHTML = `<input type="${o.control === 'number' ? 'number' : 'text'}"${o.control === 'number' ? ' step="any" min="0"' : ''}>`; const i = $('input', el); get = () => i.value; }
  return { el, get };
}
function addChange(pre) {
  const row = document.createElement('div'); row.className = 'change';
  row.innerHTML = `<div class="field combo"><input role="combobox" aria-label="Campo a cambiar" aria-expanded="false" aria-autocomplete="list" autocomplete="off" placeholder="Buscar campo o metacampo…"><ul role="listbox" hidden></ul></div><div class="ctl-slot"><p class="muted">Elige primero el campo.</p></div><button type="button" class="button secondary remove" aria-label="Quitar este cambio">✕</button>`;
  $('.remove', row).onclick = () => { row.remove(); if (!OPEN_ROWS().length) addChange(); };
  const choose = o => { row._ctl = valueControl(o); const slot = $('.ctl-slot', row); slot.innerHTML = ''; slot.appendChild(row._ctl.el); const f = $('input,select,textarea', slot); if (f) f.focus(); };
  makeCombo(row, choose); $('#g-changes').appendChild(row);
  if (pre) { const o = scopeOptions().find(x => x.id === pre); if (o) { row._sel = o; $('input', row).value = o.label + (o.sub ? '  ·  ' + o.sub : ''); choose(o); } }
  return row;
}
function setScope(s) {
  scope = s; $$('[data-scope]').forEach(b => b.setAttribute('aria-pressed', b.dataset.scope === s));
  $('#scope-hint').textContent = SCOPE_HINT[s]; $('#g-changes').innerHTML = ''; addChange();
}
function parseSkus() { return [...new Set($('#g-skus').value.split(/[\n,;]+/).map(x => x.trim()).filter(Boolean))]; }
function goMulti(owner, namespace, key) {
  showTab('varias'); setScope(owner === 'PRODUCT' ? 'product' : 'variant');
  const id = `m:${namespace}.${key}`;
  const go = () => { $('#g-changes').innerHTML = ''; addChange(id); $('#g-skus').focus(); };
  state.defs ? go() : loadDefs().then(go);
}
function initMulti() {
  $('#g-stores').innerHTML = state.stores.map(s => `<label class="chip"><input type="checkbox" value="${esc(s.code)}" checked><span>${esc(s.code)}</span></label>`).join('');
  $$('[data-scope]').forEach(b => b.onclick = () => { if (b.dataset.scope !== scope) setScope(b.dataset.scope); });
  $('#g-skus').addEventListener('input', () => { const n = parseSkus().length; $('#sku-count').textContent = `${n} SKU`; });
  $('#g-find').onclick = findByName; $('#g-check').onclick = checkDuplicates;
  $('#g-find-q').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); findByName(); } });
  $('#g-all').onclick = () => $$('#g-stores input').forEach(i => i.checked = true);
  $('#g-none').onclick = () => $$('#g-stores input').forEach(i => i.checked = false);
  $('#g-add').onclick = () => { const r = addChange(); $('input', r).focus(); };
  $('#g-preview').onclick = e => runGlobal('/api/admin/global/preview', e.target, false);
  $('#g-apply').onclick = e => runGlobal('/api/admin/global/apply', e.target, true);
  setScope('product'); loadDefs().catch(() => {});
}
/* Búsqueda por nombre y control de SKU repetidos */
const gStores = () => $$('#g-stores input:checked').map(i => i.value);
const dupPlural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
function dupReport(stores) {
  const bad = stores.filter(s => s.error), withDup = stores.filter(s => s.dup_skus);
  let h = bad.map(s => `<div class="callout err"><strong>${esc(s.store)}</strong>: no se pudo comprobar (${esc(s.error)})</div>`).join('');
  if (!withDup.length) return h + (stores.length > bad.length ? `<div class="callout ok">Ningún SKU está repetido en ${esc(stores.filter(s => !s.error).map(s => s.store).join(', '))}.</div>` : '');
  const totalSkus = withDup.reduce((n, s) => n + s.dup_skus, 0);
  h += `<div class="callout"><strong>SKU duplicados: ${dupPlural(totalSkus, 'caso', 'casos')} en ${dupPlural(withDup.length, 'tienda', 'tiendas')}.</strong> Un SKU repetido aplica el cambio a todas las variantes que lo comparten.</div>`;
  h += withDup.map(s => `<details class="res-store" open><summary><strong>${esc(s.store)}</strong>${tag(`${dupPlural(s.dup_skus, 'SKU repetido', 'SKU repetidos')} · ${s.dup_variants} variantes`, 'warn')}</summary><div>${table(['SKU', 'Veces', 'Variantes que lo comparten'], s.duplicates.map(d => `<tr><td class="nw">${esc(d.sku)}</td><td class="nw">×${d.count}</td><td class="wrap-cell">${esc(d.items.join(' | '))}${d.count > d.items.length ? ' …' : ''}</td></tr>`).join(''))}</div></details>`).join('');
  return h;
}
function addSkusToBox(skus) {
  const set = new Set(parseSkus().map(x => x.toLowerCase())), add = skus.filter(x => !set.has(x.toLowerCase())), box = $('#g-skus');
  box.value = (box.value.trim() ? box.value.trim() + '\n' : '') + add.join('\n');
  box.dispatchEvent(new Event('input')); return add.length;
}
function removeSkusFromBox(skus) {
  const drop = new Set(skus.map(x => x.toLowerCase())), box = $('#g-skus');
  box.value = parseSkus().filter(x => !drop.has(x.toLowerCase())).join('\n');
  box.dispatchEvent(new Event('input'));
}
const finderBusy = on => $$('#g-find,#g-check').forEach(b => b.disabled = on);
async function findByName() {
  const q = $('#g-find-q').value.trim(), st = $('#g-find-status'), out = $('#g-find-result'), stores = gStores();
  if (q.length < 2) { st.textContent = 'Escribe al menos 2 letras del nombre.'; return; }
  if (!stores.length) { st.textContent = 'Paso 2: elige al menos una tienda.'; return; }
  finderBusy(true); st.textContent = 'Buscando en las tiendas…'; out.innerHTML = '';
  try {
    const d = await post('/api/admin/global/find', { q, stores });
    st.textContent = d.skus.length ? `${dupPlural(d.skus.length, 'SKU encontrado', 'SKU encontrados')}.` : 'No se encontró ningún producto con ese nombre.';
    const warn = (d.errors || []).map(e => `<div class="callout err"><strong>${esc(e.store)}</strong>: ${esc(e.error)}</div>`).join('')
      + (d.truncated.length ? `<div class="callout">Hay más resultados en ${esc(d.truncated.join(', '))}: afina el nombre para verlos todos.</div>` : '')
      + (d.limited ? `<div class="callout">Solo se comprobaron duplicados en los primeros ${d.max} SKU.</div>` : '');
    if (!d.skus.length) { out.innerHTML = warn; return; }
    const dupMap = {}; d.stores.forEach(s => (s.duplicates || []).forEach(x => (dupMap[x.sku.toLowerCase()] = dupMap[x.sku.toLowerCase()] || []).push(`${s.store} ×${x.count}`)));
    const rows = d.skus.map((e, i) => { const dp = dupMap[e.sku.toLowerCase()]; return `<tr><td class="chk"><input type="checkbox" data-i="${i}" checked aria-label="Elegir ${esc(e.sku)}"></td><td class="nw">${esc(e.sku)}${dp ? '<br>' + tag('SKU repetido: ' + dp.join(', '), 'warn') : ''}</td><td class="wrap-cell">${esc(e.title)}</td><td class="nw">${esc(e.stores.join(', '))}</td></tr>`; }).join('');
    out.innerHTML = warn + dupReport(d.stores) + `<div class="actions"><button type="button" class="button primary" id="g-use">Usar los SKU marcados</button><label class="muted"><input type="checkbox" id="g-find-all" checked> Marcar todos</label></div>` + table(['', 'SKU', 'Producto', 'Encontrado en'], rows);
    const sync = () => {
      const all = $$('#g-find-result tbody input[data-i]'), sku = c => d.skus[+c.dataset.i].sku;
      removeSkusFromBox(all.filter(c => !c.checked).map(sku)); addSkusToBox(all.filter(c => c.checked).map(sku));
    };
    $('#g-find-all').onchange = e => { $$('#g-find-result tbody input[data-i]').forEach(c => c.checked = e.target.checked); sync(); };
    $$('#g-find-result tbody input[data-i]').forEach(c => c.addEventListener('change', sync));
    sync(); st.textContent += ' Los marcados se añaden al paso 1.';
    $('#g-use').onclick = () => {
      const picked = $$('#g-find-result tbody input[data-i]:checked').map(c => d.skus[+c.dataset.i].sku);
      if (!picked.length) { st.textContent = 'Marca al menos un SKU.'; return; }
      const n = addSkusToBox(picked); st.textContent = `${dupPlural(n, 'SKU añadido', 'SKU añadidos')} al paso 1${n < picked.length ? ' (el resto ya estaba)' : ''}.`;
    };
  } catch (err) { st.textContent = err.message; } finally { finderBusy(false); }
}
async function checkDuplicates() {
  const skus = parseSkus(), st = $('#g-find-status'), out = $('#g-find-result'), stores = gStores();
  if (!skus.length) { st.textContent = 'Escribe o añade primero algún SKU.'; return; }
  if (!stores.length) { st.textContent = 'Paso 2: elige al menos una tienda.'; return; }
  finderBusy(true); st.textContent = 'Comprobando SKU…'; out.innerHTML = '';
  try {
    const d = await post('/api/admin/global/sku-check', { skus, stores }); st.textContent = `${dupPlural(d.skus, 'SKU comprobado', 'SKU comprobados')}.`;
    out.innerHTML = dupReport(d.stores) + d.stores.filter(s => s.missing).map(s => `<p class="muted">${esc(s.store)}: ${dupPlural(s.missing, 'SKU no existe', 'SKU no existen')}.</p>`).join('');
  } catch (err) { st.textContent = err.message; } finally { finderBusy(false); }
}
function collectBody() {
  const skus = parseSkus(), stores = $$('#g-stores input:checked').map(i => i.value);
  if (!skus.length) throw new Error('Paso 1: indica al menos un SKU.');
  if (!stores.length) throw new Error('Paso 2: elige al menos una tienda.');
  const changes = OPEN_ROWS().map((row, n) => {
    const o = row._sel; if (!o || !row._ctl) throw new Error(`Paso 3: elige el campo del cambio ${n + 1}.`);
    const v = row._ctl.get();
    if (o.kind === 'metafield') { if (v === '' || v === '[]') throw new Error(`Paso 3: indica el valor de ${o.label}.`); return { kind: 'metafield', namespace: o.namespace, key: o.key, type: o.type, value: String(v) }; }
    if (o.key === 'weight' && v === '') throw new Error('Paso 3: indica el peso.');
    return { kind: 'field', field: o.key, value: v };
  });
  return { scope, stores, skus, changes };
}
const gShow = v => { if (typeof v === 'boolean') return v ? 'Sí' : 'No'; if (v === '' || v == null) return '(vacío)'; const t = String(v); if (t.startsWith('{"type":"root"')) { try { return richText(JSON.parse(t)); } catch (e) { /* texto */ } } return STATUS_ES[t] || t; };
const EXC = { SKU_NOT_FOUND: 'SKU no encontrado', SKU_AMBIGUOUS: 'SKU ambiguo', ENTITY_UNREADABLE: 'No se pudo leer' };

async function runGlobal(path, btn, applied) {
  const st = $('#g-status'); let body;
  try { body = collectBody(); } catch (err) { st.textContent = err.message; return; }
  $$('#g-preview,#g-apply').forEach(b => b.disabled = true); st.textContent = applied ? 'Aplicando en las tiendas…' : 'Comprobando…'; $('#g-result').innerHTML = '';
  try {
    const d = await post(path, body); renderResults(d, applied, $('#g-result'));
    st.textContent = applied ? `Operación terminada${d.operation_id ? ' (registro #' + d.operation_id + ')' : ''}.` : 'Previsualización lista: no se ha escrito nada.';
    if (applied) { shown.historial = false; }
    $('#g-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) { st.textContent = err.message; } finally { $$('#g-preview,#g-apply').forEach(b => b.disabled = false); }
}
function renderResults(d, applied, box) {
  const missing = {};
  d.stores.forEach(s => (s.exceptions || []).forEach(e => { if (e.code === 'METAFIELD_DEFINITION_MISSING') { const k = [e.owner, e.namespace, e.key].join('|'); (missing[k] = missing[k] || { owner: e.owner, namespace: e.namespace, key: e.key, stores: [] }).stores.push(s.store); } }));
  const cards = d.stores.map(s => {
    const items = s.results || s.items || [], real = items.filter(i => (i.changes || []).length);
    const ok = items.filter(i => i.status === 'OK').length, fail = items.filter(i => i.status === 'FAILED').length;
    const badge = s.blocked ? (s.error ? tag('Error', 'err') : tag('Omitida', 'warn')) : applied ? (fail ? tag(`${ok} aplicados · ${fail} con error`, 'err') : tag(`${ok} aplicados`, 'ok')) : tag(`${real.length} se modificarán`, real.length ? 'ok' : 'muted');
    const blocking = (s.exceptions || []).filter(e => !e.sku), perSku = {};
    (s.exceptions || []).filter(e => e.sku).forEach(e => (perSku[e.code] = perSku[e.code] || []).push(e.sku));
    const exc = blocking.map(e => `<div class="callout">${esc(e.message)}</div>`).join('') + Object.entries(perSku).map(([c, skus]) => `<div class="callout"><strong>${esc(EXC[c] || c)} (${skus.length})</strong><div class="skus">${skus.map(x => tag(x)).join('')}</div></div>`).join('');
    const dupBox = (s.duplicates || []).length ? `<div class="callout"><strong>SKU repetidos en ${esc(s.store)}: ${s.duplicates.length} (${s.duplicates.reduce((n, d) => n + d.count, 0)} variantes)</strong><div class="skus">${s.duplicates.map(d => tag(`${d.sku} ×${d.count}`, 'warn', d.items.join(' | '))).join('')}</div></div>` : '';
    const rows = items.map(i => `<tr><td class="nw">${esc((i.skus || []).join(', '))}</td><td class="wrap-cell">${esc(i.title)}</td><td class="wrap-cell diff">${(i.changes || []).map(c => `<div><strong>${esc(c.label)}</strong>: <span class="was">${esc(gShow(c.before))}</span><span class="arrow">→</span><strong>${esc(gShow(c.after))}</strong></div>`).join('') || '<span class="muted">Sin cambios</span>'}</td>${applied ? `<td class="wrap-cell">${i.status === 'OK' ? tag('Aplicado', 'ok') : i.status === 'FAILED' ? tag('Error', 'err') + `<small>${esc(i.error || '')}</small>` : tag('Sin cambios', 'muted')}</td>` : ''}</tr>`).join('');
    return `<details class="res-store"${(s.blocked || fail || (s.exceptions || []).length) ? ' open' : ''}><summary><strong>${esc(s.store)}</strong>${badge}</summary><div>${exc}${dupBox}${rows ? `<div style="margin-top:14px">${table(['SKU', 'Elemento', 'Cambios', ...(applied ? ['Resultado'] : [])], rows)}</div>` : ''}</div></details>`;
  }).join('');
  const total = d.stores.reduce((n, s) => n + (s.results || s.items || []).filter(i => applied ? i.status === 'OK' : (i.changes || []).length).length, 0);
  const omitted = d.stores.filter(s => s.blocked).length;
  box.innerHTML = `<div class="res-summary"><h2>${applied ? 'Resultado' : 'Previsualización'}</h2>${tag(`${total} ${applied ? 'aplicados' : 'se modificarán'}`, total ? 'ok' : 'muted')}${omitted ? tag(`${omitted} ${omitted === 1 ? 'tienda omitida' : 'tiendas omitidas'}`, 'warn') : ''}</div>` +
    Object.values(missing).map((m, i) => `<div class="callout"><strong>${esc(m.namespace + '.' + m.key)}</strong> no existe en ${esc(m.stores.join(', '))}. <div class="btnrow"><button type="button" class="button secondary" data-mk="${i}">Crear en ${esc(m.stores.join(', '))} (copiando la definición de otra tienda)</button></div></div>`).join('') + cards;
  const list = Object.values(missing);
  $$('[data-mk]', box).forEach(b => b.onclick = async () => {
    const m = list[+b.dataset.mk], st = $('#g-status'); b.disabled = true; st.textContent = 'Creando definición…';
    try {
      const r = await post('/api/admin/definitions/create', { owner: m.owner, namespace: m.namespace, key: m.key, stores: m.stores });
      const bad = r.results.filter(x => !x.ok); st.textContent = bad.length ? 'No se pudo en: ' + bad.map(x => `${x.store} (${x.error})`).join('; ') : `Definición creada (copiada de ${r.source}). Comprobando de nuevo…`;
      if (!bad.length) { state.defs = null; state.defsPromise = null; loadDefs().catch(() => {}); runGlobal('/api/admin/global/preview', $('#g-preview'), false); }
    } catch (err) { st.textContent = err.message; } finally { b.disabled = false; }
  });
}

/* ---------- HISTORIAL ---------- */
const KIND_ES = { 'global.apply': 'Edición multitienda', 'global.retry': 'Reintento', 'definition.create': 'Crear definición', 'product.update': 'Producto', 'variant.update': 'Variante', 'collection.update': 'Colección', 'metafield.set': 'Metacampo', 'values.apply': 'Cambio de valores' };
$('#hist-reload').onclick = () => loadHistory();
async function loadHistory() {
  const box = $('#hist'), st = $('#hist-status'); st.textContent = 'Cargando…';
  try {
    const d = await api('/api/admin/operations/summary'); st.textContent = d.operations.length ? '' : 'Todavía no hay operaciones.';
    box.innerHTML = d.operations.length ? table(['#', 'Fecha', 'Operación', 'Tiendas', 'Resultado', ''], d.operations.map(o => `<tr data-op="${o.id}"><td class="nw">${o.id}</td><td class="nw">${esc(o.at.slice(0, 16).replace('T', ' '))}</td><td class="nw">${esc(KIND_ES[o.kind] || o.kind)}</td><td class="nw">${esc(o.stores.join(', '))}</td><td class="nw">${o.ok ? tag(o.ok + ' ok', 'ok') : ''} ${o.failed ? tag(o.failed + ' error', 'err') : ''} ${o.skipped ? tag(o.skipped + ' omitidos', 'muted') : ''}</td><td class="nw"><button type="button" class="button secondary" data-view>Ver</button> ${o.retryable ? '<button type="button" class="button secondary" data-retry>Reintentar fallidos</button>' : ''}</td></tr>`).join('')) : '';
  } catch (err) { st.textContent = err.message; }
}
$('#hist').addEventListener('click', async e => {
  const v = e.target.closest('[data-view]'), r = e.target.closest('[data-retry]'); if (!v && !r) return;
  const tr = e.target.closest('tr'), id = tr.dataset.op, st = $('#hist-status');
  if (v) {
    const next = tr.nextElementSibling; if (next && next.classList.contains('detail')) { next.remove(); return; }
    try {
      const o = await api('/api/admin/operations/' + id);
      const det = document.createElement('tr'); det.className = 'detail';
      det.innerHTML = `<td colspan="6">${table(['Tienda', 'Elemento', 'Estado', 'Cambios', 'Error'], o.items.map(i => `<tr><td class="nw">${esc(i.store)}</td><td class="nw">${esc((i.entity || '').split('/').pop())}</td><td class="nw">${tag(i.status === 'OK' ? 'Aplicado' : i.status === 'FAILED' ? 'Error' : 'Omitido', i.status === 'OK' ? 'ok' : i.status === 'FAILED' ? 'err' : 'muted')}</td><td class="wrap-cell diff">${Object.keys(i.after || {}).map(k => `<div><strong>${esc(k)}</strong>: <span class="was">${esc(gShow((i.before || {})[k]))}</span><span class="arrow">→</span><strong>${esc(gShow(i.after[k]))}</strong></div>`).join('')}</td><td class="wrap-cell">${esc(i.error || '')}</td></tr>`).join(''))}</td>`;
      tr.after(det);
    } catch (err) { st.textContent = err.message; }
  } else {
    r.disabled = true; st.textContent = `Reintentando lo fallido de #${id}…`;
    try { const res = await post(`/api/admin/operations/${id}/retry-failed`); st.textContent = `Reintento terminado (registro #${res.operation_id}).`; await loadHistory(); }
    catch (err) { st.textContent = err.message; r.disabled = false; }
  }
});

/* ---------- arranque ---------- */
(async () => {
  try {
    state.stores = (await api('/api/admin/stores')).stores;
    const saved = mem('adm.store'); state.store = state.stores.some(s => s.code === saved) ? saved : (state.stores.find(s => s.code === 'ES') || state.stores[0] || {}).code || null;
    renderStoreChips();
  } catch (err) { $('#list-status').textContent = err.message; }
  const h = location.hash.slice(1); showTab(TABS.includes(h) ? h : 'tienda', false);
  window.addEventListener('hashchange', () => { const n = location.hash.slice(1); if (TABS.includes(n)) showTab(n, false); });
})();
})();
