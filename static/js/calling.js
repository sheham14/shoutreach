// ── Cold calling ─────────────────────────────────────────────────────────────
//
// Built as a working queue rather than a CRM screen. The To do tab is what to
// do next -- callbacks due, then leads never called -- because deciding who
// to ring is friction at exactly the moment momentum matters, and "Save & next
// lead" keeps you in the loop instead of returning to a table between calls.
// The Leads tab is the other half: everyone on Calling, as a table to manage.

let _callBucket   = 'today';
let _callLeads    = [];
let _callOutcomes = [];
let _callLead     = null;      // the lead currently on screen
let _callScript   = null;
let _attemptLimit = 6;
let _callCampaigns = [];
let _callCampaignId = '';   // '' = every lead, ignoring campaigns

onTab('calling', name => {
  if (name === 'todo') loadCallQueue();
  if (name === 'leads') loadCallLeads();
  if (name === 'campaigns') loadCallCampaigns();
  if (name === 'script') { _renderScriptEditor(); _renderOutcomeEditor(); }
});

async function loadCalling() {
  await Promise.all([loadCallScript(), loadCallSources(), loadCallCampaigns()]);
  setTab('calling', currentTab('calling', 'todo'));
  if (currentTab('calling', 'todo') !== 'leads') {
    const page = await api('/api/calls/leads?per_page=1');
    if (page && page.total !== undefined) setTabCount('calling', 'leads', page.total);
  }
}

// From the Dashboard: straight to a bucket, or to one campaign's queue.
function openCallingTodo(bucket) {
  _callBucket = bucket;
  showSection('calling');
  setTimeout(() => { setTab('calling', 'todo', { load: false }); setCallBucket(bucket); }, 0);
}

function openCallingCampaign(id) {
  _callCampaignId = String(id);
  showSection('calling');
  setTimeout(() => {
    setTab('calling', 'leads', { load: false });
    const sel = document.getElementById('cl-campaign');
    if (sel) sel.value = String(id);
    loadCallLeads();
  }, 0);
}

// ── Campaigns ────────────────────────────────────────────────────────────────
//
// A batch you decided to work ("10 clinics, St John's"), as distinct from a
// lead list, which groups by whenever the scrape happened to run.

async function loadCallCampaigns() {
  _callCampaigns = await api('/api/call-campaigns') || [];
  setTabCount('calling', 'campaigns', _callCampaigns.length);
  const sel = document.getElementById('cq-campaign');
  if (sel) {
    sel.innerHTML = '<option value="">All campaigns</option>' +
      _callCampaigns.map(c =>
        `<option value="${c.id}">${esc(c.name)} — ${c.remaining} left of ${c.total}</option>`
      ).join('');
    sel.value = _callCampaignId;
  }
  const leadSel = document.getElementById('cl-campaign');
  if (leadSel) {
    const keep = leadSel.value;
    leadSel.innerHTML = '<option value="">Any campaign</option><option value="none">Not in a campaign</option>' +
      _callCampaigns.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
    leadSel.value = keep;
  }
  _renderCampaignCards();
}

