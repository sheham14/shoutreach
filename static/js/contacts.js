const CONTACT_COLS = [
  { key: 'email',         label: 'Email' },
  { key: 'first_name',    label: 'First Name' },
  { key: 'last_name',     label: 'Last Name' },
  { key: 'company',       label: 'Company' },
  { key: 'phone',         label: 'Phone' },
  { key: 'website',       label: 'Website' },
  { key: 'rating',        label: 'Rating' },
  { key: 'review_count',  label: 'Reviews' },
  { key: 'category',      label: 'Category' },
  { key: 'address',       label: 'Address' },
  { key: 'source_job_id', label: 'List' },
  { key: 'status',        label: 'Status' },
  { key: 'created_at',    label: 'Added' },
];

// Hidden by default purely to keep the table narrow enough to read — all of
// them are one click away under ⊞ Columns.
let contactHiddenCols  = new Set(['address', 'category']);
let contactSortCol     = '';
let contactSortDir     = 'desc';
let contactEditId      = null;
let contactSelectedIds = new Set();

// Server-side paging state. The table used to hold every contact in the
// browser; it now holds one page, and every filter is applied in SQL.
let contactRows        = [];
let contactPage        = 1;
let contactPerPage     = 50;
let contactTotal       = 0;
let contactPages       = 1;
let contactSourceId    = '';     // '' = every list, 'manual' = hand-added/CSV
let contactSources     = [];
let contactSearchTimer = null;

async function loadContacts() {
  contactPage = 1;
  contactSelectedIds.clear();
  await loadContactSources();
  _renderContactColDropdown();
  await fetchContactsPage();
  loadUnsubscribed();
  loadInvalidMx();
}

async function loadContactSources() {
  contactSources = await api('/api/contacts/sources') || [];
  const sel = document.getElementById('contacts-source-filter');
  if (!sel) return;
  const total = contactSources.reduce((n, s) => n + s.count, 0);
  sel.innerHTML =
    `<option value="">All lists (${total})</option>` +
    contactSources.map(s =>
      `<option value="${esc(String(s.job_id))}">${esc(s.label)} (${s.count})</option>`
    ).join('');
  sel.value = contactSourceId;
}

function _contactQueryString(extra = {}) {
  const p = new URLSearchParams();
  const q = (document.getElementById('contacts-search')?.value || '').trim();
  if (q) p.set('q', q);
  if (contactSourceId) p.set('source_job_id', contactSourceId);
  const status = document.getElementById('contacts-status-filter')?.value || '';
  if (status) p.set('status', status);
  if (document.getElementById('contacts-show-deleted')?.checked) p.set('include_deleted', '1');
  Object.entries(extra).forEach(([k, v]) => p.set(k, v));
  return p.toString();
}

async function fetchContactsPage() {
  const params = { page: contactPage, per_page: contactPerPage };
  if (contactSortCol) { params.sort_col = contactSortCol; params.sort_dir = contactSortDir; }
  const data = await api('/api/contacts?' + _contactQueryString(params));

  if (!data || data.error || !data.rows) {
    toast((data && data.error) || 'Failed to load contacts', 'err');
    return;
  }

  contactRows  = data.rows;
  contactTotal = data.total;
  contactPages = data.pages;
  contactPage  = data.page;
  // Kept in sync so the edit/delete handlers, which look a contact up by id,
  // still find the row the user just clicked.
  allContacts  = data.rows;

  renderContactsTable();
  _updateDeleteSelectedBtn();
}

function contactSearch() {
  clearTimeout(contactSearchTimer);
  contactSearchTimer = setTimeout(() => {
    contactPage = 1;
    fetchContactsPage();
  }, 300);
}

function contactFilterChanged() {
  contactSourceId = document.getElementById('contacts-source-filter')?.value || '';
  contactPage = 1;
  fetchContactsPage();
}

function contactPageStep(dir) {
  const next = contactPage + dir;
  if (next < 1 || next > contactPages) return;
  contactPage = next;
  fetchContactsPage();
}

