// ── Email page: campaigns, leads, and addresses never to mail again ──────────
//
// The leads table here is every email address you hold -- what the Contacts
// page used to be before Contacts became every business on every channel.

let allContacts = [];     // the rows on screen, for the edit form to look up
let contactEditId = null;
let _emailSources = [];

onTab('email', name => {
  if (name === 'campaigns') loadCampaigns();
  if (name === 'leads') loadEmailLeads();
  if (name === 'suppressed') { loadUnsubscribed(); loadInvalidMx(); }
});

async function loadEmail() {
  const tab = currentTab('email', 'campaigns');
  setTab('email', tab);
  // Counts for the tabs that aren't open, so they say what's behind them.
  if (tab !== 'campaigns') {
    const campaigns = await api('/api/campaigns') || [];
    setTabCount('email', 'campaigns', campaigns.length);
  }
  if (tab !== 'leads') {
    const page = await api('/api/contacts?per_page=1');
    if (page && page.total !== undefined) setTabCount('email', 'leads', page.total);
  }
}

// ── Leads table ──────────────────────────────────────────────────────────────

const _EMAIL_STATUS_TONE = { active: 'green', bounced: 'red', unsubscribed: 'red', deleted: '' };
const _ENROLL_TONE = { queued: 'blue', completed: '', replied: 'green', bounced: 'red',
                       unsubscribed: 'red', paused: 'amber' };

createLeadTable({
  id: 'el',
  url: '/api/contacts',
  idsUrl: '/api/contacts/ids',
  empty: 'No email addresses match. Import a CSV or scrape with Email as the destination.',
  params: () => ({
    q: document.getElementById('el-search')?.value.trim(),
    campaign_id: document.getElementById('el-campaign')?.value,
    status: document.getElementById('el-status')?.value,
    source_job_id: document.getElementById('el-source')?.value,
    include_deleted: document.getElementById('el-deleted')?.checked ? '1' : '',
  }),
  columns: [
    { key: 'email', label: 'Email', sort: true, cls: 'nowrap',
      render: r => `<span class="mono" style="font-size:12px">${esc(r.email || '—')}</span>` },
    { key: 'company', label: 'Business', sort: true,
      render: r => `<span class="biz-name">${esc(r.company || '—')}</span>${
        r.first_name || r.last_name ? `<span class="sub">${esc([r.first_name, r.last_name].filter(Boolean).join(' '))}</span>` : ''}` },
    { key: 'campaign', label: 'Campaign',
      render: r => {
        const e = (r.enrollments || [])[0];
        if (!e) return '<span class="text-muted">Not in a campaign</span>';
        return `${esc(e.campaign)} ${pill(e.status === 'queued' ? `step ${e.current_step}` : e.status, _ENROLL_TONE[e.status] || '')}`;
      } },
    { key: 'status', label: 'Address', sort: true,
      render: r => pill(r.status, _EMAIL_STATUS_TONE[r.status] || '') },
    { key: 'phone', label: 'Phone', sort: true, cls: 'num', render: r => esc(r.phone || '') },
    { key: 'created_at', label: 'Added', sort: true, cls: 'num', render: r => esc(shortDate(r.created_at)) },
  ],
  mobile: r => {
    const e = (r.enrollments || [])[0];
    return mCard(`<span class="mono" style="font-size:12.5px;color:#fff;word-break:break-all">${esc(r.email || '—')}</span>`,
      esc(r.company || ''),
      (e ? `${esc(e.campaign)} ${pill(e.status === 'queued' ? `step ${e.current_step}` : e.status, _ENROLL_TONE[e.status] || '')}`
         : 'Not in a campaign') + (r.status !== 'active' ? ` ${pill(r.status, _EMAIL_STATUS_TONE[r.status] || '')}` : ''));
  },
  onRowClick: r => r.business_id && openLeadPanel(r.business_id, {
    channel: 'email', panelId: 'el-panel', splitId: 'el-split',
    onClose: () => { LT.el.currentId = null; LT.el.render(); },
  }),
  bulk: () => `
    <button class="btn btn-ghost btn-sm" onclick="enrollSelectedEmailLeads()">Enroll in campaign</button>
    <button class="btn btn-danger btn-sm" onclick="deleteSelectedContacts()">Delete</button>`,
  menu: r => [
    { label: 'Edit', run: `openEditContactModal(${r.id})` },
    { label: 'Enroll in a campaign…', run: `enrollEmailLeads([${r.id}])` },
    ...(r.enrollments || []).map(e => ({
      label: `Remove from “${e.campaign}”`, run: `removeEnrollment(${e.id})`,
    })),
    { label: 'Delete address', run: `deleteContact(${r.id})`, danger: true },
  ],
  onLoad: data => {
    allContacts = data.rows;
    setTabCount('email', 'leads', data.total);
  },
});