function _renderCampaignCards() {
  const wrap = document.getElementById('cq-campaign-cards');
  if (!wrap) return;
  if (!_callCampaigns.length) {
    wrap.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><p>No call campaigns yet.
      Create one, then add leads to it — from here, from the Leads tab, or from Contacts.</p></div>`;
    return;
  }
  wrap.innerHTML = _callCampaigns.map(c => {
    const pct = c.total ? Math.round((c.closed / c.total) * 100) : 0;
    return `<div class="card" style="padding:16px">
      <div class="flex items-center" style="gap:8px;margin-bottom:8px">
        <div class="card-title">${esc(c.name)}</div>
        <div class="row-menu ml-auto">
          <button class="btn btn-ghost btn-sm" onclick="toggleRowMenu(this)">⋯</button>
          <div class="row-menu-list">
            <button onclick="renameCallCampaign(${c.id})">Rename</button>
            <button class="danger" onclick="deleteCallCampaign(${c.id})">Delete campaign</button>
          </div>
        </div>
      </div>
      <div class="bar" style="margin-bottom:10px" title="${pct}% closed out"><i style="width:${pct}%"></i></div>
      <div class="text-muted" style="font-size:12px;line-height:1.8">
        <div><strong style="color:var(--text)">${c.remaining}</strong> left of ${c.total}</div>
        <div>${c.due} due now · ${c.uncalled} never called</div>
        <div><span style="color:var(--green)">${c.booked} booked</span> · ${c.not_interested} not interested</div>
      </div>
      <div class="flex gap-2" style="margin-top:12px;flex-wrap:wrap">
        <button class="btn btn-primary btn-sm" onclick="workCallCampaign(${c.id})">☎ Work it</button>
        <button class="btn btn-ghost btn-sm" onclick="openCallingCampaign(${c.id})">Leads</button>
        <button class="btn btn-ghost btn-sm" onclick="openAddLeadsModal(${c.id})">+ Add leads</button>
      </div>
    </div>`;
  }).join('');
}

function workCallCampaign(id) {
  _callCampaignId = String(id);
  setTab('calling', 'todo', { load: false });
  setCallCampaign(id);
}

function setCallCampaign(id) {
  _callCampaignId = id ? String(id) : '';
  const sel = document.getElementById('cq-campaign');
  if (sel) sel.value = _callCampaignId;
  loadCallQueue();
}

async function openNewCallCampaign() {
  const name = await chooseDialog({
    title: 'New call campaign',
    body: `<label class="field-label">Name</label>
      <input id="ncc-name" class="soft-input" placeholder="No-website clinics, Sharjah" />
      <div class="form-hint">Then add leads to it from the Leads tab, from Contacts, or with + Add leads.</div>`,
    confirm: 'Create',
    collect: () => {
      const v = document.getElementById('ncc-name').value.trim();
      if (!v) { toast('Give it a name', 'err'); return null; }
      return v;
    },
  });
  if (!name) return;
  const res = await api('/api/call-campaigns', 'POST', { name });
  if (!res || res.error) { toast((res && res.error) || 'Could not create it', 'err'); return; }
  toast('Campaign created');
  await loadCallCampaigns();
}

async function renameCallCampaign(id) {
  const c = _callCampaigns.find(x => x.id === id);
  const name = await chooseDialog({
    title: 'Rename campaign',
    body: `<input id="rcc-name" class="soft-input" value="${esc(c ? c.name : '')}" />`,
    confirm: 'Save',
    collect: () => document.getElementById('rcc-name').value.trim() || null,
  });
  if (!name) return;
  await api(`/api/call-campaigns/${id}`, 'PATCH', { name });
  loadCallCampaigns();
}

async function deleteCallCampaign(id) {
  const c = _callCampaigns.find(x => x.id === id);
  const ok = await chooseDialog({
    title: `Delete "${c ? c.name : 'this campaign'}"?`,
    body: `<p class="text-small" style="line-height:1.6">The ${c ? c.total : 0} leads and their call history are kept
      on Calling — only the grouping goes.</p>`,
    confirm: 'Delete campaign', danger: true,
  });
  if (!ok) return;
  const res = await api(`/api/call-campaigns/${id}`, 'DELETE');
  if (!res || res.error) { toast((res && res.error) || 'Could not delete', 'err'); return; }
  toast('Campaign deleted — leads kept');
  if (_callCampaignId === String(id)) _callCampaignId = '';
  await loadCallCampaigns();
}

// ── Leads table ──────────────────────────────────────────────────────────────

createLeadTable({
  id: 'cl',
  url: '/api/calls/leads',
  empty: 'Nobody on Calling matches. Add leads with + Add leads, from Contacts, or scrape with Calling as the destination.',
  params: () => ({
    q: document.getElementById('cl-search')?.value.trim(),
    outcome: document.getElementById('cl-outcome')?.value,
    call_campaign_id: document.getElementById('cl-campaign')?.value,
    source_job_id: document.getElementById('cl-source')?.value,
  }),
  columns: [
    { key: 'company', label: 'Business', sort: true,
      render: r => `<span class="biz-name">${esc(r.company || 'Unnamed business')}</span>${contactSignalPill(r)}${
        r.city || r.address ? `<span class="sub">${esc(r.city || r.address)}</span>` : ''}` },
    { key: 'phone', label: 'Phone', sort: true, cls: 'num', render: r => esc(r.phone || '') },
    { key: 'campaigns', label: 'Campaigns',
      render: r => (r.campaigns || []).length
        ? `<span class="pills">${r.campaigns.map(c => pill(c.name)).join('')}</span>`
        : '<span class="text-muted">—</span>' },
    { key: 'call_status', label: 'Last outcome', sort: true,
      render: r => (r.do_not_contact ? pill('Do not contact', 'red') + ' ' : '') + callStatusBadge(r.call_status) },
    { key: 'call_attempts', label: 'Attempts', sort: true, cls: 'num', render: r => r.call_attempts || 0 },
    { key: 'next_call_at', label: 'Next call', sort: true, cls: 'num',
      render: r => esc(r.next_call_at ? r.next_call_at.substring(0, 16) : '') },
    { key: 'last_called_at', label: 'Last called', sort: true, cls: 'num',
      render: r => esc(shortDate(r.last_called_at)) },
  ],
  mobile: r => mCard(`<span class="biz-name">${esc(r.company || 'Unnamed business')}</span>`,
    `${r.do_not_contact ? pill('Do not contact', 'red') + ' ' : ''}${callStatusBadge(r.call_status)}${
      (r.campaigns || []).length ? ` ${esc(r.campaigns.map(c => c.name).join(', '))}` : ''}`,
    [r.phone && `<span class="mono">${esc(r.phone)}</span>`,
     r.next_call_at && `next call ${esc(r.next_call_at.substring(0, 16))}`].filter(Boolean).join(' · ')),
  onRowClick: r => openLeadPanel(r.id, {
    channel: 'calling', panelId: 'cl-panel', splitId: 'cl-split',
    onClose: () => { LT.cl.currentId = null; LT.cl.render(); },
  }),
  bulk: () => `
    <button class="btn btn-ghost btn-sm" onclick="callLeadsToCampaign(LT.cl.selectedIds())">Add to campaign</button>
    ${document.getElementById('cl-campaign')?.value && document.getElementById('cl-campaign')?.value !== 'none'
      ? `<button class="btn btn-ghost btn-sm" onclick="callLeadsOutOfCampaign(LT.cl.selectedIds())">Remove from this campaign</button>` : ''}
    <button class="btn btn-ghost btn-sm" onclick="contactsToWhatsApp(LT.cl.selectedIds(), {onDone: () => LT.cl.load()})">+ WhatsApp</button>
    <button class="btn btn-danger btn-sm" onclick="removeFromCalling(LT.cl.selectedIds())">Take off Calling</button>`,
  menu: r => [
    { label: 'Open in the dialler', run: `openInDialler(${r.id})` },
    { label: 'Add to a campaign…', run: `callLeadsToCampaign([${r.id}])` },
    ...(r.campaigns || []).map(c => ({ label: `Remove from “${c.name}”`, run: `callLeadsOutOfCampaign([${r.id}], ${c.id})` })),
    r.call_status && { label: 'Reopen (put back in the queue)', run: `reopenCallLead(${r.id})` },
    { label: 'Take off Calling', run: `removeFromCalling([${r.id}])`, danger: true },
  ],
  onLoad: data => setTabCount('calling', 'leads', data.total),
});

async function loadCallLeads() {
  const outcomeSel = document.getElementById('cl-outcome');
  if (outcomeSel && outcomeSel.options.length <= 1) {
    const outcomes = await api('/api/call-outcomes') || [];
    outcomeSel.innerHTML = '<option value="">Any outcome</option><option value="none">Never called</option>' +
      outcomes.filter(o => !o.archived).map(o => `<option value="${esc(o.key)}">${esc(o.label)}</option>`).join('');
  }
  const src = document.getElementById('cl-source');
  if (src && src.options.length <= 1) {
    const sources = await api('/api/contacts/sources') || [];
    src.innerHTML = '<option value="">All lists</option>' +
      sources.map(s => `<option value="${esc(String(s.job_id))}">${esc(s.label)} (${s.count})</option>`).join('');
  }
  await loadCallCampaigns();
  LT.cl.load();
  refreshLeadPanel('cl-panel');
}

function openInDialler(businessId) {
  setTab('calling', 'todo', { load: false });
  _callBucket = 'all';
  ['today', 'new', 'upcoming', 'all', 'worked'].forEach(b =>
    document.getElementById(`cq-tab-${b}`)?.classList.toggle('active', b === 'all'));
  loadCallQueue().then(() => openCallLead(businessId));
}

async function callLeadsToCampaign(ids) {
  if (!ids.length) return;
  const ok = await chooseDialog({
    title: `Add ${ids.length} to a call campaign`,
    body: `<label class="field-label">Campaign</label>${campaignSelectHtml('clc-pick', _callCampaigns)}`,
    confirm: 'Add',
    collect: () => document.getElementById('clc-pick').value || null,
  });
  if (!ok) return;
  const cid = await resolveCampaignSelect('clc-pick', '/api/call-campaigns');
  if (!cid) return;
  const res = await api('/api/calls/add', 'POST', { business_ids: ids, call_campaign_id: cid, confirm_conflicts: true });
  if (!res || res.error) { toast((res && res.error) || 'Could not add them', 'err'); return; }
  toast(`Added ${res.in_campaign} to the campaign` + (ids.length - res.in_campaign > 0
    ? ` — ${ids.length - res.in_campaign} were already in it or couldn't be added` : ''));
  LT.cl.clear();
  loadCallLeads();
}

async function callLeadsOutOfCampaign(ids, campaignId = null) {
  const cid = campaignId || document.getElementById('cl-campaign').value;
  if (!cid || cid === 'none' || !ids.length) return;
  const res = await api(`/api/call-campaigns/${cid}/members`, 'DELETE', { contact_ids: ids });
  if (!res || res.error) { toast((res && res.error) || 'Could not remove them', 'err'); return; }
  toast(`Removed ${res.removed} from the campaign — still on Calling`);
  LT.cl.clear();
  loadCallLeads();
}

async function removeFromCalling(ids) {
  if (!ids.length) return;
  const ok = await chooseDialog({
    title: `Take ${ids.length} off Calling?`,
    body: `<p class="text-small" style="line-height:1.6">They leave every call queue and campaign. Their call history
      is kept, and they stay in Contacts (under Unassigned if they're on no other channel), so you can add them back any time.</p>`,
    confirm: 'Take off Calling', danger: true,
  });
  if (!ok) return;
  const res = await api('/api/calls/remove', 'POST', { business_ids: ids });
  if (!res || res.error) { toast((res && res.error) || 'Could not remove them', 'err'); return; }
  toast(`Took ${res.removed} off Calling`);
  LT.cl.clear();
  loadCallLeads();
}

// ── Adding leads ─────────────────────────────────────────────────────────────
//
// Three routes in: pick from leads you already have, type a few, or bring a
// CSV. A campaign is optional -- a lead can just be on Calling.

let _aclTab = 'existing';
let _aclSelected = new Set();
let _aclRows = [];
let _aclTimer = null;

async function openAddLeadsModal(campaignId = '') {
  if (!_callCampaigns.length) await loadCallCampaigns();
  document.getElementById('acl-campaign-wrap').innerHTML =
    campaignSelectHtml('acl-campaign', _callCampaigns,
                       { allowNone: true, noneLabel: 'No campaign — just add them to Calling',
                         selected: campaignId || '' });
  _aclSelected.clear();
  document.getElementById('acl-search').value = '';
  document.getElementById('acl-manual').value = '';
  setAddLeadsTab('existing');
  openModal('modal-add-call-leads');
  aclSearch();
}

function setAddLeadsTab(tab) {
  _aclTab = tab;
  ['existing', 'manual', 'csv'].forEach(t => {
    const btn = document.getElementById(`acl-tab-${t}`);
    if (btn) {
      btn.classList.toggle('btn-primary', t === tab);
      btn.classList.toggle('btn-ghost', t !== tab);
    }
    const pane = document.getElementById(`acl-pane-${t}`);
    if (pane) pane.style.display = t === tab ? 'block' : 'none';
  });
}

function aclSearch() {
  clearTimeout(_aclTimer);
  _aclTimer = setTimeout(_aclFetch, 250);
}

async function _aclFetch() {
  const p = new URLSearchParams({ per_page: 100 });
  const q = document.getElementById('acl-search').value.trim();
  if (q) p.set('q', q);
  const status = document.getElementById('acl-status').value;
  if (status) p.set('status', status);
  if (document.getElementById('acl-uncalled').checked) p.set('not_on', 'calling');

  // Businesses, not email leads: a clinic the scraper found with no email at
  // all is exactly the kind of lead this tab exists to surface.
  const data = await api('/api/businesses/search?' + p.toString());
  _aclRows = (data && data.rows) || [];
  _aclRenderTable(data ? data.total : 0);
}

function _aclRenderTable(total) {
  const tbody = document.getElementById('acl-table');
  document.getElementById('acl-count').textContent =
    `${_aclSelected.size} selected · showing ${_aclRows.length} of ${total}`;
  if (!_aclRows.length) {
    tbody.innerHTML = '<tr><td colspan="4"><div class="empty-state"><p>No leads match</p></div></td></tr>';
    return;
  }
  tbody.innerHTML = _aclRows.map(c => `
    <tr style="cursor:pointer" onclick="aclToggle(${c.id})">
      <td><input type="checkbox" ${_aclSelected.has(c.id) ? 'checked' : ''}
                 onclick="event.stopPropagation();aclToggle(${c.id})" style="cursor:pointer" /></td>
      <td>${esc(c.company || c.email || '—')}${contactSignalPill(c)}</td>
      <td class="mono" style="font-size:12px">${c.phone ? esc(c.phone) : '<span class="text-muted">no phone</span>'}</td>
      <td>${callStatusBadge(c.call_status)}</td>
    </tr>`).join('');
}

function aclToggle(id) {
  if (_aclSelected.has(id)) _aclSelected.delete(id); else _aclSelected.add(id);
  _aclRenderTable(_aclRows.length);
}

function aclSelectAllShown() {
  _aclRows.forEach(c => _aclSelected.add(c.id));
  _aclRenderTable(_aclRows.length);
}

function aclClearSelection() {
  _aclSelected.clear();
  _aclRenderTable(_aclRows.length);
}

// Puts businesses on Calling (and into the picked campaign), holding for
// confirmation any already active on another channel.
async function _addToCalling(businessIds, campaignId) {
  if (!businessIds.length) return { added: 0 };
  const body = { business_ids: businessIds, call_campaign_id: campaignId || null };
  const first = await api('/api/calls/add', 'POST', body);
  if (!first || first.error) return first;
  const final = await confirmChannelConflicts(first, () => api('/api/calls/add', 'POST', {
    ...body, business_ids: first.conflicts.map(c => c.business_id), confirm_conflicts: true,
  }));
  return _sumCounts(first, final);
}

async function submitAddLeads() {
  const campaignId = await resolveCampaignSelect('acl-campaign', '/api/call-campaigns');
  if (campaignId === null) return;
  let res, imported = null;

  if (_aclTab === 'existing') {
    if (!_aclSelected.size) { toast('Select at least one lead', 'err'); return; }
    res = await _addToCalling([..._aclSelected], campaignId);

  } else if (_aclTab === 'manual') {
    // "Name, phone, website" per line. Deliberately forgiving about the tail:
    // the name is the only part you always have when typing from a list.
    const rows = document.getElementById('acl-manual').value
      .split('\n').map(l => l.trim()).filter(Boolean)
      .map(line => {
        const [company, phone, website] = line.split(',').map(x => (x || '').trim());
        return { company, phone: phone || '', website: website || '',
                 status: website ? 'no_email' : 'no_website' };
      })
      .filter(r => r.company);
    if (!rows.length) { toast('Nothing to add — one business per line', 'err'); return; }
    imported = await _importForCalling(rows);
    if (!imported || imported.error) { toast((imported && imported.error) || 'Could not add those', 'err'); return; }
    res = await _addToCalling(imported.business_ids || [], campaignId);

  } else {
    const file = document.getElementById('acl-csv').files[0];
    if (!file) { toast('Choose a CSV first', 'err'); return; }
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch('/api/contacts/import', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRF-Token': await _getCsrfToken() }, body: fd,
    });
    imported = await r.json().catch(() => ({}));
    if (!r.ok || imported.error) { toast(imported.error || 'CSV import failed', 'err'); return; }
    imported = await _resolveImportConflicts(imported);
    res = await _addToCalling(imported.business_ids || [], campaignId);
  }

  if (!res || res.error) { toast((res && res.error) || 'Could not add them', 'err'); return; }
  toast(describeAdd(res, 'Added to Calling:'));
  if (imported) notifyCrossOwnerOverlap(imported);
  closeModal('modal-add-call-leads');
  await loadCallCampaigns();
  const tab = currentTab('calling', 'todo');
  if (tab === 'leads') LT.cl.load(); else if (tab === 'todo') loadCallQueue();
}

