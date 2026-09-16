// ── Contacts: every business, on any channel or none ─────────────────────────
//
// The channel pages each list their own leads. This is the list of businesses
// behind all of them: where each one is, what's happened with it, and the
// place to send a business to a channel it isn't on yet.

let _ctView = 'all';
let _ctDetailId = null;
let _ctEditId = null;

const CT_VIEW_HINTS = {
  unassigned: 'Businesses on no channel — taken off one, scraped with nothing to reach them on, or added by hand. Tick some and send them to a channel.',
  dnc: 'Businesses that asked not to be contacted or unsubscribed. They stay here so a later scrape or import can never put them back on a list.',
};

function callOutcomeLabel(key) {
  if (!key) return 'never called';
  const meta = (typeof CALL_STATUS_META !== 'undefined') && CALL_STATUS_META[key];
  return meta ? meta.label.toLowerCase() : String(key).replace(/_/g, ' ');
}

// Where a business is, as pills -- the "category" of each contact.
function channelPills(r) {
  const out = [];
  if (r.email_count > 0) {
    const bad = ['unsubscribed', 'bounced'].includes(r.email_status);
    const state = bad ? r.email_status : (r.email_enrollment ? r.email_enrollment : 'not enrolled');
    out.push(pill(`Email · ${state}`, bad ? 'red' : 'green'));
  }
  if (r.call_lead_id) out.push(pill(`Calling · ${callOutcomeLabel(r.call_status)}`, 'blue'));
  if (r.wa_stage && !['moved', 'removed'].includes(r.wa_stage)) {
    out.push(pill(`WhatsApp · ${(WA_STAGE_META[r.wa_stage] || [r.wa_stage])[0].toLowerCase()}`, 'purple'));
  }
  if (!out.length) {
    out.push(r.do_not_contact ? pill('Do not contact', 'red') : pill('Unassigned', 'dashed', r.unassigned_label || ''));
  } else if (r.do_not_contact) {
    out.push(pill('Do not contact', 'red'));
  }
  return `<span class="pills">${out.join('')}</span>`;
}

const _CT_COLUMNS_BASE = [
  { key: 'company', label: 'Business', sort: true,
    render: r => `<span class="biz-name">${esc(r.company || 'Unnamed business')}</span>${
      r.city || r.address ? `<span class="sub">${esc(r.city || r.address)}</span>` : ''}` },
  { key: 'channels', label: 'Channels', render: channelPills },
  { key: 'phone', label: 'Phone', sort: true, cls: 'num', render: r => esc(r.phone || '') },
  { key: 'email', label: 'Email', sort: true, cls: 'nowrap',
    render: r => r.email ? `<span class="mono" style="font-size:12px">${esc(r.email)}</span>${
      r.email_count > 1 ? ` <span class="text-muted">+${r.email_count - 1}</span>` : ''}` : '' },
  { key: 'category', label: 'Category', sort: true, render: r => esc(r.category || '') },
  { key: 'created_at', label: 'Added', sort: true, cls: 'num', render: r => esc(shortDate(r.created_at)) },
];

const _CT_REASON_COLUMN = {
  key: 'reason', label: "Why it's here",
  render: r => pill(r.unassigned_label || 'Unassigned', 'dashed'),
};

createLeadTable({
  id: 'ct',
  url: '/api/businesses',
  idsUrl: '/api/businesses/ids',
  columns: _CT_COLUMNS_BASE,
  empty: 'No contacts match.',
  params: () => ({
    view: _ctView,
    q: document.getElementById('ct-search')?.value.trim(),
    channel: _ctView === 'all' ? document.getElementById('ct-channel')?.value : '',
    source_job_id: document.getElementById('ct-source')?.value,
  }),
  onRowClick: r => openContactDetail(r.id),
  bulk: () => `
    <button class="btn btn-ghost btn-sm" onclick="contactsToEmail(LT.ct.selectedIds())">+ Email campaign</button>
    <button class="btn btn-ghost btn-sm" onclick="contactsToCalling(LT.ct.selectedIds())">+ Calling</button>
    <button class="btn btn-ghost btn-sm" onclick="contactsToWhatsApp(LT.ct.selectedIds())">+ WhatsApp</button>
    <button class="btn btn-danger btn-sm" onclick="deleteBusinesses(LT.ct.selectedIds())">Delete</button>`,
  menu: r => [
    { label: 'Open details', run: `openContactDetail(${r.id})` },
    { label: 'Edit', run: `openBusinessForm(${r.id})` },
    r.email_count > 0 && { label: 'Enroll in an email campaign…', run: `contactsToEmail([${r.id}])` },
    !r.call_lead_id && { label: 'Add to Calling…', run: `contactsToCalling([${r.id}])` },
    // Not offered for a number already ruled out as not on WhatsApp.
    (!r.wa_stage || r.wa_stage === 'removed') && !r.do_not_contact
      && { label: 'Add to WhatsApp…', run: `contactsToWhatsApp([${r.id}])` },
    { label: 'Delete', run: `deleteBusinesses([${r.id}])`, danger: true },
  ],
  onLoad: data => {
    const c = data.counts || {};
    setTabCount('contacts', 'all', c.all);
    setTabCount('contacts', 'unassigned', c.unassigned, true);
    setTabCount('contacts', 'dnc', c.dnc);
  },
});

