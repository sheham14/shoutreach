async function loadContacts() {
  allContacts = await api('/api/contacts');
  const tbody = document.getElementById('contacts-table');
  if (!allContacts.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state"><p>No contacts yet. Import a CSV.</p></td></tr>';
    return;
  }
  tbody.innerHTML = allContacts.slice(0, 300).map(c => `
    <tr>
      <td class="mono" style="font-size:12px">${esc(c.email)}</td>
      <td>${esc(c.first_name)}</td>
      <td>${esc(c.last_name)}</td>
      <td>${esc(c.company)}</td>
      <td>${contactStatusBadge(c.status)}</td>
      <td class="mono text-muted" style="font-size:11px">${c.created_at?.split('T')[0] || c.created_at?.split(' ')[0]}</td>
    </tr>
  `).join('');
}

function openImportModal() { openModal('modal-import'); }

async function importContacts() {
  const fileInput = document.getElementById('import-file');
  const paste     = document.getElementById('import-paste').value.trim();

  if (fileInput.files.length) {
    const form = new FormData();
    form.append('file', fileInput.files[0]);
    const res = await fetch('/api/contacts/import', { method: 'POST', body: form });
    const data = await res.json();
    toast(`Imported ${data.inserted} contacts ✓`);
    closeModal('modal-import');
    loadContacts();
    return;
  }

  if (paste) {
    const rows = paste.split('\n').map(line => ({ email: line.trim() })).filter(r => r.email);
    const data = await api('/api/contacts/import', 'POST', { rows });
    toast(`Imported ${data.inserted} contacts ✓`);
    closeModal('modal-import');
    loadContacts();
    return;
  }

  toast('Select a file or paste emails', 'err');
}

async function openEnrollModal() {
  allContacts = await api('/api/contacts');
  renderEnrollList(allContacts);
  openModal('modal-enroll');
}

function renderEnrollList(contacts) {
  const el = document.getElementById('enroll-list');
  if (!contacts.length) {
    el.innerHTML = '<div class="empty-state"><p>No active contacts</p></div>';
    return;
  }
  el.innerHTML = contacts.filter(c => c.status === 'active').map(c => `
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