// A CSV or pasted line for calling can still carry an email column, which
// goes through the same email-channel check as the Contacts importer. Held
// rows are confirmed the same way, then folded back into one business id
// list so the add step sees every business that ended up imported.
async function _importForCalling(rows) {
  const first = await api('/api/contacts/import', 'POST', { rows });
  if (!first || first.error) return first;
  return _resolveImportConflicts(first);
}

async function _resolveImportConflicts(first) {
  const final = await confirmChannelConflicts(first, () =>
    api('/api/contacts/import', 'POST', {
      rows: first.conflicts.map(c => c.row), confirm_conflicts: true,
    })
  );
  if (final === first) return first;
  return {
    inserted: (first.inserted || 0) + (final.inserted || 0),
    business_ids: [...(first.business_ids || []), ...(final.business_ids || [])],
    overlaps: first.overlaps || [],
  };
}

async function loadCallSources() {
  const sel = document.getElementById('cq-source-filter');
  if (!sel) return;
  const sources = await api('/api/contacts/sources') || [];
  sel.innerHTML = '<option value="">All lists</option>' + sources
    .map(s => `<option value="${esc(String(s.job_id))}">${esc(s.label)} (${s.count})</option>`)
    .join('');
}

function setCallBucket(bucket) {
  _callBucket = bucket;
  ['today', 'new', 'upcoming', 'all', 'worked'].forEach(b => {
    document.getElementById(`cq-tab-${b}`)?.classList.toggle('active', b === bucket);
  });
  loadCallQueue();
}