onTab('contacts', name => {
  _ctView = name;
  LT.ct.cfg.columns = name === 'unassigned'
    ? [_CT_COLUMNS_BASE[0], _CT_REASON_COLUMN, ..._CT_COLUMNS_BASE.slice(2)]
    : _CT_COLUMNS_BASE;
  document.getElementById('ct-channel').style.display = name === 'all' ? '' : 'none';
  const hint = document.getElementById('ct-view-hint');
  hint.style.display = CT_VIEW_HINTS[name] ? 'block' : 'none';
  hint.textContent = CT_VIEW_HINTS[name] || '';
  LT.ct.load({ resetPage: true, keepSelection: false });
});

async function loadContacts() {
  const sources = await api('/api/contacts/sources') || [];
  const sel = document.getElementById('ct-source');
  const keep = sel.value;
  sel.innerHTML = '<option value="">All lists</option>' +
    sources.map(s => `<option value="${esc(String(s.job_id))}">${esc(s.label)} (${s.count})</option>`).join('');
  sel.value = keep;
  setTab('contacts', currentTab('contacts', 'all'));
}

// From anywhere in the app: jump to one business in Contacts.
async function openBusiness(id) {
  showSection('contacts');
  openContactDetail(id);
}

// ── Detail panel ─────────────────────────────────────────────────────────────

function closeContactDetail() {
  _ctDetailId = null;
  LT.ct.currentId = null;
  LT.ct.render();
  document.getElementById('ct-detail').style.display = 'none';
  document.getElementById('ct-split').style.gridTemplateColumns = '1fr';
}

