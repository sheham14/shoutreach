// ── WhatsApp ─────────────────────────────────────────────────────────────────
//
// Sending is always manual. Every action in this file either reads state or
// stages one (a signal, a draft, a sent-date) -- the only thing that ever
// reaches WhatsApp is the operator's own tap on Send, after this code opens
// a wa.me link and gets out of the way. If you're tempted to make "Open in
// WhatsApp" fire automatically on a schedule, don't -- see
// docs/WhatsApp Module Handover.md for why that's off the table.

let _waBucket = 'review';
let _waQueue = [];
let _waCurrent = null;
let _waCampaigns = [];
const _waCampaignDetail = {};   // id -> campaign with templates, for previews
let _waSignal = null;           // the choice made in the review panel
let _waOpenNext = null;         // {tab, filter} to land on, from another page

const WA_SIGNAL_LABELS = { gap_found: 'No online booking', no_gap: 'Has online booking', unclear: 'Unclear' };

// Phrases that finish the sentence each template puts {{signal_detail}} in,
// one tap instead of typing the same observation forty times.
const WA_DETAIL_PHRASES = {
  gap_found: ['no way to book online', 'only a phone number to call', 'just a contact form',
              'bookings only over WhatsApp'],
  no_gap: ['an online booking button', 'booking right on their website', 'a link to a booking app'],
};

const WA_COUNTRIES = [['AE', 'United Arab Emirates'], ['QA', 'Qatar']];

function _waFilter() { return document.getElementById('wa-campaign-filter')?.value || ''; }

onTab('whatsapp', name => {
  if (name === 'todo') loadWaTodo();
  if (name === 'leads') LT.wl.load({ resetPage: true });
  if (name === 'campaigns') renderWaCampaigns();
  if (name === 'templates') renderWaTemplateEditor(_waFilter() || (_waCampaigns[0] && _waCampaigns[0].id));
});

async function loadWaCampaigns() {
  _waCampaigns = await api('/api/wa/campaigns') || [];
  setTabCount('whatsapp', 'campaigns', _waCampaigns.length);
  const sel = document.getElementById('wa-campaign-filter');
  const keep = sel.value;
  sel.innerHTML = '<option value="">All campaigns</option>' +
    _waCampaigns.map(c => `<option value="${c.id}">${esc(c.name)}${c.status === 'archived' ? ' (archived)' : ''}</option>`).join('');
  sel.value = _waCampaigns.some(c => String(c.id) === keep) ? keep : '';
  return _waCampaigns;
}

async function _waCampaign(id) {
  if (!id) return null;
  if (!_waCampaignDetail[id]) _waCampaignDetail[id] = await api(`/api/wa/campaigns/${id}`);
  return _waCampaignDetail[id];
}

async function loadWhatsApp() {
  await loadWaCampaigns();
  const next = _waOpenNext;
  _waOpenNext = null;
  if (next && next.filter !== undefined) document.getElementById('wa-campaign-filter').value = String(next.filter);
  const tab = next ? next.tab : currentTab('whatsapp', 'todo');
  setTab('whatsapp', tab);
  if (tab !== 'todo') refreshWaCounts();
  if (tab !== 'leads') {
    const page = await api('/api/wa/leads/page?per_page=1');
    if (page && page.total !== undefined) setTabCount('whatsapp', 'leads', page.total);
  }
}

function waCampaignFilterChanged() {
  const tab = currentTab('whatsapp', 'todo');
  if (tab === 'todo') loadWaTodo();
  else if (tab === 'leads') LT.wl.load({ resetPage: true, keepSelection: false });
  else if (tab === 'templates') renderWaTemplateEditor(_waFilter() || (_waCampaigns[0] && _waCampaigns[0].id));
}

// From the Dashboard and elsewhere.
function openWhatsAppTodo(bucket) {
  _waBucket = bucket;
  _waCurrent = null;
  _waOpenNext = { tab: 'todo', filter: '' };
  showSection('whatsapp');
}

function openWhatsAppCampaign(id) {
  _waOpenNext = { tab: 'leads', filter: id };
  if (document.getElementById('section-whatsapp').classList.contains('active')) loadWhatsApp();
  else showSection('whatsapp');
}

// ── To do ────────────────────────────────────────────────────────────────────

async function refreshWaCounts() {
  const q = _waFilter() ? `?wa_campaign_id=${_waFilter()}` : '';
  const s = await api(`/api/wa/summary${q}`) || {};
  document.getElementById('wa-count-review').textContent = s.awaiting_review || 0;
  document.getElementById('wa-count-ready').textContent = s.ready_to_send || 0;
  document.getElementById('wa-count-due').textContent = s.due || 0;
  document.getElementById('wa-count-confirmed').textContent = s.awaiting_draft || 0;
  document.getElementById('wa-draft-batch-btn').style.display = s.awaiting_draft ? 'inline-flex' : 'none';
  document.getElementById('wa-checking-note').textContent = s.pending_signal
    ? `${s.pending_signal} website${s.pending_signal === 1 ? '' : 's'} still being checked` : '';
  const work = (s.awaiting_review || 0) + (s.ready_to_send || 0) + (s.due || 0);
  setTabCount('whatsapp', 'todo', work, true);
  if (!_waFilter()) {
    const nav = document.getElementById('nav-count-whatsapp');
    if (nav) nav.textContent = work ? work : '';
  }
  return s;
}

async function loadWaTodo() {
  ['review', 'ready', 'due'].forEach(b =>
    document.getElementById(`wa-chip-${b}`).classList.toggle('active', b === _waBucket));
  const [s] = await Promise.all([refreshWaCounts(), loadWaQueue()]);
  return s;
}

function setWaBucket(bucket) {
  _waBucket = bucket;
  _waCurrent = null;
  loadWaTodo();
}

async function loadWaQueue() {
  const camp = _waFilter() ? `&wa_campaign_id=${_waFilter()}` : '';
  let rows;
  if (_waBucket === 'due') {
    rows = await api(`/api/wa/followups-due?x=1${camp}`) || [];
  } else {
    const status = _waBucket === 'review' ? 'signal_ready' : 'drafted';
    rows = await api(`/api/wa/leads?status=${status}&limit=500${camp}`) || [];
    // Oldest first: the list is worked top to bottom.
    rows.sort((a, b) => a.id - b.id);
  }
  _waQueue = Array.isArray(rows) ? rows : [];
  _renderWaQueue();
  if (!_waQueue.some(l => l.id === _waCurrent)) _waCurrent = _waQueue.length ? _waQueue[0].id : null;
  if (_waCurrent) openWaLead(_waCurrent);
  else _renderWaEmptyPanel();
}

function _renderWaEmptyPanel() {
  const msg = {
    review: 'Nothing to review. New leads show up here once their website has been checked.',
    ready: 'Nothing ready to send. Confirm leads under Needs review, then write their messages.',
    due: 'No follow-ups due right now.',
  }[_waBucket];
  document.getElementById('wa-panel').innerHTML = `<div class="empty-state"><p>${esc(msg)}</p></div>`;
}

function _waNumberCell(l) {
  return `${esc(prettyWaNumber(l.wa_number) || 'No number')}${l.number_type === 'landline' ? ` ${pill('landline', '', 'Landlines are less likely to have WhatsApp')}` : ''}`;
}