async function loadCallQueue() {
  const p = new URLSearchParams({ bucket: _callBucket });
  const src = document.getElementById('cq-source-filter')?.value;
  if (src) p.set('source_job_id', src);
  if (document.getElementById('cq-no-website')?.checked) p.set('no_website', '1');
  if (_callCampaignId) p.set('call_campaign_id', _callCampaignId);

  const data = await api('/api/calls/queue?' + p.toString());
  if (!data || !data.leads) { toast('Could not load the call queue', 'err'); return; }

  _callLeads    = data.leads;
  _callOutcomes = data.outcomes || [];
  _attemptLimit = data.attempt_limit || 6;

  ['today', 'new', 'upcoming', 'worked'].forEach(b => {
    const el = document.getElementById(`cq-count-${b}`);
    if (el) el.textContent = (data.counts || {})[b] ?? 0;
  });
  const due = (data.counts || {}).today || 0;
  setTabCount('calling', 'todo', due, true);
  const nav = document.getElementById('nav-count-calling');
  if (nav && !_callCampaignId) nav.textContent = due ? due : '';

  _renderSummary(data.summary);
  _renderCallTable();

  // Drop straight into the first lead so the common case is zero clicks --
  // except when reviewing closed-out leads, where there is nothing to dial and
  // opening one would just put a live outcome form in front of you.
  if (_callLeads.length && _callBucket !== 'worked') openCallLead(_callLeads[0].id);
  else {
    _callLead = null;
    document.getElementById('cq-lead-card').style.display = 'none';
    _renderScriptFor(null);
    closeSheet('cq-work');
  }
}

