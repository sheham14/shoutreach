let dbCurrentTable = null;
let dbCurrentPage  = 1;
let dbLastGoodPage = 1;   // reverted to on a failed fetch, so Next/Prev can't drift out of sync
let dbSearchTimer  = null;
let dbSortCol      = '';
let dbSortDir      = 'desc';
let dbHiddenCols   = new Set();

async function loadDbTables() {
  const tables = await api('/api/db/tables');
  const el = document.getElementById('db-table-pills');
  el.innerHTML = tables.map(t => `
    <button class="btn btn-ghost" id="dbtab-${t.name}" onclick="openDbTable('${t.name}')">
      ${esc(t.name)}
      <span class="badge badge-gray" style="margin-left:6px">${t.count}</span>
    </button>
  `).join('');
  if (tables.length) openDbTable(tables[0].name);
}

async function openDbTable(name) {
  dbCurrentTable = name;
  dbCurrentPage  = 1;
  dbLastGoodPage = 1;
  dbSortCol      = '';
  dbSortDir      = 'desc';
  dbHiddenCols   = new Set();
  document.getElementById('db-search').value = '';
  document.getElementById('db-col-dropdown').innerHTML = '';
  document.querySelectorAll('[id^="dbtab-"]').forEach(b => {
    b.classList.remove('btn-primary');
    b.classList.add('btn-ghost');
  });
  const tab = document.getElementById(`dbtab-${name}`);
  if (tab) { tab.classList.remove('btn-ghost'); tab.classList.add('btn-primary'); }
  document.getElementById('db-table-view').style.display = 'block';
  document.getElementById('db-table-name').textContent = name;
  await fetchDbPage();
}

async function fetchDbPage() {
  const q = encodeURIComponent(document.getElementById('db-search').value.trim());
  let url = `/api/db/table/${dbCurrentTable}?page=${dbCurrentPage}&q=${q}`;
  if (dbSortCol) url += `&sort_col=${encodeURIComponent(dbSortCol)}&sort_dir=${dbSortDir}`;
  const data = await api(url);

  if (data.error || !data.rows) {
    // A failed request used to throw here on data.rows.length with no
    // feedback -- the page number had already been bumped by dbPage(), so
    // the Next button looked like it did nothing while quietly drifting out
    // of sync with what was on screen. Revert to the last page that actually
    // loaded so the next click resumes from where the user really is.
    toast(data.error || 'Failed to load this page', 'err');
    dbCurrentPage = dbLastGoodPage;
    return;
  }
  dbLastGoodPage = data.page;

  document.getElementById('db-row-count').textContent = `${data.total} rows`;
  document.getElementById('db-page-label').textContent =
    `Page ${data.page} of ${data.pages}`;
  document.getElementById('db-prev').disabled = data.page <= 1;
  document.getElementById('db-next').disabled = data.page >= data.pages;

  const visibleCols = data.columns.filter(c => !dbHiddenCols.has(c));

  document.getElementById('db-thead').innerHTML = '<tr>' + visibleCols.map(c => {
    const isSorted = data.sort_col === c;
    const arrow    = isSorted ? (data.sort_dir === 'asc' ? '▲' : '▼') : '⇅';
    const nextDir  = (isSorted && data.sort_dir === 'asc') ? 'desc' : 'asc';
    return `<th style="cursor:pointer;user-select:none;white-space:nowrap"
                onclick="dbSort('${c}','${nextDir}')">
              ${esc(c)}&nbsp;<span style="opacity:0.45;font-size:10px">${arrow}</span>
            </th>`;
  }).join('') + '</tr>';

  if (!data.rows.length) {
    document.getElementById('db-tbody').innerHTML =
      `<tr><td colspan="${visibleCols.length}"><div class="empty-state"><p>No rows</p></div></td></tr>`;
    _renderColDropdown(data.columns);
    return;
  }

  document.getElementById('db-tbody').innerHTML = data.rows.map(row =>
    '<tr>' + visibleCols.map(col => {
      const raw     = row[col] ?? '';
      const str     = String(raw);
      const display = str.length > 80 ? str.slice(0, 80) + '…' : str;
      return `<td class="mono" style="font-size:12px;max-width:280px;overflow:hidden;
              text-overflow:ellipsis;white-space:nowrap" title="${esc(str)}">${esc(display)}</td>`;
    }).join('') + '</tr>'
  ).join('');

  _renderColDropdown(data.columns);
}

function _renderColDropdown(allCols) {
  const dd = document.getElementById('db-col-dropdown');
  if (!dd) return;
  dd.innerHTML = allCols.map(c => `
    <label style="display:flex;align-items:center;gap:8px;padding:5px 14px;cursor:pointer;
                  white-space:nowrap;font-size:13px;color:var(--text)">
      <input type="checkbox" ${dbHiddenCols.has(c) ? '' : 'checked'}
             onchange="dbToggleCol('${c}',this.checked)" style="cursor:pointer">
      ${esc(c)}
    </label>
  `).join('');
}

function dbToggleCol(col, visible) {
  if (visible) dbHiddenCols.delete(col);
  else dbHiddenCols.add(col);
  fetchDbPage();
}

function dbToggleColDropdown() {
  const dd = document.getElementById('db-col-dropdown');
  dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
}

document.addEventListener('click', e => {
  const wrap = document.getElementById('db-col-toggle-wrap');
  if (wrap && !wrap.contains(e.target)) {
    const dd = document.getElementById('db-col-dropdown');
    if (dd) dd.style.display = 'none';
  }
});

function dbSort(col, dir) {
  dbSortCol = col;
  dbSortDir = dir;
  dbCurrentPage = 1;
  fetchDbPage();
}

function dbPage(dir) {
  dbCurrentPage = Math.max(1, dbCurrentPage + dir);
  fetchDbPage();
}

function dbSearch() {
  clearTimeout(dbSearchTimer);
  dbSearchTimer = setTimeout(() => { dbCurrentPage = 1; fetchDbPage(); }, 300);
}

function dbExport() {
  window.location.href = `/api/db/table/${dbCurrentTable}/export`;
}