function _renderWaQueue() {
  const heads = {
    review: ['Business', 'Campaign', 'Check found', 'Number'],
    ready:  ['Business', 'Campaign', 'Version', 'Number'],
    due:    ['Business', 'Campaign', 'Last sent', 'Follow-ups'],
  }[_waBucket];
  document.getElementById('wa-queue-head').innerHTML = `<tr>${heads.map(h => `<th>${h}</th>`).join('')}</tr>`;
  const tbody = document.getElementById('wa-queue');
  if (!_waQueue.length) {
    tbody.innerHTML = `<tr><td colspan="4"><div class="empty-state"><p>Nothing here</p></div></td></tr>`;
    return;
  }
  tbody.innerHTML = _waQueue.map(l => {
    const name = `<span class="biz-name">${esc(l.company || 'Unnamed business')}</span>${
      l.website ? `<span class="sub">${esc(l.website.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, ''))}</span>` : ''}`;
    const camp = l.campaign_name ? esc(l.campaign_name) : pill('No campaign', 'amber');
    let c3, c4;
    if (_waBucket === 'review') {
      const tone = { gap_found: 'amber', no_gap: 'green' }[l.signal_type] || '';
      c3 = pill(WA_SIGNAL_LABELS[l.signal_type] || 'Unclear', tone);
      c4 = `<span class="mono" style="font-size:12px">${_waNumberCell(l)}</span>`;
    } else if (_waBucket === 'ready') {
      c3 = l.template_variant ? pill(`Version ${l.template_variant}`) : '<span class="text-muted">—</span>';
      c4 = `<span class="mono" style="font-size:12px">${_waNumberCell(l)}</span>`;
    } else {
      c3 = `<span class="mono" style="font-size:12px">${esc(shortDate(l.sent_date))}</span>`;
      c4 = `<span class="mono">${l.followup_count || 0}</span>`;
    }
    return `<tr class="clickable ${l.id === _waCurrent ? 'current' : ''}" onclick="openWaLead(${l.id})">
      <td>${name}</td><td>${camp}</td><td>${c3}</td><td class="nowrap">${c4}</td></tr>`;
  }).join('');
}

function _waLinks(l) {
  const bits = [];
  if (l.website) bits.push(`<a href="${esc(l.website)}" target="_blank" rel="noopener" style="color:var(--blue)">Open website ↗</a>`);
  bits.push(`<span class="mono">${_waNumberCell(l)}</span>`);
  if (l.rating != null) bits.push(`${esc(l.rating)}★`);
  return `<div class="text-small" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:4px">${bits.join('')}</div>
    <div class="text-muted text-small" style="margin-top:4px">${l.campaign_name
      ? `Campaign: ${esc(l.campaign_name)}` : '<span style="color:var(--amber)">No campaign — move it into one before its message can be written</span>'}</div>`;
}

function _waMoreMenu(l) {
  return `<div class="row-menu">
    <button class="btn btn-ghost btn-sm" onclick="toggleRowMenu(this)" title="More">⋯</button>
    <div class="row-menu-list">
      <button onclick="moveWaLeadsToCampaign([${l.id}])">Move to another campaign…</button>
      <button onclick="openBusiness(${l.business_id})">Open in Contacts</button>
      <button class="danger" onclick="removeWaLeads([${l.id}])">Take off WhatsApp</button>
    </div>
  </div>`;
}

async function openWaLead(id) {
  _waCurrent = id;
  _renderWaQueue();
  const l = _waQueue.find(x => x.id === id);
  if (!l) return;
  const campaign = await _waCampaign(l.wa_campaign_id);
  if (_waCurrent !== id) return;  // another lead was clicked while this loaded
  const panel = document.getElementById('wa-panel');
  const head = `<div class="flex items-center gap-2" style="justify-content:space-between">
      <h3>${esc(l.company || 'Unnamed business')}</h3>
      <span class="text-muted text-small mono">${_waQueue.indexOf(l) + 1} of ${_waQueue.length}</span>
    </div>${_waLinks(l)}`;

  if (_waBucket === 'review') {
    _waSignal = ['gap_found', 'no_gap'].includes(l.signal_type) ? l.signal_type : null;
    panel.innerHTML = `${head}
      <span class="field-label">The automatic check found</span>
      <div class="text-small">${pill(WA_SIGNAL_LABELS[l.signal_type] || 'Unclear',
        { gap_found: 'amber', no_gap: 'green' }[l.signal_type] || '')} <span class="text-muted">${esc(l.signal_detail || '')}</span></div>

      <span class="field-label">Can they be booked online?</span>
      <div class="seg" id="wa-seg">
        <button type="button" data-sig="gap_found" onclick="setWaSignal('gap_found')">No — no online booking</button>
        <button type="button" data-sig="no_gap" onclick="setWaSignal('no_gap')">Yes — they have it</button>
      </div>
      <div class="form-hint" id="wa-seg-hint"></div>

      <span class="field-label">What you saw — this goes into the message</span>
      <div class="chips" id="wa-phrases" style="margin-bottom:8px"></div>
      <input id="wa-detail" class="soft-input" value="${esc(_waSignal ? (l.signal_detail || '') : '')}"
             placeholder="e.g. only a phone number to call" oninput="renderWaPreview()" />

      <span class="field-label">Message preview</span>
      <div class="box" id="wa-preview"></div>
      <div class="form-hint" id="wa-preview-note"></div>

      <div class="flex gap-2" style="flex-wrap:wrap;margin-top:14px;align-items:center">
        <button class="btn btn-primary" onclick="confirmWaSignal(${l.id})">Confirm &amp; next</button>
        <button class="btn btn-ghost" onclick="skipWaLead()">Skip</button>
        <button class="btn btn-ghost" onclick="moveWaLead(${l.id})">Not on WhatsApp…</button>
        <span class="ml-auto">${_waMoreMenu(l)}</span>
      </div>`;
    setWaSignal(_waSignal, { keepText: true });
    return;
  }

  if (_waBucket === 'ready') {
    panel.innerHTML = `${head}
      <span class="field-label">Message${l.template_variant ? ` · version ${esc(l.template_variant)}` : ''}${l.paraphrased ? ' · reworded by AI' : ''}</span>
      <textarea id="wa-msg" class="soft-input" style="min-height:150px"
                onchange="saveWaMessage(${l.id}, this.value)">${esc(l.draft_message || '')}</textarea>
      <div class="form-hint">Edit freely — what's in this box is what opens in WhatsApp. Nothing is sent until you tap Send there.</div>
      <div class="flex gap-2" style="flex-wrap:wrap;margin-top:14px;align-items:center">
        <button class="btn btn-primary" onclick="openWaLink(${l.id}, 'opener')">Open in WhatsApp</button>
        <button class="btn btn-ghost" onclick="skipWaLead()">Skip</button>
        <button class="btn btn-ghost" onclick="moveWaLead(${l.id})">Not on WhatsApp…</button>
        <span class="ml-auto">${_waMoreMenu(l)}</span>
      </div>`;
    return;
  }

  panel.innerHTML = `${head}
    <div class="text-muted text-small" style="margin-top:8px">Last opened ${esc((l.sent_date || '').substring(0, 16))}
      · ${l.followup_count || 0} follow-up${l.followup_count === 1 ? '' : 's'} so far
      · <a style="color:var(--blue);cursor:pointer" onclick="correctWaSentDate(${l.id})">didn't actually send?</a></div>
    <span class="field-label">Follow-up</span>
    <textarea id="wa-msg" class="soft-input" style="min-height:120px">${esc(l.followup_draft || '')}</textarea>
    <div class="form-hint">Follow-ups keep going every ${esc(l.campaign_followup_days || campaign?.followup_days || 3)} days until they reply or you pause.</div>
    <div class="flex gap-2" style="flex-wrap:wrap;margin-top:14px;align-items:center">
      <button class="btn btn-primary" onclick="openWaLink(${l.id}, 'followup')">Open in WhatsApp</button>
      <button class="btn btn-ghost" onclick="waMarkReplied(${l.id})">They replied</button>
      <button class="btn btn-ghost" onclick="waSetPaused([${l.id}], true)">Pause follow-ups</button>
      <button class="btn btn-ghost" onclick="moveWaLead(${l.id})">Not on WhatsApp…</button>
      <span class="ml-auto">${_waMoreMenu(l)}</span>
    </div>`;
}