// Mirrors the main dashboard's shape so both read the same way. Scoped to the
// selected campaign when there is one, so the numbers match the list below.
function _renderSummary(s) {
  const el = document.getElementById('cq-summary');
  if (!el || !s) return;
  const tiles = [
    ['Leads',          s.leads,          ''],
    ['Calls made',     s.calls_made,     ''],
    ['Today',          s.calls_today,    'blue'],
    ['Due now',        s.due,            s.due > 0 ? 'amber' : ''],
    ['Booked',         s.booked,         'green'],
    ['Not interested', s.not_interested, s.not_interested > 0 ? 'red' : ''],
  ];
  el.innerHTML = tiles.map(([label, value, tone]) => `
    <div class="stat-card" style="padding:12px">
      <div class="stat-label">${label}</div>
      <div class="stat-value ${tone}" style="font-size:22px">${value ?? 0}</div>
    </div>`).join('');
}

function _renderCallTable() {
  const titles = { today: 'Due now', new: 'Never called', upcoming: 'Scheduled',
                   all: 'All callable', worked: 'Closed out' };
  document.getElementById('cq-list-title').textContent = titles[_callBucket] || 'Queue';
  document.getElementById('cq-list-count').textContent =
    `${_callLeads.length} lead${_callLeads.length === 1 ? '' : 's'}`;

  const tbody = document.getElementById('cq-table');
  if (!_callLeads.length) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="empty-state"><p>Nothing here${
      _callBucket === 'today' ? " — no callbacks due. Try “Never called”." : ''}</p></div></td></tr>`;
    return;
  }

  tbody.innerHTML = _callLeads.map(l => {
    const due = l.next_call_at ? esc(l.next_call_at.substring(0, 16)) : '—';
    const over = l.call_attempts >= _attemptLimit;
    return `<tr style="cursor:pointer${_callLead && _callLead.id === l.id ? ';background:rgba(96,165,250,.08)' : ''}"
                onclick="openCallLead(${l.id}, true)">
      <td>${esc(l.company || l.email || '—')}${contactSignalPill(l)}</td>
      <td class="mono" style="font-size:12px">${esc(l.phone || '—')}</td>
      <td class="mono" style="font-size:12px${over ? ';color:var(--amber)' : ''}"
          ${over ? `title="Past ${_attemptLimit} attempts — probably time to let it go"` : ''}>${l.call_attempts}</td>
      <td class="mono text-muted" style="font-size:11px">${due}</td>
      <td>${_callBucket === 'worked'
            ? `${callStatusBadge(l.call_status)} <button class="btn btn-ghost btn-sm"
                 onclick="event.stopPropagation();reopenCallLead(${l.id})"
                 title="Put this lead back in the queue">↩ Reopen</button>`
            : `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openCallLead(${l.id}, true)">Open</button>`}</td>
      <td class="m-card">${mCard(`<span class="biz-name">${esc(l.company || l.email || '—')}</span>${contactSignalPill(l)}`,
        [l.phone && `<span class="mono">${esc(l.phone)}</span>`,
         `${l.call_attempts} attempt${l.call_attempts === 1 ? '' : 's'}`,
         l.next_call_at && `due ${due}`].filter(Boolean).join(' · '),
        _callBucket === 'worked' ? `${callStatusBadge(l.call_status)} <button class="btn btn-ghost btn-sm"
          onclick="event.stopPropagation();reopenCallLead(${l.id})">↩ Reopen</button>` : '')}</td>
    </tr>`;
  }).join('');
}

// `show` opens the lead full screen on a phone: a tap on it, as opposed to the
// queue dropping into its first lead when it loads.
async function openCallLead(id, show = false) {
  const data = await api(`/api/calls/contact/${id}`);
  if (!data || !data.contact) { toast('Could not load that lead', 'err'); return; }
  if (show) openSheet('cq-work');
  document.getElementById('cq-work').scrollTop = 0;

  _callLead = data.contact;
  const c = data.contact;

  document.getElementById('cq-lead-card').style.display = 'block';
  document.getElementById('cq-lead-name').textContent = c.company || c.email || `Contact ${c.id}`;
  document.getElementById('cq-lead-pills').innerHTML = contactSignalPill(c);

  const bits = [];
  if (c.phone)        bits.push(`<a href="tel:${esc(c.phone)}" style="color:var(--blue)">${esc(c.phone)}</a>`);
  if (c.website)      bits.push(`<a href="${esc(c.website)}" target="_blank" rel="noopener" style="color:var(--blue)">${esc(c.website)}</a>`);
  if (c.email)        bits.push(esc(c.email));
  if (c.address)      bits.push(esc(c.address));
  if (c.rating != null) bits.push(`${esc(c.rating)}★ (${esc(c.review_count ?? 0)})`);
  if (c.call_attempts) bits.push(`${c.call_attempts} previous attempt${c.call_attempts === 1 ? '' : 's'}`);
  document.getElementById('cq-lead-meta').innerHTML = bits.join(' &nbsp;·&nbsp; ');

  // Prior contact is shown, never used to hide the lead: an emailed lead with
  // no reply is still worth ringing, one that already answered is not.
  const warn = document.getElementById('cq-lead-warning');
  const t = data.touch || {};
  const notes = [];
  if (t.emails_sent) notes.push(`Emailed ${t.emails_sent}×${t.last_email_at ? ` (last ${esc(t.last_email_at.substring(0,10))})` : ''}`);
  if (t.closed)      notes.push('already replied or opted out by email — check before dialling');
  if (c.call_attempts >= _attemptLimit) notes.push(`${c.call_attempts} attempts already`);
  if (notes.length) { warn.style.display = 'block'; warn.innerHTML = '⚠ ' + notes.join(' · '); }
  else warn.style.display = 'none';

  document.getElementById('cq-notes').value = '';
  document.getElementById('cq-next-at').value = '';
  document.getElementById('cq-next-wrap').style.display = 'none';
  _renderOutcomeButtons(null);
  _renderCallHistory(data.history || []);
  _renderScriptFor(c);
  _renderCallTable();

  const ics = document.getElementById('cq-ics-link');
  if (c.next_call_at && c.call_status === 'booked') {
    ics.style.display = 'inline-flex';
    ics.href = `/api/calls/${c.id}/ics`;
  } else ics.style.display = 'none';

  // Not on a phone: it would pop the keyboard over the lead you just opened.
  if (!isNarrow()) document.getElementById('cq-notes').focus();
}

let _chosenOutcome = null;

function _renderOutcomeButtons(selected) {
  _chosenOutcome = selected;
  const wrap = document.getElementById('cq-outcomes');
  wrap.innerHTML = _callOutcomes.map(o => {
    const on = o.key === selected;
    // Colour comes from the outcome's own tone so custom ones look deliberate
    // rather than defaulting to the same blue as everything else.
    const colour = { good: 'var(--green)', bad: 'var(--red)',
                     info: 'var(--blue)', neutral: 'var(--muted)' }[o.tone]
                || (o.terminal ? 'var(--red)' : 'var(--blue)');
    return `<button type="button" onclick="chooseOutcome('${esc(o.key)}')"
      title="${o.stops_email ? 'Also stops any email sequence for this contact' : ''}"
      style="padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer;font-family:var(--font);
             border:1px solid ${on ? colour : 'var(--border2)'};
             background:${on ? 'rgba(96,165,250,.12)' : 'transparent'};
             color:${on ? colour : 'var(--muted)'}">${esc(o.label)}</button>`;
  }).join('');
}