let _unsubscribed = [];

async function loadUnsubscribed() {
  _unsubscribed = await api('/api/contacts/unsubscribed');
  const countEl = document.getElementById('unsub-count');
  const tbody   = document.getElementById('unsub-table');
  if (!countEl || !tbody) return;
  countEl.textContent = _unsubscribed.length ? `${_unsubscribed.length} contacts` : '';
  if (!_unsubscribed.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state"><p>No unsubscribes yet</p></td></tr>';
    return;
  }
  tbody.innerHTML = _unsubscribed.map(c => `<tr>
    <td class="mono" style="font-size:12px">${esc(c.email || '—')}</td>
    <td>${esc([c.first_name, c.last_name].filter(Boolean).join(' ') || '—')}</td>
    <td>${esc(c.company || '—')}</td>
    <td class="mono text-muted" style="font-size:11px">${(c.created_at || '').substring(0, 10)}</td>
  </tr>`).join('');
}

function exportUnsubscribed() {
  if (!_unsubscribed.length) { toast('No unsubscribes to export', 'err'); return; }
  const rows = [['Email','First Name','Last Name','Company','Date']];
  _unsubscribed.forEach(c => rows.push([
    c.email || '', c.first_name || '', c.last_name || '',
    c.company || '', (c.created_at || '').substring(0, 10),
  ]));
  const csv  = rows.map(r => r.map(v => `"${String(v).replace(/"/g,'""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const a    = Object.assign(document.createElement('a'), {
    href: URL.createObjectURL(blob), download: 'unsubscribed.csv',
  });
  a.click();
}

// ── Invalid MX ────────────────────────────────────────────────────────────────

let _invalidMx = [];

async function loadInvalidMx() {
  _invalidMx = await api('/api/contacts/invalid-mx');
  const countEl = document.getElementById('invalid-mx-count');
  const tbody   = document.getElementById('invalid-mx-table');
  if (!countEl || !tbody) return;
  countEl.textContent = _invalidMx.length ? `${_invalidMx.length} contacts` : '';
  if (!_invalidMx.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state"><p>No invalid emails found yet</p></td></tr>';
    return;
  }
  tbody.innerHTML = _invalidMx.map(c => `<tr>
    <td class="mono" style="font-size:12px">${esc(c.email || '—')}</td>
    <td>${esc(c.company || '—')}</td>
    <td class="mono text-muted" style="font-size:12px">${esc(c.website || '—')}</td>
    <td class="mono text-muted" style="font-size:11px">${(c.created_at || '').substring(0, 10)}</td>
  </tr>`).join('');
}

function exportInvalidMx() {
  if (!_invalidMx.length) { toast('No invalid emails to export', 'err'); return; }
  const rows = [['Email','Company','Website','Address','Date']];
  _invalidMx.forEach(c => rows.push([
    c.email || '', c.company || '', c.website || '',
    c.address || '', (c.created_at || '').substring(0, 10),
  ]));
  const csv  = rows.map(r => r.map(v => `"${String(v).replace(/"/g,'""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const a    = Object.assign(document.createElement('a'), {
    href: URL.createObjectURL(blob), download: 'invalid-mx-emails.csv',
  });
  a.click();
}

function _sourceLabel(jobId) {
  if (jobId === null || jobId === undefined || jobId === '') return 'Manual / CSV';
  const hit = contactSources.find(s => String(s.job_id) === String(jobId));
  return hit ? hit.label : `Scrape #${jobId}`;
}