function setWaSignal(sig, { keepText = false } = {}) {
  _waSignal = sig;
  document.querySelectorAll('#wa-seg button').forEach(b => b.classList.toggle('active', b.dataset.sig === sig));
  const lead = _waQueue.find(x => x.id === _waCurrent) || {};
  const hint = document.getElementById('wa-seg-hint');
  hint.textContent = sig ? '' : (lead.signal_type === 'unclear'
    ? "The check couldn't tell from their site — open the website and pick one."
    : 'Pick one.');
  document.getElementById('wa-phrases').innerHTML = (WA_DETAIL_PHRASES[sig] || []).map(p =>
    `<button type="button" class="chip" onclick="useWaPhrase('${escj(p)}')">${esc(p)}</button>`).join('');
  if (!keepText) document.getElementById('wa-detail').value = '';
  renderWaPreview();
}

function useWaPhrase(p) {
  document.getElementById('wa-detail').value = p;
  renderWaPreview();
}

async function renderWaPreview() {
  const lead = _waQueue.find(x => x.id === _waCurrent);
  const box = document.getElementById('wa-preview');
  const note = document.getElementById('wa-preview-note');
  if (!lead || !box) return;
  const campaign = await _waCampaign(lead.wa_campaign_id);
  if (!campaign) { box.textContent = 'No campaign, so no template to write from.'; note.textContent = ''; return; }
  if (!_waSignal) { box.innerHTML = '<span class="text-muted">Pick yes or no above to see the message.</span>'; note.textContent = ''; return; }
  const arms = campaign.templates[_waSignal === 'gap_found' ? 'gap' : 'no_gap'] || [];
  const detail = document.getElementById('wa-detail').value;
  box.innerHTML = fillPlaceholders(arms[0] || '', waFieldsFor({ ...lead, signal_detail: detail }, campaign.variables),
                                   { highlight: true });
  note.textContent = arms.length > 1
    ? `Showing version A of ${arms.length} — the lead gets whichever version is next in rotation.`
    : '';
}

function _advanceWa(doneId) {
  const idx = _waQueue.findIndex(l => l.id === doneId);
  _waQueue = _waQueue.filter(l => l.id !== doneId);
  const next = _waQueue[idx] || _waQueue[idx - 1] || null;
  _waCurrent = next ? next.id : null;
  _renderWaQueue();
  if (next) openWaLead(next.id); else _renderWaEmptyPanel();
  refreshWaCounts();
}

function skipWaLead() {
  const idx = _waQueue.findIndex(l => l.id === _waCurrent);
  const next = _waQueue[idx + 1] || _waQueue[0];
  if (next && next.id !== _waCurrent) openWaLead(next.id);
}

async function confirmWaSignal(id) {
  if (!_waSignal) { toast('Pick whether they can book online first', 'err'); return; }
  const signal_detail = document.getElementById('wa-detail').value.trim();
  if (!signal_detail) { toast('Add what you saw — it goes into the message', 'err'); return; }
  const res = await api(`/api/wa/leads/${id}/confirm`, 'POST', { signal_type: _waSignal, signal_detail });
  if (!res || res.error) { toast((res && res.error) || 'Could not confirm', 'err'); return; }
  toast('Confirmed');
  _advanceWa(id);
}

async function runWaDraftBatch() {
  const btn = document.getElementById('wa-draft-batch-btn');
  btn.disabled = true;
  try {
    const res = await api('/api/wa/draft-batch', 'POST', { wa_campaign_id: _waFilter() || null });
    if (!res || res.error) { toast((res && res.error) || 'Could not write the messages', 'err'); return; }
    const note = res.note ? ` — ${res.note}` : '';
    toast(`Wrote ${res.drafted} message${res.drafted === 1 ? '' : 's'}${note}`);
    if (res.drafted) setWaBucket('ready'); else loadWaTodo();
  } finally {
    btn.disabled = false;
  }
}

async function saveWaMessage(id, message) {
  if (!message.trim()) { toast("The message can't be empty", 'err'); return; }
  const res = await api(`/api/wa/leads/${id}/message`, 'PUT', { message });
  if (!res || res.error) { toast((res && res.error) || 'Could not save the edit', 'err'); return; }
  const l = _waQueue.find(x => x.id === id);
  if (l) l.draft_message = message;
}

// Opens the wa.me link with whatever is currently in the message box -- so an
// edit made just before clicking is what actually gets sent, not whatever was
// drafted originally. Marks it sent immediately after the tap; see
// mark_wa_sent's own comment on why that's an approximation, not proof.
function openWaLink(id, kind) {
  const lead = _waQueue.find(l => l.id === id);
  if (!lead || !lead.wa_number) { toast('No WhatsApp number on file for this lead', 'err'); return; }
  const message = document.getElementById('wa-msg').value;
  const url = `https://wa.me/${lead.wa_number}?text=${encodeURIComponent(message)}`;
  window.open(url, '_blank', 'noopener');
  api(`/api/wa/leads/${id}/sent`, 'POST', { kind, message }).then(res => {
    if (!res || res.error) { toast((res && res.error) || 'Opened, but could not record it', 'err'); return; }
    _advanceWa(id);
  });
}

async function correctWaSentDate(id) {
  const value = await chooseDialog({
    title: "Didn't actually send it?",
    body: `<p class="text-small" style="line-height:1.6;margin-bottom:10px">Opening WhatsApp is recorded as sent, since
      there's no way to see what happened inside WhatsApp. Put in the real time, or clear it.</p>
      ${choiceCard({ name: 'wa-sd', value: 'clear', title: "I didn't send it", hint: 'Clears the date. It stops counting towards follow-ups until you send again.', checked: true })}
      ${choiceCard({ name: 'wa-sd', value: 'set', title: 'I sent it at a different time',
        extra: '<input type="datetime-local" id="wa-sd-at" class="soft-input" style="margin-top:8px" onclick="event.stopPropagation()" />' })}`,
    confirm: 'Save',
    collect: () => {
      if (chosenRadio('wa-sd') === 'clear') return { sent_date: null };
      const at = document.getElementById('wa-sd-at').value;
      if (!at) { toast('Pick the date and time', 'err'); return null; }
      return { sent_date: at };
    },
  });
  if (!value) return;
  const res = await api(`/api/wa/leads/${id}/sent-date`, 'PUT', value);
  if (!res || res.error) { toast((res && res.error) || 'Could not update', 'err'); return; }
  toast(value.sent_date ? 'Sent date updated' : 'Sent date cleared');
  _reloadWaView();
}