async function loadEmailLeads() {
  const [campaigns, sources] = await Promise.all([api('/api/campaigns'), api('/api/contacts/sources')]);
  const campSel = document.getElementById('el-campaign');
  const keepCamp = campSel.value;
  campSel.innerHTML = '<option value="">Any campaign</option><option value="none">Not in a campaign</option>' +
    (campaigns || []).map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
  campSel.value = keepCamp;
  _emailSources = sources || [];
  const srcSel = document.getElementById('el-source');
  const keepSrc = srcSel.value;
  srcSel.innerHTML = '<option value="">All lists</option>' +
    _emailSources.map(s => `<option value="${esc(String(s.job_id))}">${esc(s.label)} (${s.count})</option>`).join('');
  srcSel.value = keepSrc;
  LT.el.load();
}

async function enrollEmailLeads(ids) {
  const campaigns = await api('/api/campaigns') || [];
  if (!campaigns.length) { toast('Create an email campaign first', 'err'); return; }
  const cid = await chooseDialog({
    title: `Enroll ${ids.length} address${ids.length === 1 ? '' : 'es'}`,
    body: `<label class="field-label">Campaign</label>
      ${campaignSelectHtml('enroll-pick', campaigns, { allowNew: false })}
      <div class="form-hint">Addresses already in another campaign, or a second address at a business
      already being emailed, are skipped — you'll be told how many.</div>`,
    confirm: 'Enroll',
    collect: () => document.getElementById('enroll-pick').value,
  });
  if (!cid) return;
  const res = await api(`/api/campaigns/${cid}/contacts`, 'POST', { contact_ids: ids });
  if (!res || res.error) { toast((res && res.error) || 'Could not enroll them', 'err'); return; }
  toast(res.message || `Enrolled ${res.enrolled}`);
  LT.el.clear();
  LT.el.load();
  refreshLeadPanel('el-panel');
}

function enrollSelectedEmailLeads() { enrollEmailLeads(LT.el.selectedIds()); }

async function removeEnrollment(enrollId) {
  if (!confirm('Take this address out of the campaign? Anything already sent stays in its history.')) return;
  const res = await api(`/api/enrollments/${enrollId}`, 'DELETE');
  if (!res || res.error) { toast((res && res.error) || 'Could not remove it', 'err'); return; }
  toast('Removed from the campaign');
  LT.el.load();
  refreshLeadPanel('el-panel');
}

async function deleteSelectedContacts() {
  const ids = LT.el.selectedIds();
  if (!ids.length) return;
  const scope = ids.length > LT.el.rows.length ? ' (including some on other pages)' : '';
  if (!confirm(`Permanently delete ${ids.length} address${ids.length > 1 ? 'es' : ''}${scope}?\n\n`
             + `The businesses stay in Contacts. This can't be undone.`)) return;
  const res = await api('/api/contacts/bulk-delete', 'POST', { ids });
  if (res && res.error) { toast(res.error, 'err'); return; }
  toast(`Deleted ${ids.length} address${ids.length > 1 ? 'es' : ''}`);
  LT.el.clear();
  LT.el.load();
}

async function deleteContact(id) {
  const c = allContacts.find(x => x.id === id);
  const label = c ? (c.email || c.company || `#${id}`) : `#${id}`;
  if (!confirm(`Delete ${label}?\n\nIt's marked deleted rather than erased, so it can be found with "Show deleted".`)) return;
  await api(`/api/contacts/${id}`, 'DELETE');
  toast('Address deleted');
  LT.el.load();
}

// ── Unsubscribed ─────────────────────────────────────────────────────────────

let _unsubscribed = [];

