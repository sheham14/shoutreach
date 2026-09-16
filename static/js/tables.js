// ── Shared pieces for the channel pages ──────────────────────────────────────
//
// Contacts, Email, Calling and WhatsApp each have a leads table, and they all
// work the same way: filter, sort, page, tick rows, act on the ticked ones.
// Built once here so the four can't drift apart -- somebody who learns the
// table on one page already knows it on the other three.

// ── Tabs ─────────────────────────────────────────────────────────────────────

const _tabHandlers = {};

// Pages register what to load when one of their tabs opens.
function onTab(group, handler) { _tabHandlers[group] = handler; }

function setTab(group, name, { load = true } = {}) {
  document.querySelectorAll(`[data-tab-group="${group}"] .tab`).forEach(b =>
    b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll(`[data-pane-group="${group}"]`).forEach(p =>
    p.classList.toggle('active', p.dataset.pane === name));
  try { localStorage.setItem(`tab:${group}`, name); } catch (_) { /* private mode */ }
  if (load && _tabHandlers[group]) _tabHandlers[group](name);
}

function currentTab(group, fallback) {
  const on = document.querySelector(`[data-tab-group="${group}"] .tab.active`);
  if (on) return on.dataset.tab;
  try { return localStorage.getItem(`tab:${group}`) || fallback; } catch (_) { return fallback; }
}

function setTabCount(group, name, n, hot = false) {
  const el = document.querySelector(`[data-tab-group="${group}"] .tab[data-tab="${name}"] .count`);
  if (!el) return;
  el.textContent = n ?? 0;
  el.classList.toggle('hot', !!hot && n > 0);
}

// ── Pills ────────────────────────────────────────────────────────────────────

function pill(text, tone = '', title = '') {
  return `<span class="pill ${tone}"${title ? ` title="${esc(title)}"` : ''}>${esc(text)}</span>`;
}

const WA_STAGE_META = {
  ready:    ['Ready to send', 'blue'],
  due:      ['Follow-up due', 'amber'],
  waiting:  ['Waiting for reply', ''],
  replied:  ['Replied', 'green'],
  paused:   ['Paused', ''],
  no_whatsapp: ['Not on WhatsApp', 'red'],
  moved:    ['Moved off WhatsApp', 'dashed'],
  removed:  ['Taken off', 'dashed'],
};

function waStagePill(stage) {
  const [label, tone] = WA_STAGE_META[stage] || [stage || '—', ''];
  return pill(label, tone);
}

// A wa.me number, stored as bare digits, laid out the way people read it.
function prettyWaNumber(n) {
  const d = String(n || '').replace(/\D/g, '');
  if (!d) return '';
  if (d.startsWith('971') && d.length === 12) return `+971 ${d.slice(3, 5)} ${d.slice(5, 8)} ${d.slice(8)}`;
  if (d.startsWith('971') && d.length === 11) return `+971 ${d.slice(3, 4)} ${d.slice(4, 7)} ${d.slice(7)}`;
  if (d.startsWith('974') && d.length === 11) return `+974 ${d.slice(3, 7)} ${d.slice(7)}`;
  // Anywhere else: split off the country code (known once the country list has
  // loaded) and group the rest in threes, the last group taking up to four.
  const list = (typeof _countries !== 'undefined' && _countries) ? _countries.list : [];
  const dial = list.map(c => String(c.dial)).filter(code => d.startsWith(code))
    .sort((a, b) => b.length - a.length)[0];
  if (!dial) return `+${d}`;
  const rest = d.slice(dial.length);
  const groups = [];
  let i = 0;
  while (rest.length - i > 4) { groups.push(rest.slice(i, i + 3)); i += 3; }
  groups.push(rest.slice(i));
  return `+${dial} ${groups.join(' ')}`;
}

function shortDate(s) {
  if (!s) return '';
  const d = String(s).replace('T', ' ');
  return d.substring(0, 10) === new Date().toISOString().substring(0, 10) ? d.substring(11, 16) : d.substring(0, 10);
}

// ── Placeholders ─────────────────────────────────────────────────────────────
//
// The same {{name|fallback}} rules the server uses to write the real message,
// so a preview shows what will actually be sent. `highlight` marks what got
// filled in, and in amber anything that came out empty.

function fillPlaceholders(text, fields, { highlight = false } = {}) {
  const src = String(text || '');
  const re = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|([^}]*))?\}\}/g;
  let out = '', last = 0, m;
  while ((m = re.exec(src)) !== null) {
    out += highlight ? esc(src.slice(last, m.index)) : src.slice(last, m.index);
    const key = m[1], fallback = m[2];
    let v = fields[key];
    let empty = v === null || v === undefined || String(v).trim() === '';
    if (empty) v = fallback !== undefined ? fallback.trim() : ((key === 'business_name' || key === 'company') ? 'there' : '');
    v = String(v).trim();
    out += highlight
      ? `<span class="fill${empty && fallback === undefined && key !== 'business_name' ? ' empty' : ''}">${esc(v) || '&nbsp;?&nbsp;'}</span>`
      : v;
    last = re.lastIndex;
  }
  out += highlight ? esc(src.slice(last)) : src.slice(last);
  return out;
}