async function waMarkReplied(id, replied = true) {
  const res = await api(`/api/wa/leads/${id}/replied`, 'POST', { replied });
  if (!res || res.error) { toast((res && res.error) || 'Could not update', 'err'); return; }
  toast(replied ? 'Marked replied — no more follow-ups' : 'Back to waiting for a reply');
  if (currentTab('whatsapp', 'todo') === 'todo' && replied) _advanceWa(id); else _reloadWaView();
}

async function waSetPaused(ids, paused) {
  const res = await api('/api/wa/leads/bulk', 'POST', { action: paused ? 'pause' : 'resume', wa_lead_ids: ids });
  if (!res || res.error) { toast((res && res.error) || 'Could not update', 'err'); return; }
  toast(paused ? `Paused ${res.updated} — no follow-ups until resumed` : `Resumed ${res.updated}`);
  if (currentTab('whatsapp', 'todo') === 'todo' && paused && ids.length === 1) _advanceWa(ids[0]);
  else _reloadWaView();
}

function _reloadWaView() {
  const tab = currentTab('whatsapp', 'todo');
  if (tab === 'todo') loadWaTodo();
  if (tab === 'leads') { LT.wl.clear(); LT.wl.load(); }
  if (tab === 'campaigns') renderWaCampaigns();
}

// "Not on WhatsApp": a proper choice of where the lead goes next, instead of
// typing "call" or "email" into a prompt.
async function moveWaLead(id) {
  const lead = _waQueue.find(l => l.id === id) || (LT.wl && LT.wl.rowById(id));
  if (!lead) return;
  const [biz, callCamps, emailCamps] = await Promise.all([
    api(`/api/businesses/${lead.business_id}`), api('/api/call-campaigns'), api('/api/campaigns'),
  ]);
  const emails = (biz && biz.emails || []).filter(e => e.status === 'active');
  const hasPhone = !!(biz && biz.phone);
  const choice = await chooseDialog({
    title: `${lead.company || 'This business'} isn't on WhatsApp`,
    width: 520,
    body: `<p class="text-muted text-small" style="margin-bottom:12px">It comes off WhatsApp for good, so a later scrape can't put the number back. Where should it go?</p>
      ${choiceCard({ name: 'wa-mv', value: 'call', title: 'Move to Calling', checked: hasPhone, disabled: !hasPhone,
        hint: hasPhone ? 'Onto your call list, and into a campaign if you pick one.' : 'No phone number on file to call.',
        extra: hasPhone ? `<div onclick="event.stopPropagation()" style="margin-top:8px">${campaignSelectHtml('wa-mv-call', callCamps || [],
          { allowNone: true, noneLabel: 'No campaign — just add to Calling' })}</div>` : '' })}
      ${choiceCard({ name: 'wa-mv', value: 'email', title: 'Move to Email', disabled: !emails.length,
        hint: emails.length ? `Uses ${esc(emails[0].email)}.` : 'No email address on file.',
        extra: emails.length ? `<div onclick="event.stopPropagation()" style="margin-top:8px">${campaignSelectHtml('wa-mv-email', emailCamps || [],
          { allowNone: true, noneLabel: "Don't enroll yet", allowNew: false })}</div>` : '' })}
      ${choiceCard({ name: 'wa-mv', value: 'none', title: 'Just take it off WhatsApp', checked: !hasPhone,
        hint: "It stays in Contacts, under Unassigned, until you decide." })}`,
    confirm: 'Move',
    collect: () => chosenRadio('wa-mv'),
  });
  if (!choice) return;
  let campaignId = null;
  if (choice === 'call') {
    campaignId = await resolveCampaignSelect('wa-mv-call', '/api/call-campaigns');
    if (campaignId === null) return;
  } else if (choice === 'email') {
    campaignId = document.getElementById('wa-mv-email').value;
  }
  const res = await api(`/api/wa/leads/${id}/move`, 'POST', { destination: choice, campaign_id: campaignId || null });
  if (!res || res.error) { toast((res && res.error) || 'Could not move it', 'err'); return; }
  toast({ call: 'Moved to Calling', email: res.enrolled ? 'Moved to Email and enrolled' : 'Moved to Email',
          none: 'Taken off WhatsApp — find it in Contacts → Unassigned' }[choice]);
  if (currentTab('whatsapp', 'todo') === 'todo' && _waQueue.some(l => l.id === id)) _advanceWa(id);
  else _reloadWaView();
}

async function removeWaLeads(ids) {
  if (!ids.length) return;
  const ok = await chooseDialog({
    title: `Take ${ids.length} off WhatsApp?`,
    body: `<p class="text-small" style="line-height:1.6">They drop out of every WhatsApp list. Their message history is kept,
      they stay in Contacts, and you can add them back any time. (If the number just isn't on WhatsApp,
      use "Not on WhatsApp" instead, so it's never re-added.)</p>`,
    confirm: 'Take off WhatsApp', danger: true,
  });
  if (!ok) return;
  const res = await api('/api/wa/leads/bulk', 'POST', { action: 'remove', wa_lead_ids: ids });
  if (!res || res.error) { toast((res && res.error) || 'Could not remove them', 'err'); return; }
  toast(`Took ${res.updated} off WhatsApp`);
  if (currentTab('whatsapp', 'todo') === 'todo' && ids.length === 1 && _waQueue.some(l => l.id === ids[0])) _advanceWa(ids[0]);
  else _reloadWaView();
}

async function moveWaLeadsToCampaign(ids) {
  if (!ids.length) return;
  await loadWaCampaigns();
  const ok = await chooseDialog({
    title: `Move ${ids.length} to a campaign`,
    body: `<label class="field-label">Campaign</label>${campaignSelectHtml('wa-mc', _waCampaigns)}
      <div class="form-hint">Messages already sent stay as they were; anything written from now on uses this campaign's templates and follow-up gap.</div>`,
    confirm: 'Move',
    collect: () => document.getElementById('wa-mc').value || null,
  });
  if (!ok) return;
  const cid = await resolveCampaignSelect('wa-mc', '/api/wa/campaigns');
  if (!cid) return;
  const res = await api('/api/wa/leads/bulk', 'POST', { action: 'campaign', wa_lead_ids: ids, wa_campaign_id: cid });
  if (!res || res.error) { toast((res && res.error) || 'Could not move them', 'err'); return; }
  toast(`Moved ${res.updated}`);
  await loadWaCampaigns();
  _reloadWaView();
}

// ── Leads table ──────────────────────────────────────────────────────────────

