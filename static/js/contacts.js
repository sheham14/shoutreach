// ── Contacts: every business, on any channel or none ─────────────────────────
//
// The channel pages each list their own leads. This is the list of businesses
// behind all of them: where each one is, what's happened with it, and the
// place to send a business to a channel it isn't on yet.

let _ctView = 'all';
let _ctEditId = null;
let _ctSources = [];

const CT_VIEW_HINTS = {
  unassigned: 'Businesses on no channel — taken off one, scraped with nothing to reach them on, or added by hand. Tick some and send them to a channel.',
  dnc: 'Businesses that asked not to be contacted or unsubscribed. They stay here so a later scrape or import can never put them back on a list.',
};

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
  mobile: r => mCard(`<span class="biz-name">${esc(r.company || 'Unnamed business')}</span>`,
    channelPills(r), esc([r.city || r.address, r.phone].filter(Boolean).join(' · '))),
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
  _ctSources = await api('/api/contacts/sources') || [];
  const sel = document.getElementById('ct-source');
  const keep = window._contactsPresetSource ?? sel.value;
  window._contactsPresetSource = undefined;
  sel.innerHTML = '<option value="">All lists</option>' +
    _ctSources.map(s => `<option value="${esc(String(s.job_id))}">${esc(s.label)} (${s.count})</option>`).join('');
  sel.value = _ctSources.some(s => String(s.job_id) === String(keep)) ? keep : '';
  document.getElementById('ct-add-list').style.display = sel.value ? '' : 'none';
  setTab('contacts', currentTab('contacts', 'all'));
}

function contactsSourceChanged() {
  document.getElementById('ct-add-list').style.display = document.getElementById('ct-source').value ? '' : 'none';
  LT.ct.filter();
}

function addSelectedListToChannel() {
  const id = document.getElementById('ct-source').value;
  const list = _ctSources.find(s => String(s.job_id) === id);
  if (list) addListToChannel(id, list.label, list.count);
}

// From anywhere in the app: jump to one business in Contacts.
async function openBusiness(id) {
  showSection('contacts');
  openContactDetail(id);
}

// ── Detail panel ─────────────────────────────────────────────────────────────

function closeContactDetail() { closeLeadPanel('ct-detail'); }

function openContactDetail(id) {
  LT.ct.currentId = id;
  LT.ct.render();
  return openLeadPanel(id, {
    panelId: 'ct-detail', splitId: 'ct-split',
    onClose: () => { LT.ct.currentId = null; LT.ct.render(); },
  });
}