async function openContactDetail(id) {
  const d = await api(`/api/businesses/${id}`);
  if (!d || d.error) { toast((d && d.error) || 'Could not open that contact', 'err'); return; }
  _ctDetailId = id;
  LT.ct.currentId = id;
  LT.ct.render();
  const panel = document.getElementById('ct-detail');
  document.getElementById('ct-split').style.gridTemplateColumns = '';
  panel.style.display = 'block';

  const links = [];
  if (d.phone) links.push(`<a href="tel:${esc(d.phone)}" style="color:var(--blue)">${esc(d.phone)}</a>`);
  if (d.website) links.push(`<a href="${esc(d.website)}" target="_blank" rel="noopener" style="color:var(--blue)">${esc(d.domain || d.website)}</a>`);
  const facts = [d.category, d.city || d.address, d.rating != null ? `${d.rating}★ (${d.review_count ?? 0})` : '']
    .filter(Boolean).map(esc).join(' · ');

  const where = [];
  (d.enrollments || []).slice(0, 3).forEach(e =>
    where.push(`<div>${pill('Email', 'green')} ${esc(e.campaign)} — ${esc(e.status === 'queued' ? `step ${e.current_step}` : e.status)}</div>`));
  if ((d.emails || []).length && !(d.enrollments || []).length) {
    where.push(`<div>${pill('Email', 'green')} not in a campaign yet</div>`);
  }
  if (d.call && !d.call.removed_at) {
    const camps = (d.call.campaigns || []).map(c => esc(c.name)).join(', ');
    where.push(`<div>${pill('Calling', 'blue')} ${esc(callOutcomeLabel(d.call.call_status))}${
      d.call.next_call_at ? ` · next ${esc(d.call.next_call_at.substring(0, 16))}` : ''}${camps ? ` · ${camps}` : ''}</div>`);
  }
  if (d.whatsapp) {
    const w = d.whatsapp;
    const off = w.moved_to || w.removed_at;
    where.push(`<div>${pill('WhatsApp', off ? 'dashed' : 'purple')} ${
      off ? (w.moved_to ? 'not on WhatsApp' : 'taken off') : esc(w.campaign_name || 'no campaign')}${
      w.replied ? ' · replied' : ''}</div>`);
  }
  if (d.do_not_contact) where.push(`<div>${pill('Do not contact', 'red')} asked to be left alone</div>`);
  if (!where.length) where.push(`<div>${pill('Unassigned', 'dashed')} not on any channel</div>`);

  const onWa = d.whatsapp && !d.whatsapp.moved_to && !d.whatsapp.removed_at;
  const canWa = !onWa && !(d.whatsapp && d.whatsapp.moved_to) && !d.do_not_contact;
  const onCall = d.call && !d.call.removed_at;

  panel.innerHTML = `
    <div class="flex items-center gap-2" style="justify-content:space-between">
      <h3>${esc(d.company || 'Unnamed business')}</h3>
      <button class="btn btn-ghost btn-sm" onclick="closeContactDetail()" title="Close">✕</button>
    </div>
    <div class="text-small" style="display:flex;gap:10px;flex-wrap:wrap">${links.join('')}</div>
    ${facts ? `<div class="text-muted text-small" style="margin-top:2px">${facts}</div>` : ''}

    <span class="field-label">Where it is</span>
    <div style="display:flex;flex-direction:column;gap:6px;font-size:12.5px">${where.join('')}</div>
    <div class="flex gap-2" style="flex-wrap:wrap;margin-top:10px">
      ${(d.emails || []).length ? `<button class="btn btn-ghost btn-sm" onclick="contactsToEmail([${d.id}])">+ Email campaign</button>` : ''}
      ${!onCall && !d.do_not_contact ? `<button class="btn btn-ghost btn-sm" onclick="contactsToCalling([${d.id}])">+ Calling</button>` : ''}
      ${canWa ? `<button class="btn btn-ghost btn-sm" onclick="contactsToWhatsApp([${d.id}])">+ WhatsApp</button>` : ''}
      <button class="btn btn-ghost btn-sm" onclick="openBusinessForm(${d.id})">✎ Edit</button>
    </div>

    ${(d.emails || []).length ? `<span class="field-label">Email addresses</span>
      <div style="display:flex;flex-direction:column;gap:4px">${d.emails.map(e =>
        `<div class="text-small"><span class="mono">${esc(e.email)}</span> ${
          e.status !== 'active' ? pill(e.status, 'red') : ''}${e.duplicate_of ? ' <span class="text-muted">(backup)</span>' : ''}</div>`).join('')}</div>` : ''}

    <span class="field-label">Notes</span>
    <textarea class="soft-input" id="ct-notes" style="min-height:70px"
              placeholder="Anything worth remembering about this business…"
              onchange="saveContactNotes(${d.id}, this.value)">${esc(d.notes || '')}</textarea>

    <span class="field-label">History</span>
    ${(d.timeline || []).length ? `<div class="timeline">${d.timeline.slice(0, 40).map(t => `
      <div class="item">
        <span class="when">${esc((t.at || '').substring(0, 16))}</span>
        <span class="channel-${t.channel}">●</span> ${esc(t.text)}
        ${t.detail ? `<div class="detail">${esc(t.detail.length > 240 ? t.detail.substring(0, 240) + '…' : t.detail)}</div>` : ''}
      </div>`).join('')}</div>` : '<div class="text-muted text-small">Nothing sent, called or messaged yet.</div>'}
  `;
}

async function saveContactNotes(id, notes) {
  const res = await api(`/api/businesses/${id}`, 'PUT', { notes });
  if (!res || res.error) { toast((res && res.error) || 'Could not save the note', 'err'); return; }
  toast('Note saved');
}

function _refreshContactsAfterChange() {
  if (document.getElementById('section-contacts').classList.contains('active')) {
    LT.ct.load();
    if (_ctDetailId) openContactDetail(_ctDetailId);
  }
}

// ── Add / edit ───────────────────────────────────────────────────────────────