function chooseOutcome(key) {
  const o = _callOutcomes.find(x => x.key === key);
  _renderOutcomeButtons(key);

  const wrap  = document.getElementById('cq-next-wrap');
  const label = document.getElementById('cq-next-label');
  const hint  = document.getElementById('cq-next-hint');

  // The date is offered on every outcome that isn't final, and only *required*
  // where it would be meaningless without one. That split is what lets
  // "follow up later" exist without recording a commitment nobody made.
  const terminal = o && o.terminal && o.key !== 'booked';
  if (terminal) { wrap.style.display = 'none'; return; }

  wrap.style.display = 'block';
  const required = !!(o && o.wants_next_call);
  label.textContent = o && o.key === 'booked'
    ? 'Meeting date & time'
    : (required ? 'Next call' : 'Next call (optional)');
  hint.textContent = o && o.key === 'booked'
    ? 'You can add this to your calendar after saving.'
    : (required
        ? 'Puts this lead back in "Due now" at that time.'
        : 'Leave blank to keep the lead in the queue with no date attached.');

  if (required && !document.getElementById('cq-next-at').value) {
    // Default to tomorrow, same time — the common case for a callback. Only
    // prefilled when a date is required, so an optional field stays empty
    // unless you actually mean to set one.
    const d = new Date(Date.now() + 864e5);
    d.setSeconds(0, 0);
    document.getElementById('cq-next-at').value =
      new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 16);
  }
}

async function saveCallOutcome() {
  if (!_callLead) return;
  if (!_chosenOutcome) { toast('Pick an outcome first', 'err'); return; }

  const o = _callOutcomes.find(x => x.key === _chosenOutcome);
  const nextAt = document.getElementById('cq-next-at').value;
  if (o && o.wants_next_call && !nextAt) {
    toast(`"${o.label}" needs a date and time`, 'err');
    return;
  }
  if (o && o.stops_email) {
    const ok = confirm(
      `Mark ${_callLead.company || 'this lead'} as "${o.label}"?\n\n` +
      `This also stops any email sequence they're in` +
      (o.key === 'do_not_call' ? ` and unsubscribes them.` : `.`)
    );
    if (!ok) return;
  }

  const res = await api('/api/calls/log', 'POST', {
    contact_id:   _callLead.id,
    outcome:      _chosenOutcome,
    notes:        document.getElementById('cq-notes').value,
    next_call_at: nextAt || null,
    call_campaign_id: _callCampaignId || null,
  });
  if (!res || res.error) { toast((res && res.error) || 'Could not save', 'err'); return; }

  toast(res.stopped_email ? 'Logged — email sequence stopped' : 'Logged');
  loadCallCampaigns();
  _advanceToNextLead(_callLead.id);
}

// Keeps the session moving: back to the table between every call is what makes
// people stop using a calling tool after a week.
function _advanceToNextLead(justDoneId) {
  const idx  = _callLeads.findIndex(l => l.id === justDoneId);
  const next = _callLeads[idx + 1];
  _callLeads = _callLeads.filter(l => l.id !== justDoneId);
  if (next) { _renderCallTable(); openCallLead(next.id); }
  else loadCallQueue();
}

