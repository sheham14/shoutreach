const CONTACT_COLS = [
  { key: 'email',      label: 'Email' },
  { key: 'first_name', label: 'First Name' },
  { key: 'last_name',  label: 'Last Name' },
  { key: 'company',    label: 'Company' },
  { key: 'website',    label: 'Website' },
  { key: 'address',    label: 'Address' },
  { key: 'status',     label: 'Status' },
  { key: 'created_at', label: 'Added' },
];

let contactHiddenCols  = new Set();
let contactSortCol     = '';
let contactSortDir     = 'asc';
let contactEditId      = null;
let contactSelectedIds = new Set();

async function loadContacts() {
  allContacts = await api('/api/contacts');
  contactSelectedIds.clear();
  _updateDeleteSelectedBtn();
  _renderContactColDropdown();
  renderContactsTable();
  loadUnsubscribed();
  loadInvalidMx();
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

function renderContactsTable() {
  const showDeleted = document.getElementById('contacts-show-deleted')?.checked;
  const q = (document.getElementById('contacts-search')?.value || '').toLowerCase();

  let data = allContacts.filter(c => {
    if (!showDeleted && c.status === 'deleted') return false;
    if (!q) return true;
    return (c.email || '').toLowerCase().includes(q) ||
           (c.first_name || '').toLowerCase().includes(q) ||
           (c.last_name || '').toLowerCase().includes(q) ||
           (c.company || '').toLowerCase().includes(q) ||
           (c.address || '').toLowerCase().includes(q) ||
           (c.website || '').toLowerCase().includes(q);
  });

  if (contactSortCol) {
    data = [...data].sort((a, b) => {
      const av = (a[contactSortCol] || '').toString().toLowerCase();
      const bv = (b[contactSortCol] || '').toString().toLowerCase();
      const cmp = av.localeCompare(bv);
      return contactSortDir === 'asc' ? cmp : -cmp;
    });
  }

  const visibleCols = CONTACT_COLS.filter(c => !contactHiddenCols.has(c.key));
  const allVisibleIds = data.map(c => c.id);
  const allSelected = allVisibleIds.length > 0 && allVisibleIds.every(id => contactSelectedIds.has(id));

  document.getElementById('contacts-count').textContent = `${data.length} contacts`;

  document.getElementById('contacts-thead').innerHTML = '<tr>' +
    `<th style="width:36px"><input type="checkbox" ${allSelected ? 'checked' : ''}
        onchange="toggleSelectAllContacts(this.checked)" style="cursor:pointer" /></th>` +
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
  if (!data.length) {
    tbody.innerHTML = `<tr><td colspan="${visibleCols.length + 2}" class="empty-state"><p>No contacts found.</p></td></tr>`;
    return;
  }

  tbody.innerHTML = data.map(c => {
    const checked = contactSelectedIds.has(c.id) ? 'checked' : '';
    const cells = visibleCols.map(col => {
      if (col.key === 'status') return `<td>${contactStatusBadge(c.status)}</td>`;
      if (col.key === 'created_at') {
        const d = (c.created_at || '').split('T')[0] || (c.created_at || '').split(' ')[0];
        return `<td class="mono text-muted" style="font-size:11px">${esc(d)}</td>`;
      }
      if (col.key === 'email') return `<td class="mono" style="font-size:12px">${esc(c.email)}</td>`;
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
}

function contactSort(col, dir) {
  contactSortCol = col;
  contactSortDir = dir;
  renderContactsTable();
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
  _rerenderSelectAll();
}

function toggleSelectAllContacts(checked) {
  const showDeleted = document.getElementById('contacts-show-deleted')?.checked;
  const q = (document.getElementById('contacts-search')?.value || '').toLowerCase();
  allContacts
    .filter(c => {
      if (!showDeleted && c.status === 'deleted') return false;
      if (!q) return true;
      return (c.email || '').toLowerCase().includes(q) ||
             (c.first_name || '').toLowerCase().includes(q) ||
             (c.last_name || '').toLowerCase().includes(q) ||
             (c.company || '').toLowerCase().includes(q) ||
             (c.address || '').toLowerCase().includes(q) ||
             (c.website || '').toLowerCase().includes(q);
    })
    .forEach(c => checked ? contactSelectedIds.add(c.id) : contactSelectedIds.delete(c.id));
  _updateDeleteSelectedBtn();
  renderContactsTable();
}

function _rerenderSelectAll() {
  const showDeleted = document.getElementById('contacts-show-deleted')?.checked;
  const q = (document.getElementById('contacts-search')?.value || '').toLowerCase();
  const visible = allContacts.filter(c => {
    if (!showDeleted && c.status === 'deleted') return false;
    if (!q) return true;
    return (c.email || '').toLowerCase().includes(q) ||
           (c.first_name || '').toLowerCase().includes(q) ||
           (c.last_name || '').toLowerCase().includes(q) ||
           (c.company || '').toLowerCase().includes(q) ||
           (c.address || '').toLowerCase().includes(q) ||
           (c.website || '').toLowerCase().includes(q);
  });
  const cb = document.querySelector('#contacts-thead input[type=checkbox]');
  if (cb) cb.checked = visible.length > 0 && visible.every(c => contactSelectedIds.has(c.id));
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
  if (!confirm(`Permanently delete ${n} contact${n > 1 ? 's' : ''}? This cannot be undone.`)) return;
  await api('/api/contacts/bulk-delete', 'POST', { ids: [...contactSelectedIds] });
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
    const res  = await fetch('/api/contacts/import', { method: 'POST', body: form });
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

async function openEnrollModal() {
  allContacts = await api('/api/contacts');
  renderEnrollList(allContacts);
  openModal('modal-enroll');
}

function renderEnrollList(contacts) {
  const el = document.getElementById('enroll-list');
  const active = contacts.filter(c => c.status === 'active');
  if (!active.length) {
    el.innerHTML = '<div class="empty-state"><p>No active contacts</p></div>';
    return;
  }
  el.innerHTML = active.map(c => `
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
  const q = document.getElementById('enroll-filter').value.toLowerCase();
  const filtered = allContacts.filter(c =>
    c.email.toLowerCase().includes(q) ||
    (c.company || '').toLowerCase().includes(q) ||
    (c.first_name || '').toLowerCase().includes(q)
  );
  renderEnrollList(filtered);
}

async function enrollSelected() {
  const ids = [...document.querySelectorAll('.enroll-cb:checked')].map(cb => +cb.value);
  if (!ids.length) { toast('Select at least one contact', 'err'); return; }
  const res = await api(`/api/campaigns/${currentCampaignId}/contacts`, 'POST', { contact_ids: ids });
  toast(`Enrolled ${res.enrolled} contacts ✓`);
  closeModal('modal-enroll');
  openCampaign(currentCampaignId);
}

async function enrollAll() {
  const res = await api(`/api/campaigns/${currentCampaignId}/contacts`, 'POST', { all: true });
  toast(`Enrolled ${res.enrolled} contacts ✓`);
  closeModal('modal-enroll');
  openCampaign(currentCampaignId);
}