function renderContactsTable() {
  const visibleCols = CONTACT_COLS.filter(c => !contactHiddenCols.has(c.key));
  const pageIds     = contactRows.map(c => c.id);
  const pageAllSel  = pageIds.length > 0 && pageIds.every(id => contactSelectedIds.has(id));

  const from = contactTotal === 0 ? 0 : (contactPage - 1) * contactPerPage + 1;
  const to   = Math.min(contactPage * contactPerPage, contactTotal);
  document.getElementById('contacts-count').textContent =
    contactTotal ? `${from}–${to} of ${contactTotal}` : '0 contacts';

  const pageLabel = document.getElementById('contacts-page-label');
  if (pageLabel) pageLabel.textContent = `Page ${contactPage} of ${contactPages}`;
  const prevBtn = document.getElementById('contacts-prev');
  const nextBtn = document.getElementById('contacts-next');
  if (prevBtn) prevBtn.disabled = contactPage <= 1;
  if (nextBtn) nextBtn.disabled = contactPage >= contactPages;

  document.getElementById('contacts-thead').innerHTML = '<tr>' +
    `<th style="width:36px"><input type="checkbox" ${pageAllSel ? 'checked' : ''}
        onchange="toggleSelectAllContacts(this.checked)" style="cursor:pointer"
        title="Select everything on this page" /></th>` +
    visibleCols.map(col => {
      const active  = contactSortCol === col.key;
      const arrow   = active ? (contactSortDir === 'asc' ? '▲' : '▼') : '⇅';
      const nextDir = (active && contactSortDir === 'asc') ? 'desc' : 'asc';
      return `<th style="cursor:pointer;user-select:none;white-space:nowrap"
                  onclick="contactSort('${col.key}','${nextDir}')">
                ${col.label}&nbsp;<span style="opacity:0.45;font-size:10px">${arrow}</span>
              </th>`;
    }).join('') +
    '<th style="width:80px"></th></tr>';

  const tbody = document.getElementById('contacts-table');
  if (!contactRows.length) {
    tbody.innerHTML = `<tr><td colspan="${visibleCols.length + 2}" class="empty-state"><p>No contacts found.</p></td></tr>`;
    _renderSelectAllMatchingBar();
    return;
  }

  tbody.innerHTML = contactRows.map(c => {
    const checked = contactSelectedIds.has(c.id) ? 'checked' : '';
    const cells = visibleCols.map(col => {
      if (col.key === 'status') return `<td>${contactStatusBadge(c.status)}</td>`;
      if (col.key === 'created_at') {
        const d = (c.created_at || '').split('T')[0] || (c.created_at || '').split(' ')[0];
        return `<td class="mono text-muted" style="font-size:11px">${esc(d)}</td>`;
      }
      if (col.key === 'company') {
        return `<td>${esc(c.company)}${contactSignalPill(c)}</td>`;
      }
      if (col.key === 'email') return `<td class="mono" style="font-size:12px">${esc(c.email)}</td>`;
      if (col.key === 'phone') return `<td class="mono" style="font-size:12px">${esc(c.phone)}</td>`;
      if (col.key === 'rating') {
        return `<td class="mono" style="font-size:12px">${c.rating == null ? '' : esc(c.rating) + '★'}</td>`;
      }
      if (col.key === 'review_count') {
        return `<td class="mono text-muted" style="font-size:12px">${c.review_count == null ? '' : esc(c.review_count)}</td>`;
      }
      if (col.key === 'source_job_id') {
        return `<td class="text-muted" style="font-size:11px">${esc(_sourceLabel(c.source_job_id))}</td>`;
      }
      return `<td>${esc(c[col.key])}</td>`;
    }).join('');
    return `<tr>
      <td><input type="checkbox" ${checked} onchange="toggleContactSelect(${c.id}, this.checked)" style="cursor:pointer" /></td>
      ${cells}
      <td style="white-space:nowrap">
        <button class="btn btn-ghost btn-sm" onclick="openEditContactModal(${c.id})" title="Edit">✎</button>
        <button class="btn btn-danger btn-sm" onclick="deleteContact(${c.id})" title="Delete">✕</button>
      </td>
    </tr>`;
  }).join('');

  _renderSelectAllMatchingBar();
}