async function loadUnsubscribed() {
  _unsubscribed = await api('/api/contacts/unsubscribed') || [];
  const countEl = document.getElementById('unsub-count');
  const tbody   = document.getElementById('unsub-table');
  if (!countEl || !tbody) return;
  countEl.textContent = _unsubscribed.length ? `${_unsubscribed.length} addresses` : '';
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

function _downloadCsv(rows, filename) {
  const csv  = rows.map(r => r.map(v => `"${String(v).replace(/"/g,'""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: filename }).click();
}

function exportUnsubscribed() {
  if (!_unsubscribed.length) { toast('No unsubscribes to export', 'err'); return; }
  const rows = [['Email','First Name','Last Name','Company','Date']];
  _unsubscribed.forEach(c => rows.push([
    c.email || '', c.first_name || '', c.last_name || '',
    c.company || '', (c.created_at || '').substring(0, 10),
  ]));
  _downloadCsv(rows, 'unsubscribed.csv');
}

// ── Invalid MX ────────────────────────────────────────────────────────────────

let _invalidMx = [];

async function loadInvalidMx() {
  _invalidMx = await api('/api/contacts/invalid-mx') || [];
  const countEl = document.getElementById('invalid-mx-count');
  const tbody   = document.getElementById('invalid-mx-table');
  if (!countEl || !tbody) return;
  countEl.textContent = _invalidMx.length ? `${_invalidMx.length} addresses` : '';
  if (!_invalidMx.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state"><p>No invalid email domains found yet</p></td></tr>';
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
  if (!_invalidMx.length) { toast('No invalid addresses to export', 'err'); return; }
  const rows = [['Email','Company','Website','Address','Date']];
  _invalidMx.forEach(c => rows.push([
    c.email || '', c.company || '', c.website || '',
    c.address || '', (c.created_at || '').substring(0, 10),
  ]));
  _downloadCsv(rows, 'invalid-mx-emails.csv');
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
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRF-Token': csrf }, body: form,
    });
    if (res.status === 401) { window.location.href = '/login'; return; }
    const first = await res.json();
    // A CSV held-back conflict is re-submitted as JSON rows, not a re-upload
    // -- the flagged rows are already sitting in the response.
    const final = await confirmChannelConflicts(first, () =>
      api('/api/contacts/import', 'POST', {
        rows: first.conflicts.map(c => c.row), confirm_conflicts: true,
      })
    );
    reportImport(mergeImportResults(first, final));
    return;
  }

  if (paste) {
    const rows = paste.split('\n').map(line => ({ email: line.trim() })).filter(r => r.email);
    const first = await api('/api/contacts/import', 'POST', { rows });
    const final = await confirmChannelConflicts(first, () =>
      api('/api/contacts/import', 'POST', {
        rows: first.conflicts.map(c => c.row), confirm_conflicts: true,
      })
    );
    reportImport(mergeImportResults(first, final));
    return;
  }

  toast('Select a file or paste emails', 'err');
}

// confirmChannelConflicts returns the SAME object back when the operator
// declines, or a fresh response from the confirmed resend when they accept --
// that reference difference is how much this needs to know to combine the
// two calls' counts into one honest total instead of reporting only the last.
function mergeImportResults(first, final) {
  if (final === first) return first;
  return {
    inserted:   (first.inserted || 0) + (final.inserted || 0),
    invalid_mx: (first.invalid_mx || 0) + (final.invalid_mx || 0),
    conflicts:  [],
    // Only the first call runs the cross-owner check; the resend is the
    // operator confirming rows they have already been told about.
    overlaps:   first.overlaps || [],
  };
}

function reportImport(data) {
  if (!data || data.error) { toast((data && data.error) || 'Import failed', 'err'); return; }
  const inv = data.invalid_mx ? ` (${data.invalid_mx} with an invalid domain — see Unsubscribed & bad addresses)` : '';
  const held = data.conflicts && data.conflicts.length
    ? ` — ${data.conflicts.length} skipped (already on another channel)` : '';
  toast(`Imported ${data.inserted} ✓${inv}${held}`);
  closeModal('modal-import');
  if (document.getElementById('section-contacts').classList.contains('active')) loadContacts();
  else loadEmail();
  notifyCrossOwnerOverlap(data);
}

// ── Add / edit an address ────────────────────────────────────────────────────

function openAddContactModal() {
  contactEditId = null;
  document.getElementById('contact-modal-title').textContent = 'Add an email address';
  ['cf-email', 'cf-first', 'cf-last', 'cf-company', 'cf-website', 'cf-address']
    .forEach(id => { document.getElementById(id).value = ''; });
  document.getElementById('cf-status').value = 'active';
  openModal('modal-contact');
}

function openEditContactModal(id) {
  const c = allContacts.find(x => x.id === id);
  if (!c) return;
  contactEditId = id;
  document.getElementById('contact-modal-title').textContent = 'Edit email address';
  document.getElementById('cf-email').value    = c.email    || '';
  document.getElementById('cf-first').value    = c.first_name || '';
  document.getElementById('cf-last').value     = c.last_name  || '';
  document.getElementById('cf-company').value  = c.company  || '';
  document.getElementById('cf-website').value  = c.website  || '';
  document.getElementById('cf-address').value  = c.address  || '';
  document.getElementById('cf-status').value   = c.status   || 'active';
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
  if (!payload.email && !contactEditId) { toast('An email address is required', 'err'); return; }

  const res = contactEditId
    ? await api(`/api/contacts/${contactEditId}`, 'PUT', payload)
    : await api('/api/contacts', 'POST', payload);
  if (!res.ok) { toast(res.error || 'Could not save it', 'err'); return; }

  toast(contactEditId ? 'Saved ✓' : 'Address added ✓');
  closeModal('modal-contact');
  LT.el.load();
}

// ── Enroll (from inside a campaign) ──────────────────────────────────────────

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
    el.innerHTML = '<div class="empty-state"><p>No active addresses</p></div>';
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
  if (!ids.length) { toast('Select at least one address', 'err'); return; }
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