// After an edit anywhere, redraw whichever lists and lead panels are showing.
function _refreshContactsAfterChange() {
  if (document.getElementById('section-contacts').classList.contains('active')) LT.ct.load();
  Object.keys(LeadPanel.current).forEach(refreshLeadPanel);
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
  const [all] = await Promise.all([api('/api/wa/campaigns'), loadCountries()]);
  const campaigns = (all || []).filter(c => c.status !== 'archived');
  const picked = await chooseDialog({
    title: `Add ${ids.length} to WhatsApp`,
    body: `<label class="field-label">WhatsApp campaign</label>
      ${campaignSelectHtml('ct-wa-camp', campaigns, { selected: campaigns[0] ? campaigns[0].id : '' })}
      <label class="field-label">Country the numbers are in</label>
      ${countryPickerHtml('ct-wa-country', (campaigns[0] && campaigns[0].country) || _countries.used[0] || 'AE')}
      <div class="form-hint">Their messages are written from the campaign's templates, ready to send. Businesses with no
      phone, already ruled out as not on WhatsApp, or who asked not to be contacted are skipped and counted.</div>`,
    confirm: 'Add to WhatsApp',
    collect: () => {
      const v = document.getElementById('ct-wa-camp').value;
      if (!v) { toast('Pick a campaign', 'err'); return null; }
      const country = countryValue('ct-wa-country');
      if (!country) { toast('Pick the country from the list', 'err'); return null; }
      return { country };
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
  ['added', 'already', 'no_phone', 'no_email', 'opted_out', 'ruled_out', 'in_campaign'].forEach(k => {
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
  Object.entries(LeadPanel.current).forEach(([panelId, cur]) => {
    if (ids.includes(cur.businessId)) closeLeadPanel(panelId);
  });
  LT.ct.load();
}

// ── A whole list to a channel ────────────────────────────────────────────────
//
// Everything one scrape found -- or everything added by hand -- into a channel
// and campaign in one go. From the Scraper's "Your scrapes" and from Contacts
// when a list is picked. Same rules as adding a selection: skips are counted,
// and anyone already being worked on another channel is asked about first.

async function addListToChannel(sourceJobId, label = '', count = null) {
  const [emailCamps, callCamps, waAll] = await Promise.all([
    api('/api/campaigns'), api('/api/call-campaigns'), api('/api/wa/campaigns'), loadCountries(),
  ]);
  const wa = (waAll || []).filter(c => c.status !== 'archived');
  const emails = Array.isArray(emailCamps) ? emailCamps : [];
  const box = 'onclick="event.stopPropagation()" style="margin-top:8px;display:flex;flex-direction:column;gap:8px"';
  const picked = await chooseDialog({
    title: `Add ${count != null ? `all ${count}` : 'everyone'} to a channel`,
    width: 540,
    body: `${label ? `<p class="text-muted text-small" style="margin-bottom:12px">${esc(label)}</p>` : ''}
      ${choiceCard({ name: 'la-ch', value: 'whatsapp', title: 'WhatsApp', checked: true,
        hint: 'Each lead gets its message written from the campaign, ready to send.',
        extra: `<div ${box}>${campaignSelectHtml('la-wa-camp', wa, { selected: wa[0] ? wa[0].id : '' })}
          ${countryPickerHtml('la-wa-country', (wa[0] && wa[0].country) || _countries.used[0] || 'AE')}</div>` })}
      ${choiceCard({ name: 'la-ch', value: 'calling', title: 'Calling',
        hint: 'Onto your call list, and into a campaign if you pick one.',
        extra: `<div ${box}>${campaignSelectHtml('la-call-camp', callCamps || [],
          { allowNone: true, noneLabel: 'No campaign — just add to Calling' })}</div>` })}
      ${choiceCard({ name: 'la-ch', value: 'email', title: 'Email campaign', disabled: !emails.length,
        hint: emails.length ? 'Enrolled by their best address.' : 'Create an email campaign first.',
        extra: emails.length ? `<div ${box}>${campaignSelectHtml('la-email-camp', emails, { allowNew: false })}</div>` : '' })}
      <div class="form-hint">Anyone with nothing to reach them on for that channel, already there, or who asked not to be
        contacted is skipped and counted. Anyone already being worked on another channel is shown to you first.</div>`,
    confirm: 'Add them',
    collect: () => {
      const channel = chosenRadio('la-ch');
      if (channel !== 'whatsapp') return { channel };
      if (!document.getElementById('la-wa-camp').value) { toast('Pick the WhatsApp campaign', 'err'); return null; }
      const country = countryValue('la-wa-country');
      if (!country) { toast('Pick the country from the list', 'err'); return null; }
      return { channel, country };
    },
  });
  if (!picked) return;

  let campaignId = '';
  if (picked.channel === 'whatsapp') {
    campaignId = await resolveCampaignSelect('la-wa-camp', '/api/wa/campaigns', { country: picked.country });
  } else if (picked.channel === 'calling') {
    campaignId = await resolveCampaignSelect('la-call-camp', '/api/call-campaigns');
  } else {
    campaignId = document.getElementById('la-email-camp').value;
  }
  if (campaignId === null) return;

  const body = { source_job_id: sourceJobId, channel: picked.channel, campaign_id: campaignId || null,
                 country: picked.country || '' };
  toast('Adding…');
  const first = await api('/api/lists/add-to', 'POST', body);
  if (!first || first.error) { toast((first && first.error) || 'Could not add them', 'err'); return; }
  const final = await confirmChannelConflicts(first, () => api('/api/lists/add-to', 'POST', {
    ...body, business_ids: first.conflicts.map(c => c.business_id), confirm_conflicts: true,
  }));
  const where = { whatsapp: 'WhatsApp', calling: 'Calling', email: 'Email' }[picked.channel];
  toast(describeAdd(_sumCounts(first, final), `Added to ${where}:`));
  loadCountries(true);
  _refreshContactsAfterChange();
}