createLeadTable({
  id: 'wl',
  url: '/api/wa/leads/page',
  empty: 'No WhatsApp leads match. Add some with + Add leads, from Contacts, or scrape with WhatsApp as the destination.',
  params: () => ({
    q: document.getElementById('wl-search')?.value.trim(),
    stage: document.getElementById('wl-stage')?.value,
    wa_campaign_id: _waFilter(),
  }),
  columns: [
    { key: 'company', label: 'Business', sort: true,
      render: r => `<span class="biz-name">${esc(r.company || 'Unnamed business')}</span><span class="sub mono">${esc(prettyWaNumber(r.wa_number) || 'no number')}</span>` },
    { key: 'campaign_name', label: 'Campaign', sort: true,
      render: r => r.campaign_name ? esc(r.campaign_name) : pill('No campaign', 'amber') },
    { key: 'stage', label: 'Stage', sort: true, render: r => waStagePill(r.stage) },
    { key: 'template_variant', label: 'Version', sort: true, cls: 'num', render: r => esc(r.template_variant || '—') },
    { key: 'sent_date', label: 'Last sent', sort: true, cls: 'num', render: r => esc(shortDate(r.sent_date)) },
    { key: 'followup_count', label: 'Follow-ups', sort: true, cls: 'num', render: r => r.followup_count || 0 },
    { key: 'created_at', label: 'Added', sort: true, cls: 'num', render: r => esc(shortDate(r.created_at)) },
  ],
  onRowClick: r => openWaLeadFromTable(r),
  bulk: () => `
    <button class="btn btn-ghost btn-sm" onclick="moveWaLeadsToCampaign(LT.wl.selectedIds())">Move to campaign</button>
    <button class="btn btn-ghost btn-sm" onclick="waSetPaused(LT.wl.selectedIds(), true)">Pause follow-ups</button>
    <button class="btn btn-ghost btn-sm" onclick="waSetPaused(LT.wl.selectedIds(), false)">Resume</button>
    <button class="btn btn-danger btn-sm" onclick="removeWaLeads(LT.wl.selectedIds())">Take off WhatsApp</button>`,
  menu: r => {
    const on = !['moved', 'removed'].includes(r.stage);
    return [
      on && ['review', 'ready', 'due'].includes(r.stage) && { label: 'Open in To do', run: `openWaLeadFromTable(LT.wl.rowById(${r.id}))` },
      on && { label: 'Move to another campaign…', run: `moveWaLeadsToCampaign([${r.id}])` },
      on && r.sent_date && !r.replied && { label: r.paused ? 'Resume follow-ups' : 'Pause follow-ups', run: `waSetPaused([${r.id}], ${!r.paused})` },
      on && ['waiting', 'due', 'paused'].includes(r.stage) && { label: 'They replied', run: `waMarkReplied(${r.id}, true)` },
      r.stage === 'replied' && { label: "Undo 'replied'", run: `waMarkReplied(${r.id}, false)` },
      on && r.sent_date && { label: "Didn't actually send?", run: `correctWaSentDate(${r.id})` },
      on && { label: 'Not on WhatsApp…', run: `moveWaLead(${r.id})` },
      { label: 'Open in Contacts', run: `openBusiness(${r.business_id})` },
      on && { label: 'Take off WhatsApp', run: `removeWaLeads([${r.id}])`, danger: true },
    ];
  },
  onLoad: data => { if (!document.getElementById('wl-stage').value) setTabCount('whatsapp', 'leads', data.total); },
});

function openWaLeadFromTable(r) {
  if (!r) return;
  const bucket = { review: 'review', ready: 'ready', due: 'due' }[r.stage];
  if (!bucket) { openBusiness(r.business_id); return; }
  _waBucket = bucket;
  _waCurrent = r.id;
  setTab('whatsapp', 'todo');
}

// ── Campaigns ────────────────────────────────────────────────────────────────

async function renderWaCampaigns() {
  await loadWaCampaigns();
  const tbody = document.getElementById('wa-campaign-list');
  if (!_waCampaigns.length) {
    tbody.innerHTML = `<tr><td colspan="9"><div class="empty-state"><p>No WhatsApp campaigns yet.
      Create one — it holds the message templates its leads are written from.</p></div></td></tr>`;
    return;
  }
  const countryName = code => (WA_COUNTRIES.find(c => c[0] === code) || [code, ''])[1];
  tbody.innerHTML = _waCampaigns.map(c => `
    <tr class="${c.status === 'archived' ? 'text-muted' : ''}">
      <td><span class="biz-name">${esc(c.name)}</span>${c.status === 'archived' ? ' ' + pill('archived') : ''}
        <span class="sub">${esc([countryName(c.country), c.notes].filter(Boolean).join(' · '))}</span></td>
      <td class="num">${c.leads}</td>
      <td class="num">${c.to_review ? `<span style="color:var(--amber)">${c.to_review}</span>` : 0}</td>
      <td class="num">${c.ready}</td>
      <td class="num">${c.due ? `<span style="color:var(--amber)">${c.due}</span>` : 0}</td>
      <td class="num">${c.messaged}</td>
      <td class="num">${c.replied} <span class="text-muted">(${c.reply_rate}%)</span></td>
      <td class="num">${c.followup_days} days</td>
      <td class="nowrap">
        <button class="btn btn-ghost btn-sm" onclick="openWaTemplates(${c.id})">Templates</button>
        <button class="btn btn-ghost btn-sm" onclick="openWhatsAppCampaign(${c.id})">Leads</button>
        <div class="row-menu">
          <button class="btn btn-ghost btn-sm" onclick="toggleRowMenu(this)">⋯</button>
          <div class="row-menu-list">
            <button onclick="openWaImportModal(${c.id})">Add leads…</button>
            <button onclick="openWaTemplates(${c.id})">Rename or edit…</button>
            <button onclick="setWaCampaignStatus(${c.id}, '${c.status === 'archived' ? 'active' : 'archived'}')">${c.status === 'archived' ? 'Unarchive' : 'Archive'}</button>
            <button class="danger" onclick="deleteWaCampaign(${c.id})">Delete campaign</button>
          </div>
        </div>
      </td>
    </tr>`).join('');
}

function openWaTemplates(id) {
  setTab('whatsapp', 'templates', { load: false });
  renderWaTemplateEditor(id);
}

async function openNewWaCampaign() {
  await loadWaCampaigns();
  const values = await chooseDialog({
    title: 'New WhatsApp campaign',
    body: `<label class="field-label">Name</label>
      <input id="nwc-name" class="soft-input" placeholder="Dubai dental — online booking" />
      <label class="field-label">Country</label>
      <select id="nwc-country" class="filter-select" style="width:100%;max-width:100%">
        ${WA_COUNTRIES.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}
      </select>
      <label class="field-label">Start its messages from</label>
      <select id="nwc-copy" class="filter-select" style="width:100%;max-width:100%">
        <option value="">The starter templates</option>
        ${_waCampaigns.map(c => `<option value="${c.id}">A copy of “${esc(c.name)}”</option>`).join('')}
      </select>`,
    confirm: 'Create',
    collect: () => {
      const name = document.getElementById('nwc-name').value.trim();
      if (!name) { toast('Give it a name', 'err'); return null; }
      return { name, country: document.getElementById('nwc-country').value,
               copy_from: document.getElementById('nwc-copy').value || null };
    },
  });
  if (!values) return;
  const res = await api('/api/wa/campaigns', 'POST', values);
  if (!res || res.error) { toast((res && res.error) || 'Could not create it', 'err'); return; }
  toast('Campaign created — now make its messages your own');
  await loadWaCampaigns();
  openWaTemplates(res.id);
}

async function setWaCampaignStatus(id, status) {
  const res = await api(`/api/wa/campaigns/${id}`, 'PATCH', { status });
  if (!res || res.error) { toast((res && res.error) || 'Could not update', 'err'); return; }
  renderWaCampaigns();
}