// Select-all ticks the current page only. Silently selecting rows the user
// cannot see would make "Delete Selected" far more destructive than it looks,
// so reaching the rest of a filtered list is a separate, explicit click.
function _renderSelectAllMatchingBar() {
  const bar = document.getElementById('contacts-selectall-bar');
  if (!bar) return;
  const pageIds    = contactRows.map(c => c.id);
  const pageAllSel = pageIds.length > 0 && pageIds.every(id => contactSelectedIds.has(id));
  const more       = contactTotal > contactRows.length;

  if (pageAllSel && more && contactSelectedIds.size < contactTotal) {
    bar.style.display = 'block';
    bar.innerHTML =
      `All ${contactRows.length} on this page are selected.
       <a href="#" onclick="selectAllMatching();return false"
          style="color:var(--blue);text-decoration:underline">
         Select all ${contactTotal} matching this filter</a>`;
  } else if (contactSelectedIds.size > 0) {
    bar.style.display = 'block';
    bar.innerHTML =
      `${contactSelectedIds.size} selected.
       <a href="#" onclick="clearContactSelection();return false"
          style="color:var(--blue);text-decoration:underline">Clear selection</a>`;
  } else {
    bar.style.display = 'none';
    bar.innerHTML = '';
  }
}

async function selectAllMatching() {
  const res = await api('/api/contacts/ids?' + _contactQueryString());
  if (!res || !res.ids) { toast('Could not expand the selection', 'err'); return; }
  res.ids.forEach(id => contactSelectedIds.add(id));
  _updateDeleteSelectedBtn();
  renderContactsTable();
}

function clearContactSelection() {
  contactSelectedIds.clear();
  _updateDeleteSelectedBtn();
  renderContactsTable();
}

function contactSort(col, dir) {
  contactSortCol = col;
  contactSortDir = dir;
  contactPage = 1;
  fetchContactsPage();
}

function _renderContactColDropdown() {
  const dd = document.getElementById('contacts-col-dropdown');
  if (!dd) return;
  dd.innerHTML = CONTACT_COLS.map(col => `
    <label style="display:flex;align-items:center;gap:8px;padding:5px 14px;cursor:pointer;
                  white-space:nowrap;font-size:13px;color:var(--text)">
      <input type="checkbox" ${contactHiddenCols.has(col.key) ? '' : 'checked'}
             onchange="contactToggleCol('${col.key}',this.checked)" style="cursor:pointer">
      ${col.label}
    </label>
  `).join('');
}

function contactToggleCol(col, visible) {
  if (visible) contactHiddenCols.delete(col);
  else contactHiddenCols.add(col);
  renderContactsTable();
}

function contactsToggleColDropdown() {
  const dd = document.getElementById('contacts-col-dropdown');
  dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
}

document.addEventListener('click', e => {
  const wrap = document.getElementById('contacts-col-toggle-wrap');
  if (wrap && !wrap.contains(e.target)) {
    const dd = document.getElementById('contacts-col-dropdown');
    if (dd) dd.style.display = 'none';
  }
});

function toggleContactSelect(id, checked) {
  if (checked) contactSelectedIds.add(id);
  else contactSelectedIds.delete(id);
  _updateDeleteSelectedBtn();
  _syncSelectAllCheckbox();
  _renderSelectAllMatchingBar();
}

function toggleSelectAllContacts(checked) {
  contactRows.forEach(c =>
    checked ? contactSelectedIds.add(c.id) : contactSelectedIds.delete(c.id));
  _updateDeleteSelectedBtn();
  renderContactsTable();
}

function _syncSelectAllCheckbox() {
  const cb = document.querySelector('#contacts-thead input[type=checkbox]');
  if (cb) {
    cb.checked = contactRows.length > 0 &&
                 contactRows.every(c => contactSelectedIds.has(c.id));
  }
}

function _updateDeleteSelectedBtn() {
  const btn = document.getElementById('contacts-delete-selected-btn');
  if (!btn) return;
  const n = contactSelectedIds.size;
  btn.disabled = n === 0;
  btn.textContent = n > 0 ? `✕ Delete Selected (${n})` : '✕ Delete Selected';
}