async function openBusinessForm(id = null) {
  _ctEditId = id;
  const fields = ['name', 'phone', 'email', 'website', 'category', 'city', 'address', 'notes'];
  fields.forEach(f => { document.getElementById(`bf-${f}`).value = ''; });
  document.getElementById('bf-title').textContent = id ? 'Edit contact' : 'Add contact';
  document.getElementById('bf-email-group').style.display = id ? 'none' : '';
  document.getElementById('bf-hint').style.display = id ? 'none' : '';
  if (id) {
    const d = await api(`/api/businesses/${id}`);
    if (!d || d.error) { toast('Could not load that contact', 'err'); return; }
    fields.forEach(f => {
      if (f !== 'email') document.getElementById(`bf-${f}`).value = d[f] || '';
    });
  }
  openModal('modal-business');
}

async function saveBusinessForm() {
  const payload = {};
  ['name', 'phone', 'email', 'website', 'category', 'city', 'address', 'notes'].forEach(f => {
    payload[f] = document.getElementById(`bf-${f}`).value.trim();
  });
  if (!payload.name) { toast('Give the business a name', 'err'); return; }
  let res;
  if (_ctEditId) {
    delete payload.email;
    res = await api(`/api/businesses/${_ctEditId}`, 'PUT', payload);
  } else {
    res = await api('/api/businesses', 'POST', payload);
  }
  if (!res || res.error) { toast((res && res.error) || 'Could not save it', 'err'); return; }
  closeModal('modal-business');
  toast(_ctEditId ? 'Saved ✓' : (res.created ? 'Contact added ✓' : 'You already had this one — opened it'));
  if (!_ctEditId && document.getElementById('section-contacts').classList.contains('active')) {
    LT.ct.load();
    openContactDetail(res.id);
  } else {
    _refreshContactsAfterChange();
  }
}

// ── Sending contacts to a channel ────────────────────────────────────────────
//
// Shared by the Contacts table, its row menus and the detail panel. Each asks
// the one question that channel needs answered -- which campaign, and for
// WhatsApp which country -- then reports anything it had to skip, and why.

async function contactsToEmail(ids) {
  if (!ids.length) return;
  const campaigns = await api('/api/campaigns') || [];
  if (!campaigns.length) {
    toast('Create an email campaign first (Email → + New campaign)', 'err');
    return;
  }
  const cid = await chooseDialog({
    title: `Enroll ${ids.length} in an email campaign`,
    body: `<label class="field-label">Campaign</label>
      ${campaignSelectHtml('ct-email-camp', campaigns, { allowNew: false })}
      <div class="form-hint">Each business is enrolled by its best address. Businesses with no email
      address, or already in another campaign, are skipped and counted.</div>`,
    confirm: 'Enroll',
    collect: () => document.getElementById('ct-email-camp').value,
  });
  if (!cid) return;
  const res = await api('/api/businesses/enroll', 'POST', { business_ids: ids, campaign_id: cid });
  if (!res || res.error) { toast((res && res.error) || 'Could not enroll them', 'err'); return; }
  const skipped = Object.values(res.skipped || {}).reduce((a, b) => a + b, 0);
  toast(`Enrolled ${res.enrolled}` + [
    res.no_email ? `${res.no_email} had no email address` : '',
    skipped ? `${skipped} skipped (already being emailed)` : '',
  ].filter(Boolean).map((s, i) => (i ? ', ' : ' — ') + s).join(''));
  LT.ct.clear();
  _refreshContactsAfterChange();
}

async function contactsToCalling(ids, { onDone } = {}) {
  if (!ids.length) return;
  const campaigns = await api('/api/call-campaigns') || [];
  const picked = await chooseDialog({
    title: `Add ${ids.length} to Calling`,
    body: `<label class="field-label">Call campaign (optional)</label>
      ${campaignSelectHtml('ct-call-camp', campaigns, { allowNone: true, noneLabel: 'Just add them to Calling' })}
      <div class="form-hint">Businesses with no phone number, or who asked not to be contacted, are skipped and counted.</div>`,
    confirm: 'Add to Calling',
    collect: () => ({ ready: true }),
  });
  if (!picked) return;
  const campaignId = await resolveCampaignSelect('ct-call-camp', '/api/call-campaigns');
  if (campaignId === null) return;
  const first = await api('/api/calls/add', 'POST', { business_ids: ids, call_campaign_id: campaignId || null });
  if (!first || first.error) { toast((first && first.error) || 'Could not add them', 'err'); return; }
  const final = await confirmChannelConflicts(first, () => api('/api/calls/add', 'POST', {
    business_ids: first.conflicts.map(c => c.business_id), call_campaign_id: campaignId || null,
    confirm_conflicts: true,
  }));
  toast(describeAdd(_sumCounts(first, final), 'Added to Calling:'));
  if (LT.ct) LT.ct.clear();
  _refreshContactsAfterChange();
  if (onDone) onDone();
}