// What a WhatsApp template can say about a lead, keyed the way templates name them.
function waFieldsFor(lead, variables = {}) {
  const f = { ...variables };
  ['city', 'category', 'rating', 'review_count', 'website', 'address', 'phone'].forEach(k => {
    if (lead[k] !== null && lead[k] !== undefined && lead[k] !== '') f[k] = lead[k];
  });
  f.business_name = lead.company || lead.name || '';
  f.company = f.business_name;
  return f;
}

// Insert text at the cursor of a textarea, for the placeholder buttons.
function insertAtCursor(el, text) {
  if (!el) return;
  const start = el.selectionStart ?? el.value.length;
  const end = el.selectionEnd ?? el.value.length;
  el.value = el.value.slice(0, start) + text + el.value.slice(end);
  el.focus();
  el.selectionStart = el.selectionEnd = start + text.length;
  el.dispatchEvent(new Event('input'));
}

// ── A dialog for choices that used to be prompt() ────────────────────────────
//
// Typing "call" or a campaign number into a browser prompt was the least
// intuitive thing in the app. One modal serves every "pick where this goes"
// question: pass the body HTML and a function that reads the answer out of it.

let _chooseResolve = null;
let _chooseCollect = null;

function chooseDialog({ title, body, confirm = 'OK', danger = false, collect = () => true, width = 480 }) {
  document.getElementById('choose-title').textContent = title;
  document.getElementById('choose-body').innerHTML = body;
  const btn = document.getElementById('choose-confirm');
  btn.textContent = confirm;
  btn.className = `btn ${danger ? 'btn-danger' : 'btn-primary'}`;
  document.querySelector('#modal-choose .modal').style.maxWidth = `${width}px`;
  _chooseCollect = collect;
  openModal('modal-choose');
  return new Promise(resolve => { _chooseResolve = resolve; });
}

function chooseConfirm() {
  const value = _chooseCollect ? _chooseCollect() : true;
  if (value === undefined || value === null || value === false) return;  // collect() showed why
  closeModal('modal-choose');
  const r = _chooseResolve; _chooseResolve = null;
  if (r) r(value);
}

function chooseCancel() {
  closeModal('modal-choose');
  const r = _chooseResolve; _chooseResolve = null;
  if (r) r(null);
}

// Radio "choice cards" inside a chooseDialog body.
function choiceCard({ name, value, title, hint = '', checked = false, disabled = false, extra = '' }) {
  return `<label class="choice${checked ? ' active' : ''}${disabled ? ' disabled' : ''}">
    <input type="radio" name="${name}" value="${esc(value)}" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''}
           onchange="document.querySelectorAll('input[name=${name}]').forEach(i => i.closest('.choice').classList.toggle('active', i.checked))" />
    <div><div class="title">${esc(title)}</div>${hint ? `<div class="hint">${hint}</div>` : ''}${extra}</div>
  </label>`;
}

function chosenRadio(name) {
  const el = document.querySelector(`input[name=${name}]:checked`);
  return el ? el.value : null;
}