async function reopenCallLead(id) {
  const res = await api(`/api/calls/${id}/reopen`, 'POST');
  if (!res || res.error) { toast((res && res.error) || 'Could not reopen', 'err'); return; }
  toast('Back in the queue');
  if (currentTab('calling', 'todo') === 'leads') loadCallLeads();
  else loadCallQueue();
}

function skipCallLead() {
  if (!_callLead) return;
  _advanceToNextLead(_callLead.id);
}

function _renderCallHistory(history) {
  const el = document.getElementById('cq-history');
  if (!history.length) { el.innerHTML = ''; return; }
  const labels = Object.fromEntries(_callOutcomes.map(o => [o.key, o.label]));
  el.innerHTML =
    `<div class="text-muted text-small" style="margin-bottom:6px">Previous calls</div>` +
    history.map(h => `
      <div style="padding:6px 0;border-top:1px solid var(--border);font-size:12px">
        <span class="mono text-muted" style="font-size:11px">${esc((h.called_at || '').substring(0, 16))}</span>
        &nbsp;<span class="badge badge-gray">${esc(labels[h.outcome] || h.outcome)}</span>
        ${h.notes ? `<div class="text-muted" style="margin-top:3px;white-space:pre-wrap">${esc(h.notes)}</div>` : ''}
      </div>`).join('');
}

// ── Outcomes ─────────────────────────────────────────────────────────────────
//
// The useful vocabulary is the operator's. "Callback booked" and "follow up
// sometime" are different things, and forcing the second into the first puts a
// commitment in the system that was never made on the call.

const OUTCOME_TONES = [
  ['neutral', 'Neutral'], ['info', 'In progress'], ['good', 'Good'], ['bad', 'Dead'],
];

async function _renderOutcomeEditor() {
  const all = await api('/api/call-outcomes') || [];
  const el = document.getElementById('cq-outcome-editor');
  el.innerHTML = `
    <div class="card" style="padding:20px">
      <div class="card-title" style="margin-bottom:4px">Call outcomes</div>
      <div class="form-hint" style="margin-bottom:14px">
        Add your own alongside the built-in ones. <strong>Needs a date</strong> makes the
        date field required — leave it off for something like "follow up later",
        where you can still set a date but nothing was actually agreed.
        <strong>Ends the lead</strong> takes it out of every call queue;
        <strong>stops email</strong> also cancels any email sequence they're in.
      </div>
      <div class="table-wrap" style="margin-bottom:14px">
        <table>
          <thead><tr><th>Name</th><th>Tone</th><th>Needs date</th><th>Ends lead</th><th>Stops email</th><th></th></tr></thead>
          <tbody>${all.map(o => _outcomeRow(o)).join('')}</tbody>
        </table>
      </div>
      <div class="flex gap-2" style="flex-wrap:wrap;align-items:center">
        <input id="oc-new-label" placeholder="New outcome, e.g. Follow up later"
               style="flex:1;min-width:200px;background:var(--bg3);border:1px solid var(--border2);
                      border-radius:6px;padding:7px 12px;color:var(--text);font-size:13px;
                      font-family:var(--font)" />
        <select id="oc-new-tone" style="background:var(--bg3);border:1px solid var(--border2);
                border-radius:6px;padding:7px 10px;color:var(--text);font-size:13px;
                font-family:var(--font);cursor:pointer">
          ${OUTCOME_TONES.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}
        </select>
        <label style="display:flex;align-items:center;gap:5px;font-size:12px;color:var(--muted)">
          <input type="checkbox" id="oc-new-date" /> Needs date</label>
        <label style="display:flex;align-items:center;gap:5px;font-size:12px;color:var(--muted)">
          <input type="checkbox" id="oc-new-terminal" /> Ends lead</label>
        <label style="display:flex;align-items:center;gap:5px;font-size:12px;color:var(--muted)">
          <input type="checkbox" id="oc-new-stops" /> Stops email</label>
        <button class="btn btn-primary btn-sm" onclick="createOutcome()">Add</button>
      </div>
    </div>`;
}

function _outcomeRow(o) {
  const yes = v => v ? '✓' : '—';
  return `<tr${o.archived ? ' style="opacity:.5"' : ''}>
    <td>
      <input value="${esc(o.label)}" onchange="renameOutcome('${esc(o.key)}', this.value)"
             style="background:var(--bg3);border:1px solid var(--border2);border-radius:6px;
                    padding:5px 8px;color:var(--text);font-size:12px;font-family:var(--font);width:100%" />
      ${o.is_builtin ? '<span class="text-muted" style="font-size:10px">built-in</span>' : ''}
      ${o.archived ? '<span class="badge badge-gray" style="font-size:10px">archived</span>' : ''}
    </td>
    <td><select onchange="setOutcomeTone('${esc(o.key)}', this.value)"
          style="background:var(--bg3);border:1px solid var(--border2);border-radius:6px;
                 padding:4px 6px;color:var(--text);font-size:12px;font-family:var(--font)">
      ${OUTCOME_TONES.map(([v, l]) =>
        `<option value="${v}" ${o.tone === v ? 'selected' : ''}>${l}</option>`).join('')}
    </select></td>
    <td class="mono" style="font-size:12px">${yes(o.requires_date)}</td>
    <td class="mono" style="font-size:12px">${yes(o.is_terminal)}</td>
    <td class="mono" style="font-size:12px">${yes(o.stops_email)}</td>
    <td>${o.is_builtin
        ? '<span class="text-muted" style="font-size:11px">—</span>'
        : `<button class="btn btn-danger btn-sm" onclick="deleteOutcome('${esc(o.key)}')">✕</button>`}</td>
  </tr>`;
}

async function createOutcome() {
  const label = document.getElementById('oc-new-label').value.trim();
  if (!label) { toast('Give it a name', 'err'); return; }
  const res = await api('/api/call-outcomes', 'POST', {
    label,
    tone:          document.getElementById('oc-new-tone').value,
    requires_date: document.getElementById('oc-new-date').checked,
    is_terminal:   document.getElementById('oc-new-terminal').checked,
    stops_email:   document.getElementById('oc-new-stops').checked,
  });
  if (!res || res.error) { toast((res && res.error) || 'Could not add it', 'err'); return; }
  toast('Outcome added');
  await _renderOutcomeEditor();
  loadCallQueue();
}