async function deleteWaCampaign(id) {
  const c = _waCampaigns.find(x => x.id === id);
  const ok = await chooseDialog({
    title: `Delete "${c ? c.name : 'this campaign'}"?`,
    body: `<p class="text-small" style="line-height:1.6">Its templates go. Its ${c ? c.leads : 0} leads stay on WhatsApp with
      no campaign — move them into another before their messages can be written.</p>`,
    confirm: 'Delete campaign', danger: true,
  });
  if (!ok) return;
  const res = await api(`/api/wa/campaigns/${id}`, 'DELETE');
  if (!res || res.error) { toast((res && res.error) || 'Could not delete', 'err'); return; }
  delete _waCampaignDetail[id];
  toast('Campaign deleted — leads kept');
  renderWaCampaigns();
}

// ── Templates ────────────────────────────────────────────────────────────────

const WA_ARM_LABELS = ['A', 'B', 'C', 'D'];

const WA_TEMPLATE_KINDS = [
  ['gap',      'Opener — no online booking found', 'For leads you confirmed have no way to book online.'],
  ['no_gap',   'Opener — they already have online booking', 'For leads you confirmed can already be booked online.'],
  ['followup', 'Follow-up', 'Sent every few days until they reply or you pause. Leave {{signal_detail}} out of this one.'],
];

const WA_SAMPLE_LEAD = { company: 'Pearl Dental Clinic', city: 'Dubai', category: 'Dentist', rating: 4.8,
                         review_count: 126, website: 'https://pearldental.ae', signal_detail: 'only a phone number to call' };

let _waEdit = null;         // the campaign being edited, with unsaved edits
let _waEditSample = null;   // a real lead from it, when it has one, for previews
let _waLastFocus = null;    // where a placeholder button inserts

async function renderWaTemplateEditor(id) {
  const wrap = document.getElementById('wa-template-editor');
  if (!_waCampaigns.length) await loadWaCampaigns();
  if (!id) {
    wrap.innerHTML = `<div class="card"><div class="empty-state"><p>Templates belong to a campaign. Create one first.</p>
      <button class="btn btn-primary" style="margin-top:12px" onclick="openNewWaCampaign()">+ New campaign</button></div></div>`;
    return;
  }
  const c = await api(`/api/wa/campaigns/${id}`);
  if (!c || c.error) { toast('Could not load that campaign', 'err'); return; }
  _waEdit = JSON.parse(JSON.stringify(c));
  const leads = await api(`/api/wa/leads?wa_campaign_id=${id}&limit=1`) || [];
  _waEditSample = leads[0] ? { ...leads[0], signal_detail: leads[0].signal_detail || WA_SAMPLE_LEAD.signal_detail } : null;
  _drawWaTemplateEditor();
}

function _readWaTemplateEditor() {
  if (!_waEdit) return;
  const val = id => document.getElementById(id);
  if (val('wt-name')) _waEdit.name = val('wt-name').value;
  if (val('wt-country')) _waEdit.country = val('wt-country').value;
  if (val('wt-followup')) _waEdit.followup_days = parseInt(val('wt-followup').value, 10) || 1;
  if (val('wt-notes')) _waEdit.notes = val('wt-notes').value;
  WA_TEMPLATE_KINDS.forEach(([kind]) => {
    _waEdit.templates[kind] = (_waEdit.templates[kind] || ['']).map(
      (arm, i) => val(`wt-${kind}-${i}`)?.value ?? arm);
  });
  const vars = {};
  document.querySelectorAll('#wt-vars .wt-var').forEach(row => {
    const k = row.querySelector('.wt-var-key').value.trim().replace(/\s+/g, '_');
    if (k) vars[k] = row.querySelector('.wt-var-val').value;
  });
  if (document.getElementById('wt-vars')) _waEdit.variables = vars;
}

function _waStatsLine(label) {
  const rows = (_waEdit.stats || []).filter(s => s.arm === label);
  if (!rows.length) return '';
  return rows.map(r => `${r.sent} sent ${r.paraphrased ? 'AI-reworded' : 'as written'} · ${r.replied} replied (${r.reply_rate}%)`).join('  |  ');
}