async function deleteSelectedContacts() {
  const n = contactSelectedIds.size;
  if (!n) return;
  // Selection can now reach past the visible page, so name the number and say
  // where it came from before doing something irreversible.
  const scope = n > contactRows.length ? ' (including rows on other pages)' : '';
  if (!confirm(`Permanently delete ${n} contact${n > 1 ? 's' : ''}${scope}? This cannot be undone.`)) return;
  const res = await api('/api/contacts/bulk-delete', 'POST', { ids: [...contactSelectedIds] });
  if (res && res.error) { toast(res.error, 'err'); return; }
  toast(`Deleted ${n} contact${n > 1 ? 's' : ''}`);
  loadContacts();
}

// ── Import ────────────────────────────────────────────────────────────────────

function openImportModal() { openModal('modal-import'); }

async function importContacts() {
  const fileInput = document.getElementById('import-file');
  const paste     = document.getElementById('import-paste').value.trim();

  if (fileInput.files.length) {
    const form = new FormData();
    form.append('file', fileInput.files[0]);
    // Raw fetch (FormData sets its own Content-Type with boundary), but the
    // CSRF middleware requires the X-CSRF-Token header on every non-GET.
    const csrf = await _getCsrfToken();
    const res  = await fetch('/api/contacts/import', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRF-Token': csrf },
      body: form,
    });
    if (res.status === 401) { window.location.href = '/login'; return; }
    const data = await res.json();
    const inv = data.invalid_mx ? ` (${data.invalid_mx} invalid MX — see Invalid Emails list)` : '';
    toast(`Imported ${data.inserted} contacts ✓${inv}`);
    closeModal('modal-import');
    loadContacts();
    return;
  }

  if (paste) {
    const rows = paste.split('\n').map(line => ({ email: line.trim() })).filter(r => r.email);
    const data = await api('/api/contacts/import', 'POST', { rows });
    const inv = data.invalid_mx ? ` (${data.invalid_mx} invalid MX — see Invalid Emails list)` : '';
    toast(`Imported ${data.inserted} contacts ✓${inv}`);
    closeModal('modal-import');
    loadContacts();
    return;
  }

  toast('Select a file or paste emails', 'err');
}

// ── Add / Edit ────────────────────────────────────────────────────────────────

function openAddContactModal() {
  contactEditId = null;
  document.getElementById('contact-modal-title').textContent = 'Add Contact';
  document.getElementById('cf-email').value      = '';
  document.getElementById('cf-first').value      = '';
  document.getElementById('cf-last').value       = '';
  document.getElementById('cf-company').value    = '';
  document.getElementById('cf-website').value    = '';
  document.getElementById('cf-address').value    = '';
  document.getElementById('cf-status').value     = 'active';
  document.getElementById('cf-email').disabled   = false;
  openModal('modal-contact');
}

function openEditContactModal(id) {
  const c = allContacts.find(x => x.id === id);
  if (!c) return;
  contactEditId = id;
  document.getElementById('contact-modal-title').textContent = 'Edit Contact';
  document.getElementById('cf-email').value    = c.email    || '';
  document.getElementById('cf-first').value    = c.first_name || '';
  document.getElementById('cf-last').value     = c.last_name  || '';
  document.getElementById('cf-company').value  = c.company  || '';
  document.getElementById('cf-website').value  = c.website  || '';
  document.getElementById('cf-address').value  = c.address  || '';
  document.getElementById('cf-status').value   = c.status   || 'active';
  document.getElementById('cf-email').disabled = false;
  openModal('modal-contact');
}

async function saveContact() {
  const payload = {
    email:      document.getElementById('cf-email').value.trim(),
    first_name: document.getElementById('cf-first').value.trim(),
    last_name:  document.getElementById('cf-last').value.trim(),
    company:    document.getElementById('cf-company').value.trim(),
    website:    document.getElementById('cf-website').value.trim(),
    address:    document.getElementById('cf-address').value.trim(),
    status:     document.getElementById('cf-status').value,
  };

  if (!payload.email && !contactEditId) { toast('Email is required', 'err'); return; }

  let res;
  if (contactEditId) {
    res = await api(`/api/contacts/${contactEditId}`, 'PUT', payload);
  } else {
    res = await api('/api/contacts', 'POST', payload);
  }

  if (!res.ok) { toast(res.error || 'Failed to save contact', 'err'); return; }

  toast(contactEditId ? 'Contact updated ✓' : 'Contact added ✓');
  closeModal('modal-contact');
  loadContacts();
}