async function contactsToWhatsApp(ids, { onDone } = {}) {
  if (!ids.length) return;
  const campaigns = (await api('/api/wa/campaigns') || []).filter(c => c.status !== 'archived');
  const picked = await chooseDialog({
    title: `Add ${ids.length} to WhatsApp`,
    body: `<label class="field-label">WhatsApp campaign</label>
      ${campaignSelectHtml('ct-wa-camp', campaigns, { selected: campaigns[0] ? campaigns[0].id : '' })}
      <label class="field-label">Country the numbers are in</label>
      <select id="ct-wa-country" class="filter-select" style="max-width:100%;width:100%">
        <option value="AE" ${campaigns[0] && campaigns[0].country === 'QA' ? '' : 'selected'}>United Arab Emirates</option>
        <option value="QA" ${campaigns[0] && campaigns[0].country === 'QA' ? 'selected' : ''}>Qatar</option>
      </select>
      <div class="form-hint">Their messages are written from the campaign's templates. Businesses with no
      phone, already ruled out as not on WhatsApp, or who asked not to be contacted are skipped and counted.</div>`,
    confirm: 'Add to WhatsApp',
    collect: () => {
      const v = document.getElementById('ct-wa-camp').value;
      if (!v) { toast('Pick a campaign', 'err'); return null; }
      return { country: document.getElementById('ct-wa-country').value };
    },
  });
  if (!picked) return;
  const campaignId = await resolveCampaignSelect('ct-wa-camp', '/api/wa/campaigns', { country: picked.country });
  if (!campaignId) return;
  const body = { business_ids: ids, country: picked.country, wa_campaign_id: campaignId };
  const first = await api('/api/wa/add-existing', 'POST', body);
  if (!first || first.error) { toast((first && first.error) || 'Could not add them', 'err'); return; }
  const final = await confirmChannelConflicts(first, () => api('/api/wa/add-existing', 'POST', {
    ...body, business_ids: first.conflicts.map(c => c.business_id), confirm_conflicts: true,
  }));
  toast(describeAdd(_sumCounts(first, final), 'Added to WhatsApp:'));
  if (LT.ct) LT.ct.clear();
  _refreshContactsAfterChange();
  if (onDone) onDone();
}

// confirmChannelConflicts hands back the first response when declined, or the
// confirmed resend's -- add the two so the toast counts everything.
function _sumCounts(first, final) {
  if (final === first) return first;
  const out = { ...first };
  ['added', 'already', 'no_phone', 'opted_out', 'ruled_out', 'in_campaign'].forEach(k => {
    out[k] = (first[k] || 0) + (final[k] || 0);
  });
  return out;
}

async function deleteBusinesses(ids) {
  if (!ids.length) return;
  const ok = await chooseDialog({
    title: `Delete ${ids.length} contact${ids.length === 1 ? '' : 's'}?`,
    body: `<p class="text-small" style="line-height:1.6">This removes ${ids.length === 1 ? 'the business' : 'them'} from every
      channel — email addresses, call history and WhatsApp messages included. It can't be undone.</p>
      <p class="text-muted text-small" style="margin-top:8px;line-height:1.6">Anyone who unsubscribed, bounced or asked
      not to be contacted is kept, so a later scrape can't put them back on a list.</p>`,
    confirm: 'Delete', danger: true,
  });
  if (!ok) return;
  const res = await api('/api/businesses/delete', 'POST', { business_ids: ids });
  if (!res || res.error) { toast((res && res.error) || 'Could not delete them', 'err'); return; }
  toast(`Deleted ${res.deleted}` + (res.kept ? ` — kept ${res.kept} who asked not to be contacted` : ''));
  LT.ct.clear();
  if (ids.includes(_ctDetailId)) closeContactDetail();
  LT.ct.load();
}