function _drawWaTemplateEditor() {
  const c = _waEdit;
  const wrap = document.getElementById('wa-template-editor');
  const coverage = Object.fromEntries(((c.coverage || {}).fields || []).map(f => [f.key, f]));
  const total = (c.coverage || {}).total || 0;

  const fieldButtons = (c.fields || []).map(f => {
    const cov = coverage[f.key];
    const gap = total && cov && cov.filled < total ? `<span class="gap">${cov.filled}/${total}</span>` : '';
    return `<button type="button" title="${esc(f.label)}" onclick="insertWaPlaceholder('${f.key}')">{{${esc(f.key)}}}${gap}</button>`;
  }).join('') + Object.keys(c.variables || {}).map(k =>
    `<button type="button" title="Your variable" onclick="insertWaPlaceholder('${escj(k)}')">{{${esc(k)}}}</button>`).join('');

  const kinds = WA_TEMPLATE_KINDS.map(([kind, label, hint]) => {
    const arms = (c.templates[kind] && c.templates[kind].length) ? c.templates[kind] : [''];
    const testing = arms.length > 1;
    return `<div style="margin-top:22px">
      <div class="card-title">${esc(label)}</div>
      <div class="text-muted text-small" style="margin:2px 0 8px">${esc(hint)}</div>
      ${arms.map((arm, i) => `
        <div style="margin-bottom:12px">
          ${testing ? `<div class="flex items-center" style="margin-bottom:4px;gap:8px">
            ${pill(`Version ${WA_ARM_LABELS[i]}`, 'blue')}
            <span class="text-muted text-small">${esc(_waStatsLine(WA_ARM_LABELS[i]))}</span>
            <button class="btn btn-ghost btn-sm ml-auto" onclick="removeWaArm('${kind}', ${i})">Remove</button>
          </div>` : ''}
          <textarea id="wt-${kind}-${i}" class="soft-input" style="min-height:${kind === 'followup' ? 70 : 96}px"
                    onfocus="_waLastFocus=this" oninput="updateWaTemplatePreview('${kind}', ${i})">${esc(arm)}</textarea>
          <div class="box" id="wt-${kind}-${i}-preview" style="margin-top:6px;font-size:12.5px;background:transparent"></div>
        </div>`).join('')}
      <button class="btn btn-ghost btn-sm" onclick="addWaArm('${kind}')">${testing ? '+ Add another version' : '+ Test a second version'}</button>
    </div>`;
  }).join('');

  const untested = _waStatsLine('-');

  wrap.innerHTML = `
    <div class="card" style="padding:20px">
      <div class="flex gap-2 items-center" style="flex-wrap:wrap;margin-bottom:14px">
        <label class="text-muted text-small">Campaign</label>
        <select class="filter-select" onchange="switchWaTemplateCampaign(this.value)">
          ${_waCampaigns.map(x => `<option value="${x.id}" ${x.id === c.id ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}
        </select>
        <button class="btn btn-ghost btn-sm" onclick="openNewWaCampaign()">+ New campaign</button>
        <button class="btn btn-primary ml-auto" onclick="saveWaTemplates()">Save</button>
      </div>

      <div class="responsive-grid-3" style="gap:12px">
        <div><span class="field-label">Name</span><input id="wt-name" class="soft-input" value="${esc(c.name)}" /></div>
        <div><span class="field-label">Country</span>
          <select id="wt-country" class="soft-input">
            <option value="">—</option>
            ${WA_COUNTRIES.map(([v, l]) => `<option value="${v}" ${c.country === v ? 'selected' : ''}>${l}</option>`).join('')}
          </select></div>
        <div><span class="field-label">Follow up every</span>
          <div class="flex items-center gap-2"><input id="wt-followup" type="number" min="1" max="365" class="soft-input"
            style="max-width:90px" value="${esc(c.followup_days)}" /> <span class="text-muted text-small">days, until they reply or you pause</span></div></div>
      </div>

      <span class="field-label">Placeholders — click to insert where you're typing</span>
      <div class="placeholder-list">${fieldButtons}</div>
      <div class="text-muted text-small">Add a fallback for anything that might be missing: <span class="mono">{{city|your area}}</span>.
        ${total ? `An amber count means some of this campaign's ${total} leads don't have that detail.` : ''}
        Previews below use ${_waEditSample ? `a real lead from this campaign (${esc(_waEditSample.company)})` : 'a made-up clinic'}.</div>

      ${kinds}
      ${untested ? `<div class="text-muted text-small" style="margin-top:12px">Sent before you started testing versions: ${esc(untested)}</div>` : ''}

      <div style="margin-top:24px">
        <div class="card-title">Your variables</div>
        <div class="text-muted text-small" style="margin:2px 0 8px">Words you reuse across this campaign's messages —
          your name, the service you're offering. Use them as <span class="mono">{{name}}</span>.</div>
        <div id="wt-vars" style="display:flex;flex-direction:column;gap:6px"></div>
        <button class="btn btn-ghost btn-sm" style="margin-top:8px" onclick="addWaVariable()">+ Add variable</button>
      </div>

      <div style="margin-top:22px">
        <span class="field-label">Notes</span>
        <input id="wt-notes" class="soft-input" value="${esc(c.notes || '')}" placeholder="What this campaign is pitching" />
      </div>

      <div class="flex gap-2" style="margin-top:20px">
        <button class="btn btn-primary" onclick="saveWaTemplates()">Save</button>
      </div>
    </div>`;

  Object.entries(c.variables || {}).forEach(([k, v]) => addWaVariable(k, v, false));
  _refreshAllWaPreviews();
}

function updateWaTemplatePreview(kind, i) {
  const ta = document.getElementById(`wt-${kind}-${i}`);
  const box = document.getElementById(`wt-${kind}-${i}-preview`);
  if (!ta || !box) return;
  _readWaVariablesOnly();
  const sample = { ...(_waEditSample || WA_SAMPLE_LEAD) };
  if (kind === 'no_gap' && !_waEditSample) sample.signal_detail = 'an online booking button';
  box.innerHTML = fillPlaceholders(ta.value, waFieldsFor(sample, _waEdit.variables || {}), { highlight: true });
}

function _readWaVariablesOnly() {
  if (!document.getElementById('wt-vars')) return;
  const vars = {};
  document.querySelectorAll('#wt-vars .wt-var').forEach(row => {
    const k = row.querySelector('.wt-var-key').value.trim().replace(/\s+/g, '_');
    if (k) vars[k] = row.querySelector('.wt-var-val').value;
  });
  _waEdit.variables = vars;
}

function _refreshAllWaPreviews() {
  WA_TEMPLATE_KINDS.forEach(([kind]) => (_waEdit.templates[kind] || ['']).forEach((_, i) => updateWaTemplatePreview(kind, i)));
}

function addWaVariable(key = '', value = '', focus = true) {
  const row = document.createElement('div');
  row.className = 'wt-var';
  row.style.cssText = 'display:flex;gap:6px;align-items:center';
  row.innerHTML = `
    <input class="wt-var-key soft-input mono" placeholder="my_name" value="${esc(key)}" style="flex:1;font-size:12.5px"
           oninput="_refreshAllWaPreviews()" />
    <input class="wt-var-val soft-input" placeholder="Sam" value="${esc(value)}" style="flex:2" oninput="_refreshAllWaPreviews()" />
    <button class="btn btn-ghost btn-sm" onclick="this.closest('.wt-var').remove();_refreshAllWaPreviews()" title="Remove">✕</button>`;
  document.getElementById('wt-vars').appendChild(row);
  if (focus) row.querySelector('.wt-var-key').focus();
}

function insertWaPlaceholder(key) {
  const target = _waLastFocus && document.body.contains(_waLastFocus) ? _waLastFocus : document.getElementById('wt-gap-0');
  insertAtCursor(target, `{{${key}}}`);
}

async function switchWaTemplateCampaign(id) {
  renderWaTemplateEditor(id);
}

function addWaArm(kind) {
  _readWaTemplateEditor();
  const max = _waEdit.max_arms || 4;
  if ((_waEdit.templates[kind] || []).length >= max) { toast(`${max} versions is the limit`, 'err'); return; }
  _waEdit.templates[kind] = [...(_waEdit.templates[kind] || []), ''];
  _drawWaTemplateEditor();
}

function removeWaArm(kind, idx) {
  _readWaTemplateEditor();
  const arms = [...(_waEdit.templates[kind] || [])];
  if (arms.length <= 1) return;
  // Leads already sent keep the label they were drafted under, so removing a
  // version stops it being used from now on without rewriting what happened.
  if (!confirm(`Remove version ${WA_ARM_LABELS[idx]}? Messages already sent with it keep their results.`)) return;
  arms.splice(idx, 1);
  _waEdit.templates[kind] = arms;
  _drawWaTemplateEditor();
}

async function saveWaTemplates() {
  _readWaTemplateEditor();
  const templates = {};
  for (const [kind, label] of WA_TEMPLATE_KINDS) {
    const arms = (_waEdit.templates[kind] || []).map(a => (a || '').trim()).filter(Boolean);
    if (!arms.length) { toast(`"${label}" needs at least one message`, 'err'); return; }
    templates[kind] = arms;
  }
  const res = await api(`/api/wa/campaigns/${_waEdit.id}`, 'PATCH', {
    name: _waEdit.name, country: _waEdit.country, followup_days: _waEdit.followup_days,
    notes: _waEdit.notes, templates, variables: _waEdit.variables,
  });
  if (!res || res.error) { toast((res && res.error) || 'Could not save', 'err'); return; }
  delete _waCampaignDetail[_waEdit.id];
  toast('Saved ✓');
  await loadWaCampaigns();
  renderWaTemplateEditor(_waEdit.id);
}

// ── Adding leads ─────────────────────────────────────────────────────────────

// One "Add leads" door with the same routes in as Calling: pick from leads you
// already have, bring a CSV or paste, or go and scrape new ones. Every route
// needs a campaign, because that's where the messages are written from.
let _waAddTab = 'existing';
let _waAddSelected = new Set();
let _waAddRows = [];
let _waAddTotal = 0;
let _waAddTimer = null;

async function openWaImportModal(campaignId = '') {
  await loadWaCampaigns();
  const active = _waCampaigns.filter(c => c.status !== 'archived');
  const selected = campaignId || _waFilter() || (active[0] && active[0].id) || '';
  document.getElementById('wa-add-campaign-wrap').innerHTML =
    campaignSelectHtml('wa-add-campaign', active, { selected });
  const sel = document.getElementById('wa-add-campaign');
  sel.addEventListener('change', _waAddCampaignChanged);
  if (!active.length) sel.value = '__new';
  sel.dispatchEvent(new Event('change'));
  _waAddSelected.clear();
  const search = document.getElementById('wa-add-search');
  if (search) search.value = '';
  openModal('modal-import-wa');
  setWaAddTab('existing');
}

function _waAddCampaignChanged() {
  const c = _waCampaigns.find(x => String(x.id) === document.getElementById('wa-add-campaign').value);
  if (c && c.country) document.getElementById('wa-import-country').value = c.country;
}

function setWaAddTab(tab) {
  _waAddTab = tab;
  ['existing', 'import'].forEach(t => {
    const btn = document.getElementById(`wa-add-tab-${t}`);
    if (btn) {
      btn.classList.toggle('btn-primary', t === tab);
      btn.classList.toggle('btn-ghost', t !== tab);
    }
    const pane = document.getElementById(`wa-add-pane-${t}`);
    if (pane) pane.style.display = t === tab ? 'block' : 'none';
  });
  const submit = document.getElementById('wa-add-submit');
  if (submit) submit.textContent = tab === 'existing' ? 'Add to WhatsApp' : 'Import';
  if (tab === 'existing') waAddSearch();
}

function waAddSearch() {
  clearTimeout(_waAddTimer);
  _waAddTimer = setTimeout(_waAddFetch, 250);
}

async function _waAddFetch() {
  const p = new URLSearchParams({ per_page: 100 });
  const q = document.getElementById('wa-add-search').value.trim();
  if (q) p.set('q', q);
  const status = document.getElementById('wa-add-status').value;
  if (status) p.set('status', status);
  if (document.getElementById('wa-add-new-only').checked) p.set('not_on', 'whatsapp');
  const data = await api('/api/businesses/search?' + p.toString());
  _waAddRows = (data && data.rows) || [];
  _waAddTotal = data ? data.total : 0;
  _waAddRender();
}

function _waAddRender() {
  const tbody = document.getElementById('wa-add-table');
  document.getElementById('wa-add-count').textContent =
    `${_waAddSelected.size} selected · showing ${_waAddRows.length} of ${_waAddTotal}`;
  if (!_waAddRows.length) {
    tbody.innerHTML = '<tr><td colspan="3"><div class="empty-state"><p>No leads match</p></div></td></tr>';
    return;
  }
  tbody.innerHTML = _waAddRows.map(c => `
    <tr style="cursor:pointer" onclick="waAddToggle(${c.id})">
      <td><input type="checkbox" ${_waAddSelected.has(c.id) ? 'checked' : ''}
                 onclick="event.stopPropagation();waAddToggle(${c.id})" style="cursor:pointer" /></td>
      <td>${esc(c.company || '—')}${contactSignalPill(c)}</td>
      <td class="mono" style="font-size:12px">${c.phone ? esc(c.phone) : '<span class="text-muted">no phone</span>'}</td>
    </tr>`).join('');
}

function waAddToggle(id) {
  if (_waAddSelected.has(id)) _waAddSelected.delete(id); else _waAddSelected.add(id);
  _waAddRender();
}

function waAddSelectAllShown() {
  _waAddRows.forEach(c => _waAddSelected.add(c.id));
  _waAddRender();
}

function waAddClearSelection() {
  _waAddSelected.clear();
  _waAddRender();
}

async function _waAddCampaignId() {
  const country = document.getElementById('wa-import-country').value;
  const id = await resolveCampaignSelect('wa-add-campaign', '/api/wa/campaigns', { country });
  if (!id) { if (id === '') toast('Pick the campaign these leads go into', 'err'); return null; }
  return id;
}

async function submitWaAdd() {
  if (_waAddTab === 'existing' && !_waAddSelected.size) { toast('Select at least one lead', 'err'); return; }
  const campaignId = await _waAddCampaignId();
  if (!campaignId) return;
  if (_waAddTab === 'import') return importWaLeads(campaignId);

  const country = document.getElementById('wa-import-country').value;
  const body = { business_ids: [..._waAddSelected], country, wa_campaign_id: campaignId };
  const first = await api('/api/wa/add-existing', 'POST', body);
  if (!first || first.error) { toast((first && first.error) || 'Could not add them', 'err'); return; }
  const final = await confirmChannelConflicts(first, () =>
    api('/api/wa/add-existing', 'POST', {
      ...body, business_ids: first.conflicts.map(c => c.business_id), confirm_conflicts: true,
    })
  );
  toast(describeAdd(_sumCounts(first, final), 'Added to WhatsApp:'));
  closeModal('modal-import-wa');
  _afterWaAdd();
}

function _afterWaAdd() {
  Object.keys(_waCampaignDetail).forEach(k => delete _waCampaignDetail[k]);
  if (document.getElementById('section-whatsapp').classList.contains('active')) {
    loadWaCampaigns().then(_reloadWaView);
  }
}

// Opens the Scraper already aimed at WhatsApp and this campaign, so what it
// finds lands here and nowhere else.
function scrapeForWhatsApp() {
  const country = document.getElementById('wa-import-country')?.value || 'AE';
  const sel = document.getElementById('wa-add-campaign');
  const campaign_id = sel && sel.value !== '__new' ? sel.value : '';
  closeModal('modal-import-wa');
  window._scraperPreset = { destination: 'whatsapp', country, campaign_id };
  showSection('scraper');
}

async function importWaLeads(campaignId) {
  const country = document.getElementById('wa-import-country').value;
  const fileInput = document.getElementById('wa-import-file');
  const paste = document.getElementById('wa-import-paste').value.trim();

  if (fileInput.files.length) {
    const form = new FormData();
    form.append('file', fileInput.files[0]);
    form.append('country', country);
    form.append('wa_campaign_id', campaignId);
    const csrf = await _getCsrfToken();
    const res = await fetch('/api/wa/import', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRF-Token': csrf }, body: form,
    });
    if (res.status === 401) { window.location.href = '/login'; return; }
    const first = await res.json();
    await _finishWaImport(first, country, campaignId);
    return;
  }

  if (paste) {
    const rows = paste.split('\n').map(l => l.trim()).filter(Boolean).map(line => {
      const [company, phone, website] = line.split(',').map(x => (x || '').trim());
      return { company, phone: phone || '', website: website || '' };
    }).filter(r => r.company);
    if (!rows.length) { toast('Nothing to import — one business per line', 'err'); return; }
    const first = await api('/api/wa/import', 'POST', { rows, country, wa_campaign_id: campaignId });
    await _finishWaImport(first, country, campaignId);
    return;
  }

  toast('Select a file or paste some businesses', 'err');
}

async function _finishWaImport(first, country, campaignId) {
  if (!first || first.error) { toast((first && first.error) || 'Import failed', 'err'); return; }
  const final = await confirmChannelConflicts(first, () =>
    api('/api/wa/import', 'POST', {
      rows: first.conflicts.map(c => c.row), country, wa_campaign_id: campaignId, confirm_conflicts: true,
    })
  );
  const inserted = (first.inserted || 0) + (final !== first ? (final.inserted || 0) : 0);
  toast(`Imported ${inserted} lead${inserted === 1 ? '' : 's'} ✓`);
  closeModal('modal-import-wa');
  _afterWaAdd();
  notifyCrossOwnerOverlap(first);
}