async function renameOutcome(key, label) {
  // Only the label changes; the key stays, so old calls keep resolving.
  await api(`/api/call-outcomes/${key}`, 'PATCH', { label: label.trim() });
  loadCallQueue();
}

async function setOutcomeTone(key, tone) {
  await api(`/api/call-outcomes/${key}`, 'PATCH', { tone });
  loadCallQueue();
}

async function deleteOutcome(key) {
  if (!confirm('Remove this outcome?\n\nIf calls already used it, it is archived '
             + 'instead of deleted so their history stays readable.')) return;
  const res = await api(`/api/call-outcomes/${key}`, 'DELETE');
  if (!res || res.error) { toast((res && res.error) || 'Could not remove it', 'err'); return; }
  toast(res.result === 'archived' ? 'Archived — past calls keep their label' : 'Removed');
  await _renderOutcomeEditor();
  loadCallQueue();
}

// ── The script ───────────────────────────────────────────────────────────────

async function loadCallScript() {
  _callScript = await api('/api/call-script');
  _renderScriptFor(_callLead);
}

// Rendered through the same {{variable}} engine the emails use, so the script
// on screen says "Hi, is this Paradise Dental?" rather than leaving you to
// substitute it mid-sentence.
function _fillScript(text, contact) {
  if (!contact) return text;
  return String(text || '').replace(
    /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\|([^}]*))?\}\}/g,
    (_m, key, fallback) => {
      let v = contact[key];
      if (key === 'full_name') v = [contact.first_name, contact.last_name].filter(Boolean).join(' ');
      if (v === null || v === undefined || String(v).trim() === '') {
        return fallback !== undefined ? fallback.trim() : '';
      }
      return String(v).trim();
    });
}

function _renderScriptFor(contact) {
  const forEl = document.getElementById('cq-script-for');
  const body  = document.getElementById('cq-script-body');
  if (!_callScript) { body.innerHTML = ''; return; }

  forEl.textContent = contact
    ? `Filled in for ${contact.company || contact.email || 'this lead'}`
    : 'Pick a lead to fill in their details.';

  const sections = _callScript.sections || [];
  if (!sections.length) { body.innerHTML = '<div class="empty-state"><p>No script yet — write one under Script &amp; outcomes.</p></div>'; return; }

  // Each section collapses independently: mid-call you need to reach the right
  // objection in a second, not scroll a wall of text.
  body.innerHTML = sections.map((s, i) => `
    <details ${i === 0 ? 'open' : ''} style="margin-bottom:8px;border:1px solid var(--border);border-radius:6px">
      <summary style="cursor:pointer;padding:8px 12px;font-size:13px;font-weight:600;list-style:revert">${esc(s.title || 'Section')}</summary>
      <div style="padding:0 12px 10px;font-size:13px;line-height:1.55;white-space:pre-wrap;color:var(--text2)">${
        s.body ? esc(_fillScript(s.body, contact))
               : '<span class="text-muted">Empty — add your words under Script &amp; outcomes.</span>'}</div>
    </details>`).join('');
}

function _renderScriptEditor() {
  const el = document.getElementById('cq-script-editor');
  const sections = (_callScript && _callScript.sections) || [];
  el.innerHTML = `
    <div class="card" style="padding:20px">
      <div class="card-title" style="margin-bottom:4px">Your call script</div>
      <div class="form-hint" style="margin-bottom:14px">
        Your words, not mine — the sections below are empty on purpose. Use
        <span class="mono">{{company}}</span>, <span class="mono">{{first_name|there}}</span>
        and any other contact variable; they are filled in per lead on the right.
      </div>
      <div id="cq-script-sections" style="display:flex;flex-direction:column;gap:12px">
        ${sections.map((s, i) => _scriptSectionRow(s, i)).join('')}
      </div>
      <div class="flex gap-2" style="margin-top:14px;flex-wrap:wrap">
        <button class="btn btn-ghost btn-sm" onclick="addScriptSection()">+ Add section</button>
        <button class="btn btn-primary btn-sm" onclick="saveCallScript()">Save script</button>
      </div>
    </div>`;
}

function _scriptSectionRow(s, i) {
  return `
    <div class="cq-script-row" style="border:1px solid var(--border2);border-radius:8px;padding:12px">
      <div class="flex gap-2" style="margin-bottom:8px">
        <input class="cq-sec-title" value="${esc(s.title || '')}" placeholder="Section name"
               style="flex:1;background:var(--bg3);border:1px solid var(--border2);border-radius:6px;
                      padding:6px 10px;color:var(--text);font-size:13px;font-family:var(--font)" />
        <button class="btn btn-danger btn-sm" onclick="this.closest('.cq-script-row').remove()">✕</button>
      </div>
      <textarea class="cq-sec-body soft-input" style="min-height:90px"
                placeholder="What you say here…">${esc(s.body || '')}</textarea>
    </div>`;
}

function addScriptSection() {
  const wrap = document.getElementById('cq-script-sections');
  wrap.insertAdjacentHTML('beforeend', _scriptSectionRow({ title: '', body: '' }, wrap.children.length));
}

async function saveCallScript() {
  const sections = [...document.querySelectorAll('#cq-script-sections .cq-script-row')].map(r => ({
    title: r.querySelector('.cq-sec-title').value.trim(),
    body:  r.querySelector('.cq-sec-body').value,
  })).filter(s => s.title || s.body);

  const res = await api('/api/call-script', 'PUT', { sections });
  if (!res || res.error) { toast((res && res.error) || 'Could not save the script', 'err'); return; }
  toast('Script saved ✓');
  await loadCallScript();
  _renderScriptEditor();
}