// ── Delete ────────────────────────────────────────────────────────────────────

async function deleteContact(id) {
  const c = allContacts.find(x => x.id === id);
  const label = c ? (c.email || c.company || `#${id}`) : `#${id}`;
  if (!confirm(`Delete ${label}?\n\nThis is a soft delete — the record is kept but marked as deleted.`)) return;
  await api(`/api/contacts/${id}`, 'DELETE');
  toast('Contact deleted');
  loadContacts();
}

// ── Enroll ────────────────────────────────────────────────────────────────────

// The enroll list searches server-side for the same reason the main table
// does: it can only ever show a slice, and filtering a slice in the browser
// would hide contacts that genuinely match.
const ENROLL_PAGE_SIZE = 200;
let _enrollSearchTimer = null;

async function openEnrollModal() {
  const filterEl = document.getElementById('enroll-filter');
  if (filterEl) filterEl.value = '';
  await fetchEnrollList('');
  openModal('modal-enroll');
}

async function fetchEnrollList(q) {
  const p = new URLSearchParams({ status: 'active', per_page: ENROLL_PAGE_SIZE });
  if (q) p.set('q', q);
  const data = await api('/api/contacts?' + p.toString());
  renderEnrollList(data && data.rows ? data.rows : [], data ? data.total : 0);
}

function renderEnrollList(contacts, total = 0) {
  const el = document.getElementById('enroll-list');
  if (!contacts.length) {
    el.innerHTML = '<div class="empty-state"><p>No active contacts</p></div>';
    return;
  }
  const truncated = total > contacts.length
    ? `<div class="text-muted" style="padding:8px 14px;font-size:11px;border-bottom:1px solid var(--border)">
         Showing ${contacts.length} of ${total} — search to narrow, or use Enroll All.
       </div>`
    : '';
  el.innerHTML = truncated + contacts.map(c => `
    <label style="display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer">
      <input type="checkbox" value="${c.id}" class="enroll-cb" />
      <div>
        <div style="font-size:13px">${esc(c.first_name)} ${esc(c.last_name)} <span class="text-muted mono" style="font-size:11px">${esc(c.email)}</span></div>
        ${c.company ? `<div class="text-muted" style="font-size:11px">${esc(c.company)}</div>` : ''}
      </div>
    </label>
  `).join('');
}

function filterEnrollList() {
  clearTimeout(_enrollSearchTimer);
  const q = document.getElementById('enroll-filter').value.trim();
  _enrollSearchTimer = setTimeout(() => fetchEnrollList(q), 300);
}

// Some contacts are deliberately skipped: already in another campaign, or a
// duplicate address at a business we already have a better contact for.
// Report that, or enrolling 9 of 12 looks like a silent failure.
function _enrollResultMessage(res) {
  return res.message || `Enrolled ${res.enrolled} contacts`;
}

async function enrollSelected() {
  const ids = [...document.querySelectorAll('.enroll-cb:checked')].map(cb => +cb.value);
  if (!ids.length) { toast('Select at least one contact', 'err'); return; }
  const res = await api(`/api/campaigns/${currentCampaignId}/contacts`, 'POST', { contact_ids: ids });
  toast(_enrollResultMessage(res));
  closeModal('modal-enroll');
  openCampaign(currentCampaignId);
}

async function enrollAll() {
  const res = await api(`/api/campaigns/${currentCampaignId}/contacts`, 'POST', { all: true });
  toast(_enrollResultMessage(res));
  closeModal('modal-enroll');
  openCampaign(currentCampaignId);
}