// A campaign <select> with a "new campaign" option that reveals a name box.
// Resolves the choice to an id, creating the campaign when asked to.
function campaignSelectHtml(id, campaigns, { allowNone = false, noneLabel = 'No campaign', selected = '', allowNew = true } = {}) {
  return `<select id="${id}" class="filter-select" style="max-width:100%;width:100%"
                  onchange="document.getElementById('${id}-new').style.display = this.value === '__new' ? 'block' : 'none'">
      ${allowNone ? `<option value="">${esc(noneLabel)}</option>` : ''}
      ${campaigns.map(c => `<option value="${c.id}" ${String(c.id) === String(selected) ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}
      ${allowNew ? '<option value="__new">+ New campaign…</option>' : ''}
    </select>
    <input id="${id}-new" class="soft-input" placeholder="Name the new campaign" style="display:none;margin-top:8px" />`;
}

async function resolveCampaignSelect(id, createUrl, extra = {}) {
  const sel = document.getElementById(id);
  if (!sel) return '';
  if (sel.value !== '__new') return sel.value;
  const name = (document.getElementById(`${id}-new`).value || '').trim();
  if (!name) { toast('Name the new campaign', 'err'); return null; }
  const res = await api(createUrl, 'POST', { name, ...extra });
  if (!res || res.error) { toast((res && res.error) || 'Could not create the campaign', 'err'); return null; }
  return String(res.id);
}

// ── Lead table ───────────────────────────────────────────────────────────────

const LT = {};

document.addEventListener('click', e => {
  document.querySelectorAll('.row-menu.open').forEach(m => { if (!m.contains(e.target)) m.classList.remove('open'); });
});

function toggleRowMenu(btn) {
  const menu = btn.closest('.row-menu');
  const open = !menu.classList.contains('open');
  document.querySelectorAll('.row-menu.open').forEach(m => m.classList.remove('open'));
  menu.classList.toggle('open', open);
}

/*
 * cfg:
 *   id        -- key in LT, and the prefix of this table's element ids
 *   url       -- list endpoint returning {rows, total, page, pages}
 *   idsUrl    -- optional; endpoint returning {ids} for "select all N matching"
 *   params()  -- the current filters, as an object
 *   columns   -- [{key, label, sort, render(row), cls}]
 *   rowId(r)  -- defaults to r.id
 *   bulk()    -- HTML of the actions shown when rows are ticked
 *   menu(r)   -- [{label, run: "js", danger}] for the row's ⋯ menu
 *   onRowClick(r) -- optional
 *   empty     -- text when nothing matches
 *   onLoad(data)  -- optional
 */
function createLeadTable(cfg) {
  const t = {
    cfg, page: 1, perPage: cfg.perPage || 50, sortCol: '', sortDir: 'desc',
    rows: [], total: 0, pages: 1, selected: new Set(), currentId: null, _timer: null,
  };
  const rowId = r => (cfg.rowId ? cfg.rowId(r) : r.id);
  const el = suffix => document.getElementById(`${cfg.id}-${suffix}`);

  t.query = (extra = {}) => {
    const p = new URLSearchParams();
    Object.entries({ ...(cfg.params ? cfg.params() : {}), ...extra }).forEach(([k, v]) => {
      if (v !== '' && v !== null && v !== undefined && v !== false) p.set(k, v);
    });
    return p.toString();
  };

  t.load = async ({ resetPage = false, keepSelection = true } = {}) => {
    if (resetPage) t.page = 1;
    if (!keepSelection) t.selected.clear();
    const extra = { page: t.page, per_page: t.perPage };
    if (t.sortCol) { extra.sort_col = t.sortCol; extra.sort_dir = t.sortDir; }
    const data = await api(`${cfg.url}?${t.query(extra)}`);
    if (!data || data.error || !data.rows) { toast((data && data.error) || 'Could not load the list', 'err'); return; }
    t.rows = data.rows; t.total = data.total; t.pages = data.pages || 1; t.page = data.page || t.page;
    t.data = data;
    t.render();
    if (cfg.onLoad) cfg.onLoad(data);
  };

  t.search = () => { clearTimeout(t._timer); t._timer = setTimeout(() => t.load({ resetPage: true }), 280); };
  t.filter = () => t.load({ resetPage: true, keepSelection: false });
  t.step = dir => { const n = t.page + dir; if (n < 1 || n > t.pages) return; t.page = n; t.load(); };
  t.sort = key => {
    if (t.sortCol === key) t.sortDir = t.sortDir === 'asc' ? 'desc' : 'asc';
    else { t.sortCol = key; t.sortDir = 'asc'; }
    t.load({ resetPage: true });
  };
  t.toggle = (id, on) => { on ? t.selected.add(id) : t.selected.delete(id); t.render(); };
  t.togglePage = on => { t.rows.forEach(r => on ? t.selected.add(rowId(r)) : t.selected.delete(rowId(r))); t.render(); };
  t.clear = () => { t.selected.clear(); t.render(); };
  t.selectedIds = () => [...t.selected];
  t.selectAllMatching = async () => {
    if (!cfg.idsUrl) return;
    const res = await api(`${cfg.idsUrl}?${t.query()}`);
    if (!res || !res.ids) { toast('Could not select them all', 'err'); return; }
    res.ids.forEach(id => t.selected.add(id));
    t.render();
  };
  t.rowById = id => t.rows.find(r => String(rowId(r)) === String(id));
  t.click = id => { const r = t.rowById(id); if (r && cfg.onRowClick) { t.currentId = rowId(r); t.render(); cfg.onRowClick(r); } };

  t.render = () => {
    const body = el('body');
    if (!body) return;
    const pageIds = t.rows.map(rowId);
    const allOnPage = pageIds.length > 0 && pageIds.every(id => t.selected.has(id));
    const n = t.selected.size;

    const count = el('count');
    if (count) {
      const from = t.total ? (t.page - 1) * t.perPage + 1 : 0;
      const to = Math.min(t.page * t.perPage, t.total);
      count.textContent = t.total ? `${from}–${to} of ${t.total}` : '0';
    }

    const more = allOnPage && cfg.idsUrl && t.total > t.rows.length && n < t.total
      ? ` <a onclick="LT['${cfg.id}'].selectAllMatching()">Select all ${t.total}</a>` : '';
    const bulk = `<div class="lt-bulk ${n ? 'show' : ''}">
        <span class="sel">${n} selected</span>${more}
        <a onclick="LT['${cfg.id}'].clear()">Clear</a>
        <span style="flex-basis:8px"></span>
        ${n && cfg.bulk ? cfg.bulk() : ''}
      </div>`;

    const head = `<tr>
        <th class="check"><input type="checkbox" ${allOnPage ? 'checked' : ''} title="Select this page"
             onchange="LT['${cfg.id}'].togglePage(this.checked)" /></th>
        ${cfg.columns.map(c => {
          if (!c.sort) return `<th>${esc(c.label)}</th>`;
          const on = t.sortCol === c.key;
          return `<th class="sortable" onclick="LT['${cfg.id}'].sort('${c.key}')">${esc(c.label)}<span class="arrow">${on ? (t.sortDir === 'asc' ? '▲' : '▼') : '⇅'}</span></th>`;
        }).join('')}
        ${cfg.menu ? '<th style="width:40px"></th>' : ''}
      </tr>`;

    const colspan = cfg.columns.length + 1 + (cfg.menu ? 1 : 0);
    const rows = t.rows.length ? t.rows.map(r => {
      const id = rowId(r);
      const idLit = JSON.stringify(id).replace(/"/g, '&quot;');
      const menu = cfg.menu ? cfg.menu(r).filter(Boolean) : [];
      return `<tr class="${cfg.onRowClick ? 'clickable' : ''} ${t.selected.has(id) ? 'selected' : ''} ${String(t.currentId) === String(id) ? 'current' : ''}"
                  ${cfg.onRowClick ? `onclick="LT['${cfg.id}'].click(${idLit})"` : ''}>
          <td class="check" onclick="event.stopPropagation()"><input type="checkbox" ${t.selected.has(id) ? 'checked' : ''}
               onchange="LT['${cfg.id}'].toggle(${idLit}, this.checked)" /></td>
          ${cfg.columns.map(c => `<td class="${c.cls || ''}">${c.render ? c.render(r) : esc(r[c.key] ?? '')}</td>`).join('')}
          ${cfg.menu ? `<td onclick="event.stopPropagation()">${menu.length ? `<div class="row-menu">
              <button class="btn btn-ghost btn-sm" onclick="toggleRowMenu(this)" title="More">⋯</button>
              <div class="row-menu-list">${menu.map(m =>
                `<button class="${m.danger ? 'danger' : ''}" onclick="this.closest('.row-menu').classList.remove('open');${esc(m.run)}">${esc(m.label)}</button>`).join('')}</div>
            </div>` : ''}</td>` : ''}
        </tr>`;
    }).join('') : `<tr><td colspan="${colspan}"><div class="empty-state"><p>${esc(cfg.empty || 'Nothing here')}</p></div></td></tr>`;

    body.innerHTML = `${bulk}
      <div class="table-wrap"><table class="lead-table"><thead>${head}</thead><tbody>${rows}</tbody></table></div>
      ${t.pages > 1 ? `<div class="lt-pager">
          <button class="btn btn-ghost btn-sm" ${t.page <= 1 ? 'disabled' : ''} onclick="LT['${cfg.id}'].step(-1)">← Prev</button>
          <span class="text-muted text-small mono">Page ${t.page} of ${t.pages}</span>
          <button class="btn btn-ghost btn-sm" ${t.page >= t.pages ? 'disabled' : ''} onclick="LT['${cfg.id}'].step(1)">Next →</button>
        </div>` : ''}`;
  };

  LT[cfg.id] = t;
  return t;
}

// Summaries of "added 3, skipped 2 because…" responses, shared by every
// add-to-a-channel action so the wording is the same everywhere.
function describeAdd(res, verb = 'Added') {
  const bits = [];
  if (res.already)   bits.push(`${res.already} already there`);
  if (res.no_phone)  bits.push(`${res.no_phone} had no phone number`);
  if (res.no_email)  bits.push(`${res.no_email} had no email address`);
  if (res.opted_out) bits.push(`${res.opted_out} asked not to be contacted`);
  if (res.ruled_out) bits.push(`${res.ruled_out} already ruled out as not on WhatsApp`);
  const n = res.added ?? res.enrolled ?? 0;
  return `${verb} ${n}` + (bits.length ? ` — ${bits.join(', ')}` : '');
}
