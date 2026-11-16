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
const TABS = ['tienda', 'varias', 'metacampos', 'historial'];
const shown = {};
function showTab(name, push = true) {
  if (!TABS.includes(name)) name = 'tienda';
  TABS.forEach(t => {
    const b = $('#tab-' + t), on = t === name;
    b.setAttribute('aria-selected', on); b.tabIndex = on ? 0 : -1; $('#panel-' + t).hidden = !on;
  });
  if (push) history.replaceState(null, '', '#' + name);
  if (!shown[name]) { shown[name] = true; ({ tienda: loadList, varias: initMulti, metacampos: renderMatrix, historial: loadHistory })[name](true); }
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
    row.innerHTML = `<div><div class="k">${esc(m.namespace + '.' + m.key)}</div>${tag(m.type)}</div><div class="slot"></div><div><button type="button" class="button secondary" disabled>Guardar</button> <span class="st muted" role="status"></span></div>`;
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
const SCOPE_HINT = { product: 'Se modifica el producto que contiene cada SKU.', variant: 'Se modifica solo la variante que tiene ese SKU.' };
let scope = 'product', comboSeq = 0;
const OPEN_ROWS = () => $$('#g-changes .change');

function scopeOptions() {
  const out = FIELDS[scope].map(f => ({ ...f, kind: 'field', sub: '', group: scope === 'product' ? 'Campos del producto' : 'Campos de la variante', tagText: '' }));
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
  $('#g-all').onclick = () => $$('#g-stores input').forEach(i => i.checked = true);
  $('#g-none').onclick = () => $$('#g-stores input').forEach(i => i.checked = false);
  $('#g-add').onclick = () => { const r = addChange(); $('input', r).focus(); };
  $('#g-preview').onclick = e => runGlobal('/api/admin/global/preview', e.target, false);
  $('#g-apply').onclick = e => runGlobal('/api/admin/global/apply', e.target, true);
  setScope('product'); loadDefs().catch(() => {});
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
    const rows = items.map(i => `<tr><td class="nw">${esc((i.skus || []).join(', '))}</td><td class="wrap-cell">${esc(i.title)}</td><td class="wrap-cell diff">${(i.changes || []).map(c => `<div><strong>${esc(c.label)}</strong>: <span class="was">${esc(gShow(c.before))}</span><span class="arrow">→</span><strong>${esc(gShow(c.after))}</strong></div>`).join('') || '<span class="muted">Sin cambios</span>'}</td>${applied ? `<td class="wrap-cell">${i.status === 'OK' ? tag('Aplicado', 'ok') : i.status === 'FAILED' ? tag('Error', 'err') + `<small>${esc(i.error || '')}</small>` : tag('Sin cambios', 'muted')}</td>` : ''}</tr>`).join('');
    return `<details class="res-store"${(s.blocked || fail || (s.exceptions || []).length) ? ' open' : ''}><summary><strong>${esc(s.store)}</strong>${badge}</summary><div>${exc}${rows ? `<div style="margin-top:14px">${table(['SKU', 'Elemento', 'Cambios', ...(applied ? ['Resultado'] : [])], rows)}</div>` : ''}</div></details>`;
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
const KIND_ES = { 'global.apply': 'Edición multitienda', 'global.retry': 'Reintento', 'definition.create': 'Crear definición', 'product.update': 'Producto', 'variant.update': 'Variante', 'collection.update': 'Colección', 'metafield.set': 'Metacampo' };
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
