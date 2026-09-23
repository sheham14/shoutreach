// ── WhatsApp ─────────────────────────────────────────────────────────────────
//
// Sending is always manual. Every action in this file either reads state or
// stages one (a message, a sent-date) -- the only thing that ever reaches
// WhatsApp is the operator's own tap on Send, after this code opens WhatsApp
// with the message typed in and gets out of the way. If you're tempted to make
// "Open in WhatsApp" fire automatically on a schedule, don't -- see
// docs/WhatsApp Module Handover.md for why that's off the table.
//
// A lead arrives ready to send, its message written live from its campaign's
// template. The To do tab is two lists -- Ready to send and Follow-up due --
// worked one lead at a time; everything else (notes, the audit) is optional
// and never in the way of sending. Opening the chat records nothing: the
// operator comes back and says whether it sent, or that the number isn't on
// WhatsApp, because WhatsApp can't tell the app either.

let _waBucket = 'ready';
let _waQueue = [];
let _waCurrent = null;
let _waCampaigns = [];
let _waOpenNext = null;         // {tab, filter} to land on, from another page

function _waFilter() { return document.getElementById('wa-campaign-filter')?.value || ''; }

// Midnight where the operator is, in UTC like the log, for "sent today".
function startOfLocalDay() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.toISOString().replace('T', ' ').substring(0, 19);
}

onTab('whatsapp', name => {
  if (name === 'todo') loadWaTodo();
  if (name === 'leads') { closeLeadPanel('wl-panel'); LT.wl.load({ resetPage: true }); }
  if (name === 'campaigns') renderWaCampaigns();
  if (name === 'templates') renderWaTemplateEditor(_waFilter() || (_waCampaigns[0] && _waCampaigns[0].id));
});

async function loadWaCampaigns() {
  _waCampaigns = await api('/api/wa/campaigns') || [];
  setTabCount('whatsapp', 'campaigns', _waCampaigns.length);
  const sel = document.getElementById('wa-campaign-filter');
  const keep = sel.value;
  // "No campaign" is offered because leads can end up there — deleting a
  // campaign leaves its leads behind (ON DELETE SET NULL). Without a way to
  // list them they were invisible on every tab that scopes by campaign, and
  // invisible leads can't be selected and moved into one.
  const orphans = _waNoCampaignCount;
  sel.innerHTML = '<option value="">All campaigns</option>'
    + _waCampaigns.map(c => `<option value="${c.id}">${esc(c.name)}${c.status === 'archived' ? ' (archived)' : ''}</option>`).join('')
    + (orphans ? `<option value="none">No campaign (${orphans})</option>` : '');
  sel.value = (keep === 'none' && orphans) || _waCampaigns.some(c => String(c.id) === keep)
    ? keep : '';
  return _waCampaigns;
}

// How many of this operator's leads are in no campaign. Read from the summary
// rather than counted here, so it can't drift from what the page shows.
let _waNoCampaignCount = 0;

// ── What actually went out ───────────────────────────────────────────────────
//
// wa_log has recorded every send since the module existed; nothing ever
// showed it, so "which ones did I message?" could only be answered by
// counting pills. The count in the corner is now the way in to the list.

let _waSentWindow = 'today';

async function openWaSentLog() {
  _waSentWindow = 'today';
  const opened = chooseDialog({
    title: 'Messages you’ve sent',
    body: '<div id="wa-sent-body"><div class="empty-state"><p>Loading…</p></div></div>',
    confirm: 'Close', width: 720,
    collect: () => true,   // read-only: Close and ✕ do the same thing
  });
  await waSentWindow('today');
  await opened;
}

// Redraws inside the open dialog rather than opening another one — switching
// period should not stack a second modal behind the first.
async function waSentWindow(key) {
  _waSentWindow = key;
  const el = document.getElementById('wa-sent-body');
  if (el) el.innerHTML = await _waSentLogHtml();
}

async function _waSentLogHtml() {
  const since = { today: startOfLocalDay(), week: _daysAgoUtc(7), all: '' }[_waSentWindow];
  const camp = _waFilter() ? `&wa_campaign_id=${_waFilter()}` : '';
  const rows = await api(`/api/wa/sent?since=${encodeURIComponent(since)}${camp}`) || [];
  const chip = (key, label) =>
    `<button class="chip ${_waSentWindow === key ? 'active' : ''}"
             onclick="waSentWindow('${key}')">${label}</button>`;

  return `
    <div class="chips" style="margin-bottom:12px">
      ${chip('today', 'Today')}${chip('week', 'Last 7 days')}${chip('all', 'Everything')}
    </div>
    ${!rows.length ? '<div class="empty-state"><p>Nothing sent in this period.</p></div>' : `
    <div class="table-wrap" style="max-height:52vh;overflow-y:auto">
      <table class="lead-table"><thead><tr>
        <th>Business</th><th>What</th><th>When</th><th>Since</th>
      </tr></thead><tbody>
        ${rows.map(r => `<tr>
          <td><span class="biz-name">${esc(r.company || 'Unnamed business')}</span>
              <span class="sub mono">${esc(prettyWaNumber(r.wa_number) || '')}</span></td>
          <td>${r.kind === 'followup' ? 'Follow-up' : 'Opening message'}
              ${r.campaign_name ? `<span class="sub">${esc(r.campaign_name)}</span>` : ''}</td>
          <td class="nowrap">${esc(whenLocal(r.sent_at))}</td>
          <td>${r.pipeline_stage ? pipelinePill(r)
                : (r.replied ? pill('Replied', 'green') : '<span class="text-muted">no reply yet</span>')}</td>
        </tr>`).join('')}
      </tbody></table>
    </div>
    <div class="text-muted text-small" style="margin-top:8px">${rows.length} message${rows.length === 1 ? '' : 's'}${
      rows.length >= 500 ? ' (newest 500)' : ''}</div>`}`;
}

function _daysAgoUtc(n) {
  const d = new Date();
  d.setDate(d.getDate() - n);
  d.setHours(0, 0, 0, 0);
  return d.toISOString().replace('T', ' ').substring(0, 19);
}

// The deal half of the Leads filter. "Replied" is already in the markup as
// the handoff point, so only the stages past it are added — and they come
// from the server because you can invent your own.
async function fillWaDealFilter() {
  const group = document.getElementById('wl-stage-deal');
  if (!group) return;
  const stages = (await loadPipelineStages()).filter(s => !s.archived && s.key !== 'replied');
  const keep = group.querySelector('option[value="replied"]');
  group.innerHTML = keep ? keep.outerHTML : '<option value="replied">Replied</option>';
  group.insertAdjacentHTML('beforeend',
    stages.map(s => `<option value="deal:${esc(s.key)}">${esc(s.label)}</option>`).join(''));
}

async function loadWhatsApp() {
  // The unfiltered summary first: it carries both the Leads badge total and
  // how many leads are in no campaign, and the campaign picker needs the
  // second before it can offer "No campaign".
  await refreshWaTotals();
  await Promise.all([loadWaCampaigns(), loadCountries(), fillWaDealFilter()]);
  const next = _waOpenNext;
  _waOpenNext = null;
  if (next && next.filter !== undefined) document.getElementById('wa-campaign-filter').value = String(next.filter);
  renderWaHeader();
  const tab = next ? next.tab : currentTab('whatsapp', 'todo');
  setTab('whatsapp', tab);
  if (tab !== 'todo') refreshWaCounts();
}

// The Leads badge counts every lead on the channel, never what a filter
// happens to be showing. It used to be set from the filtered page and only
// when no stage filter was set, so one click on "marked not on WhatsApp"
// left it reading 0 with 47 leads sitting behind the filter.
async function refreshWaTotals() {
  const s = await api('/api/wa/summary') || {};
  _waNoCampaignCount = s.no_campaign || 0;
  if (s.total !== undefined) setTabCount('whatsapp', 'leads', s.total);
  return s;
}

// Says, above the table, that a filter is narrowing it — and offers the way
// out. Without this an empty Leads tab looks like an empty channel, which is
// exactly how someone concludes their 47 leads have vanished.
function renderWaFilterNote() {
  const note = document.getElementById('wl-filter-note');
  if (!note) return;
  const stageSel = document.getElementById('wl-stage');
  const stage = stageSel ? stageSel.value : '';
  const camp = _waFilter();
  const bits = [];
  if (stage) {
    const opt = stageSel.options[stageSel.selectedIndex];
    bits.push(`showing only <b>${esc(opt ? opt.textContent.trim() : stage)}</b>`);
  }
  if (camp === 'none') bits.push('showing only <b>leads with no campaign</b>');
  else if (camp) {
    const c = _waCampaigns.find(x => String(x.id) === String(camp));
    if (c) bits.push(`in <b>${esc(c.name)}</b>`);
  }
  if (!bits.length) { note.hidden = true; note.innerHTML = ''; return; }
  note.hidden = false;
  note.innerHTML = `Filtered — ${bits.join(', ')}. `
    + `<a style="color:var(--blue);cursor:pointer" onclick="clearWaLeadFilters()">Show all leads</a>`;
}

function clearWaLeadFilters() {
  const stage = document.getElementById('wl-stage');
  const search = document.getElementById('wl-search');
  const camp = document.getElementById('wa-campaign-filter');
  if (stage) stage.value = '';
  if (search) search.value = '';
  if (camp) camp.value = '';
  renderWaHeader();
  LT.wl.load({ resetPage: true });
}

// The heading says which campaign you're in. Every tab below is scoped to it,
// so reading "Leads" without knowing whose leads is how you end up thinking
// you have none.
function renderWaHeader() {
  const id = _waFilter();
  const c = _waCampaigns.find(x => String(x.id) === String(id));
  const crumb = document.getElementById('wa-crumb');
  const title = document.getElementById('wa-title');
  const sub = document.getElementById('wa-sub');
  if (!title) return;
  if (id === 'none') {
    crumb.hidden = false;
    title.textContent = 'Leads with no campaign';
    sub.textContent = 'They have no messages until you move them into one — select them below and use Move to campaign.';
    return;
  }
  if (!c) {
    crumb.hidden = true;
    title.textContent = 'WhatsApp';
    sub.textContent = 'Nothing sends by itself — you always tap Send yourself, inside WhatsApp';
    return;
  }
  crumb.hidden = false;
  title.textContent = c.name;
  sub.textContent = `${c.leads} lead${c.leads === 1 ? '' : 's'} · follows up every `
    + `${c.followup_days} day${c.followup_days === 1 ? '' : 's'}`
    + (c.status === 'archived' ? ' · archived' : '');
}

function waShowAllCampaigns() {
  const sel = document.getElementById('wa-campaign-filter');
  if (sel) sel.value = '';
  waCampaignFilterChanged();
}

function waCampaignFilterChanged() {
  renderWaHeader();
  const tab = currentTab('whatsapp', 'todo');
  if (tab === 'todo') { _waCurrent = null; loadWaTodo(); }
  else if (tab === 'leads') { closeLeadPanel('wl-panel'); LT.wl.load({ resetPage: true, keepSelection: false }); }
  else if (tab === 'templates') renderWaTemplateEditor(_waFilter() || (_waCampaigns[0] && _waCampaigns[0].id));
}

// From the Dashboard and elsewhere.
function openWhatsAppTodo(bucket) {
  _waBucket = bucket === 'due' ? 'due' : 'ready';
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
  const p = new URLSearchParams({ since: startOfLocalDay() });
  if (_waFilter()) p.set('wa_campaign_id', _waFilter());
  const s = await api(`/api/wa/summary?${p}`) || {};
  document.getElementById('wa-count-ready').textContent = s.ready_to_send || 0;
  document.getElementById('wa-count-due').textContent = s.due || 0;
  document.getElementById('wa-sent-today').textContent = s.sent_today || 0;
  const marked = document.getElementById('wa-no-wa-link');
  marked.hidden = !s.no_whatsapp;
  marked.textContent = `${s.no_whatsapp || 0} marked not on WhatsApp →`;
  const work = (s.ready_to_send || 0) + (s.due || 0);
  setTabCount('whatsapp', 'todo', work, true);
  if (!_waFilter()) {
    const nav = document.getElementById('nav-count-whatsapp');
    if (nav) nav.textContent = work ? work : '';
  }
  return s;
}

async function loadWaTodo() {
  ['ready', 'due'].forEach(b =>
    document.getElementById(`wa-chip-${b}`).classList.toggle('active', b === _waBucket));
  await Promise.all([refreshWaCounts(), loadWaQueue()]);
}

// Leads tab, filtered to the ones marked not on WhatsApp, ready to move off.
function openWaNotOnWhatsApp() {
  document.getElementById('wl-stage').value = 'no_whatsapp';
  setTab('whatsapp', 'leads');
}

function setWaBucket(bucket) {
  _waBucket = bucket;
  _waCurrent = null;
  loadWaTodo();
}

async function loadWaQueue() {
  const camp = _waFilter() ? `?wa_campaign_id=${_waFilter()}` : '';
  const rows = _waBucket === 'due'
    ? await api(`/api/wa/followups-due${camp}`)
    : await api(`/api/wa/ready${camp}`);
  _waQueue = Array.isArray(rows) ? rows : [];
  _renderWaQueue();
  if (!_waQueue.some(l => l.id === _waCurrent)) _waCurrent = _waQueue.length ? _waQueue[0].id : null;
  if (_waCurrent) openWaLead(_waCurrent);
  else _renderWaEmptyPanel();
}

function _renderWaEmptyPanel() {
  const msg = _waBucket === 'ready'
    ? 'Nothing waiting to be sent. Scrape with WhatsApp as the destination, or use + Add leads.'
    : 'No follow-ups due right now.';
  document.getElementById('wa-panel').innerHTML = `<div class="empty-state"><p>${esc(msg)}</p></div>`;
  closeSheet('wa-panel');
}

function _waNumberCell(l) {
  return `${esc(prettyWaNumber(l.wa_number) || 'No number')}${l.number_type === 'landline'
    ? ` ${pill('landline', '', 'Landlines are less likely to have WhatsApp')}` : ''}`;
}

function _renderWaQueue() {
  const heads = _waBucket === 'ready'
    ? ['Business', 'Campaign', 'Version', 'Number']
    : ['Business', 'Campaign', 'Last sent', 'Follow-ups'];
  document.getElementById('wa-queue-head').innerHTML = `<tr>${heads.map(h => `<th>${h}</th>`).join('')}<th></th></tr>`;
  const tbody = document.getElementById('wa-queue');
  if (!_waQueue.length) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="empty-state"><p>Nothing here</p></div></td></tr>`;
    return;
  }
  tbody.innerHTML = _waQueue.map(l => {
    const name = `<span class="biz-name">${esc(l.company || 'Unnamed business')}</span>${
      l.opened_at ? ` ${pill('opened', 'amber', "Opened in WhatsApp — say whether it sent")}` : ''}${
      l.city || l.category ? `<span class="sub">${esc([l.category, l.city].filter(Boolean).join(' · '))}</span>` : ''}`;
    const camp = l.campaign_name ? esc(l.campaign_name) : pill('No campaign', 'amber');
    const cells = _waBucket === 'ready'
      ? [l.template_variant ? pill(`Version ${l.template_variant}`) : '<span class="text-muted">—</span>',
         `<span class="mono" style="font-size:12px">${_waNumberCell(l)}</span>`]
      : [`<span class="mono" style="font-size:12px">${esc(shortDate(l.sent_date))}</span>`,
         `<span class="mono">${l.followup_count || 0}</span>`];
    const mobile = mCard(
      `<span class="biz-name">${esc(l.company || 'Unnamed business')}</span>${l.opened_at ? ` ${pill('opened', 'amber')}` : ''}`,
      _waBucket === 'ready'
        ? `${l.campaign_name ? esc(l.campaign_name) : pill('No campaign', 'amber')} · <span class="mono">${_waNumberCell(l)}</span>`
        : `${l.campaign_name ? esc(l.campaign_name) : pill('No campaign', 'amber')} · sent ${esc(shortDate(l.sent_date))} · ${l.followup_count || 0} follow-up${l.followup_count === 1 ? '' : 's'}`);
    return `<tr class="clickable ${l.id === _waCurrent ? 'current' : ''}" onclick="openWaLead(${l.id}, true)">
      <td>${name}</td><td>${camp}</td><td>${cells[0]}</td><td class="nowrap">${cells[1]}</td>
      <td class="m-card">${mobile}</td>
      <td class="nowrap m-keep" style="text-align:right">${l.wa_number ? `<button class="btn btn-primary btn-sm row-wa"
        onclick="event.stopPropagation();openWaFromRow(${l.id})" title="Open this chat in WhatsApp">WhatsApp ↗</button>` : ''}</td></tr>`;
  }).join('');
}

function _waEditedNote(l) {
  if (!l.message_edited) {
    return 'Written from the campaign template. Change anything — this lead keeps your version.';
  }
  // message_source is authoritative where it's set; older leads predate it
  // and are described by the flags that used to carry the same fact.
  const label = WA_SOURCE_LABELS[l.message_source]
    || (l.paraphrased ? WA_SOURCE_LABELS.ai : WA_SOURCE_LABELS.manual);
  return `${label} — template changes won't touch it ·
    <a style="color:var(--blue);cursor:pointer" onclick="resetWaMessage(${l.id})">Reset to template</a>`;
}

// A lead carries its own copy when its opener was written for it, or when an
// import brought follow-ups with it.
function waHasOwnCopy(r) {
  return !!(r.message_edited || (r.draft_followups || '').length);
}

// The version letter says which template arm this lead would get. For a lead
// writing its own messages that's only true of the follow-ups past the ones
// it brought, so the letter is shown as a title rather than as the answer --
// reading it as "this lead got version A's copy" is exactly the mistake the
// reply figures make.
function waVersionCell(r) {
  // Nothing to say when a campaign runs one version and the lead didn't bring
  // its own copy: a column reading "—" down every row is just a question the
  // reader has to answer for themselves.
  if (!waHasOwnCopy(r) && !r.template_variant) return '<span class="text-muted">—</span>';
  if (!waHasOwnCopy(r)) return esc(r.template_variant);
  const label = WA_SOURCE_LABELS[r.message_source] || 'Own copy';
  const letter = r.template_variant ? ` · falls back to version ${r.template_variant}` : '';
  return pill('own copy', 'blue', `${label}${letter}`);
}

// draft_followups arrives as the raw JSON string the column stores.
function waFollowupDrafts(l) {
  try {
    const parsed = JSON.parse(l.draft_followups || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch (e) {
    return [];
  }
}

// Which follow-up is next out, and whether this lead has its own wording for
// it. followup_count is how many have gone, so it indexes the next one.
// Past the last bespoke slot every follow-up is the template, repeating --
// the cadence has no end, so only the first few are ever written per lead.
function _waFollowupNote(l) {
  const i = l.followup_count || 0;
  const which = `Follow-up ${i + 1}`;
  if (i >= WA_MAX_LEAD_FOLLOWUPS) {
    return `${which} — past your written follow-ups, so it's the campaign template from here on.`;
  }
  if (waFollowupDrafts(l)[i]) {
    return `${which} of ${WA_MAX_LEAD_FOLLOWUPS} — your own wording. Clear the box to put it back on the template.`;
  }
  return `${which} of ${WA_MAX_LEAD_FOLLOWUPS} — from the campaign template. Edit it and this lead keeps your version.`;
}

// `show` opens the lead full screen on a phone: a tap on it, as opposed to
// the list picking its first lead when it loads.
let _waShownId = null;

async function openWaLead(id, show = false) {
  _waCurrent = id;
  if (show) openSheet('wa-panel');
  _renderWaQueue();
  const l = _waQueue.find(x => x.id === id);
  if (!l) return;
  const [detail] = await Promise.all([api(`/api/businesses/${l.business_id}`), loadAuditLinks(),
                                      loadPipelineStages()]);
  if (_waCurrent !== id) return;  // another lead was clicked while this loaded
  const ready = _waBucket === 'ready';
  const message = ready ? (l.message || '') : (l.followup_draft || '');
  const links = [];
  if (l.website) links.push(`<a href="${esc(l.website.startsWith('http') ? l.website : 'https://' + l.website)}" target="_blank" rel="noopener" style="color:var(--blue)">Website ↗</a>`);
  links.push(`<span class="mono">${_waNumberCell(l)}</span>`);
  if (l.rating != null) links.push(`${esc(l.rating)}★ (${esc(l.review_count ?? 0)})`);
  const context = [
    l.campaign_name ? `Campaign: ${esc(l.campaign_name)}` : '<span style="color:var(--amber)">No campaign — move it into one (⋯) to get its message</span>',
    l.template_variant && `version ${esc(l.template_variant)}`,
    !ready && `last sent ${esc((l.sent_date || '').substring(0, 16))}`,
    !ready && `${l.followup_count || 0} follow-up${l.followup_count === 1 ? '' : 's'} so far`,
  ].filter(Boolean).join(' · ');

  const panel = document.getElementById('wa-panel');
  if (_waShownId !== id) panel.scrollTop = 0;
  _waShownId = id;
  panel.innerHTML = `
    ${sheetBarHtml("closeSheet('wa-panel')")}
    <div class="flex items-center gap-2" style="justify-content:space-between">
      <h3>${esc(l.company || 'Unnamed business')}</h3>
      <span class="flex items-center gap-2">
        <span class="text-muted text-small mono">${_waQueue.indexOf(l) + 1} of ${_waQueue.length}</span>
        ${_waMoreMenu(l)}
      </span>
    </div>
    <div class="text-small" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:4px">${links.join('')}</div>
    <div class="text-muted text-small" style="margin-top:4px">${context}</div>

    <span class="field-label">${ready ? 'Message' : 'Follow-up'}</span>
    <textarea id="wa-msg" class="soft-input" style="min-height:140px"
              onchange="${ready ? `saveWaMessage(${l.id}, this.value)`
                                : `saveWaFollowup(${l.id}, ${l.followup_count || 0}, this.value)`}"
              >${esc(message)}</textarea>
    <div class="text-muted text-small" id="wa-msg-note">${ready ? _waEditedNote(l) : _waFollowupNote(l)}</div>

    <div id="wa-actions">${_waActionsHtml(l)}</div>
    <div class="text-muted text-small" style="margin-top:8px;display:flex;gap:10px;flex-wrap:wrap">
      ${waOpensInToggleHtml()}
      ${!ready ? `<a style="color:var(--blue);cursor:pointer" onclick="correctWaSentDate(${l.id})">Didn't actually send the last one?</a>` : ''}
    </div>

    <div style="margin-top:14px">${detail && !detail.error ? sharedSectionsHtml(detail, { open: 'none' }) : ''}</div>`;
}

// Before opening the chat: open it. After: did it send?
function _waActionsHtml(l) {
  const kind = _waBucket === 'ready' ? 'opener' : 'followup';
  if (l.opened_at) return waConfirmHtml(l.id, kind, l.opened_at, l.wa_number);
  return `<div class="flex gap-2" style="flex-wrap:wrap;margin-top:12px;align-items:center">
      <button class="btn btn-primary" onclick="openWaLink(${l.id})">Open in WhatsApp</button>
      <button class="btn btn-ghost" onclick="skipWaLead()">Skip</button>
      ${kind === 'opener' ? `<button class="btn btn-ghost btn-sm" onclick="rewordWaMessage(${l.id})">✨ Reword with AI</button>`
              : `<button class="btn btn-ghost btn-sm" onclick="waMarkReplied(${l.id})">They replied</button>
                 <button class="btn btn-ghost btn-sm" onclick="waSetPaused([${l.id}], true)">Pause</button>`}
    </div>
    <div class="text-muted text-small" style="margin-top:8px">Nothing is recorded until you come back and say whether it sent.</div>`;
}

function _renderWaActions(l) {
  const el = document.getElementById('wa-actions');
  if (el && _waCurrent === l.id) el.innerHTML = _waActionsHtml(l);
}

function _waMoreMenu(l) {
  return `<div class="row-menu">
    <button class="btn btn-ghost btn-sm" onclick="toggleRowMenu(this)" title="More">⋯</button>
    <div class="row-menu-list">
      <button onclick="markNotOnWhatsApp([${l.id}])">Not on WhatsApp</button>
      <button onclick="moveWaLeadsToCampaign([${l.id}])">Move to another campaign…</button>
      <button onclick="openBusinessForm(${l.business_id})">Edit details…</button>
      <button class="danger" onclick="removeWaLeads([${l.id}])">Take off WhatsApp</button>
    </div>
  </div>`;
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

function _onTodo(id) {
  return currentTab('whatsapp', 'todo') === 'todo' && _waQueue.some(l => l.id === id);
}

// Opens WhatsApp with whatever is in the message box right now -- an edit made
// just before clicking is what goes, not what was written originally. Records
// only that the chat was opened; the lead then asks whether it sent.
function openWaLink(id) {
  const lead = _waQueue.find(l => l.id === id);
  if (!lead || !lead.wa_number) { toast('No WhatsApp number on file for this lead', 'err'); return; }
  const message = document.getElementById('wa-msg').value;
  openWhatsAppChat(lead.wa_number, message);
  lead.opened_at = utcNow();
  _renderWaQueue();
  _renderWaActions(lead);
  api(`/api/wa/leads/${id}/opened`, 'POST').then(res => {
    if (res && res.opened_at) lead.opened_at = res.opened_at;
  });
}

// Straight from the lead's row in the list: opens the chat and the lead
// together, one click fewer than opening the lead first. The message is what's
// in the box when it's the lead already open (edits included), otherwise the
// lead's own message.
function openWaFromRow(id) {
  const lead = _waQueue.find(l => l.id === id);
  if (!lead || !lead.wa_number) { toast('No WhatsApp number on file for this lead', 'err'); return; }
  const box = document.getElementById('wa-msg');
  const message = _waCurrent === id && box ? box.value
    : ((_waBucket === 'ready' ? lead.message : lead.followup_draft) || '');
  openWhatsAppChat(lead.wa_number, message);
  lead.opened_at = utcNow();
  if (_waCurrent === id) { _renderWaQueue(); _renderWaActions(lead); openSheet('wa-panel'); } else openWaLead(id, true);
  api(`/api/wa/leads/${id}/opened`, 'POST').then(res => {
    if (res && res.opened_at) lead.opened_at = res.opened_at;
  });
}

// The same, from a lead's side panel on the Leads tab.
function openFromPanel(id, kind, number, panelId) {
  const box = document.getElementById('wl-msg');
  if (!number) { toast('No WhatsApp number on file for this lead', 'err'); return; }
  openWhatsAppChat(number, box ? box.value : '');
  const actions = document.getElementById('wl-actions');
  if (actions) actions.innerHTML = waConfirmHtml(id, kind, utcNow(), number, panelId);
  api(`/api/wa/leads/${id}/opened`, 'POST').then(() => { if (LT.wl) LT.wl.load(); });
}

// "Open the chat again", from the did-it-send box.
function reopenWaChat(id, number, panelId = null) {
  const box = document.getElementById(panelId ? 'wl-msg' : 'wa-msg');
  openWhatsAppChat(number, box ? box.value : '');
  api(`/api/wa/leads/${id}/opened`, 'POST');
}

// "Sent": the one thing that records a message as sent.
async function confirmWaSent(id, kind, panelId = null) {
  await _waSaving;
  const box = document.getElementById(panelId ? 'wl-msg' : 'wa-msg');
  const res = await api(`/api/wa/leads/${id}/sent`, 'POST', { kind, message: box ? box.value : '' });
  if (!res || res.error) { toast((res && res.error) || 'Could not record it', 'err'); return; }
  if (panelId) {
    toast('Recorded as sent');
    LT.wl.load();
    refreshLeadPanel(panelId);
    refreshWaCounts();
    return;
  }
  _advanceWa(id);
}

async function waDidntSend(id, panelId = null) {
  const res = await api(`/api/wa/leads/${id}/opened`, 'DELETE');
  if (!res || res.error) { toast((res && res.error) || 'Could not update', 'err'); return; }
  if (panelId) { refreshLeadPanel(panelId); LT.wl.load(); return; }
  const l = _waQueue.find(x => x.id === id);
  if (l) { l.opened_at = null; _renderWaQueue(); _renderWaActions(l); }
}

// Saving an edit happens when the box loses focus -- which is also what
// clicking "Reset to template" or "Reword" does first. Those wait for the save
// so it can't land after them and undo them.
let _waSaving = Promise.resolve();

function saveWaMessage(id, message, panelId = null) {
  _waSaving = _waSaving.then(() => _saveWaMessage(id, message, panelId)).catch(() => {});
  return _waSaving;
}

async function _saveWaMessage(id, message, panelId) {
  if (!message.trim()) { toast("The message can't be empty", 'err'); return; }
  const res = await api(`/api/wa/leads/${id}/message`, 'PUT', { message });
  if (!res || res.error) { toast((res && res.error) || 'Could not save the edit', 'err'); return; }
  if (panelId) { refreshLeadPanel(panelId); return; }
  const l = _waQueue.find(x => x.id === id);
  if (!l) return;
  Object.assign(l, { message, message_edited: 1, paraphrased: 0 });
  const note = document.getElementById('wa-msg-note');
  if (note && _waCurrent === id) note.innerHTML = _waEditedNote(l);
}

// The follow-up that's next out for this lead. Saved into its own slot rather
// than into draft_message, which is the opener's -- a lead on follow-up two
// still has an opener, and it isn't this.
//
// Past the bespoke slots the box is showing the campaign's template, which is
// shared copy: saving it here would quietly turn a template edit into a
// per-lead one, so it isn't offered.
function saveWaFollowup(id, index, message) {
  if (index >= WA_MAX_LEAD_FOLLOWUPS) {
    toast('Past your written follow-ups — edit the campaign template instead', 'err');
    return Promise.resolve();
  }
  _waSaving = _waSaving.then(() => _saveWaFollowup(id, index, message)).catch(() => {});
  return _waSaving;
}

async function _saveWaFollowup(id, index, message) {
  const res = await api(`/api/wa/leads/${id}/followup/${index}`, 'PUT', { message });
  if (!res || res.error) { toast((res && res.error) || 'Could not save the follow-up', 'err'); return; }
  const l = _waQueue.find(x => x.id === id);
  if (!l) return;
  Object.assign(l, {
    draft_followups: JSON.stringify(res.followups || []),
    followup_draft: res.followup_message,
    message_source: res.message_source,
  });
  const note = document.getElementById('wa-msg-note');
  if (note && _waCurrent === id) note.innerHTML = _waFollowupNote(l);
  toast(message.trim() ? 'Saved for this lead' : 'Back to the campaign template');
}

async function resetWaMessage(id, panelId = null) {
  await _waSaving;
  const res = await api(`/api/wa/leads/${id}/message`, 'DELETE');
  if (!res || res.error) { toast((res && res.error) || 'Could not reset it', 'err'); return; }
  toast('Back to the template');
  if (panelId) { refreshLeadPanel(panelId); return; }
  const l = _waQueue.find(x => x.id === id);
  if (l) { Object.assign(l, { message: res.message, message_edited: 0, paraphrased: 0 }); openWaLead(id); }
}

async function rewordWaMessage(id, panelId = null) {
  await _waSaving;
  const box = document.getElementById(panelId ? 'wl-msg' : 'wa-msg');
  toast('Rewording…');
  const res = await api(`/api/wa/leads/${id}/reword`, 'POST', { message: box ? box.value : '' });
  if (!res || res.error) { toast((res && res.error) || 'Could not reword it', 'err'); return; }
  if (panelId) { refreshLeadPanel(panelId); return; }
  const l = _waQueue.find(x => x.id === id);
  if (l) { Object.assign(l, { message: res.message, message_edited: 1, paraphrased: 1 }); openWaLead(id); }
}

async function correctWaSentDate(id) {
  const value = await chooseDialog({
    title: "Didn't actually send it?",
    body: `<p class="text-small" style="line-height:1.6;margin-bottom:10px">It was recorded as sent when you said so.
      Put in the real time, or clear it.</p>
      ${choiceCard({ name: 'wa-sd', value: 'clear', title: "I didn't send it", checked: true,
        hint: 'Clears the date. If it was the first message, the lead goes back to Ready to send.' })}
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
  if (replied && _onTodo(id)) _advanceWa(id); else _reloadWaView();
}

async function waSetPaused(ids, paused) {
  const res = await api('/api/wa/leads/bulk', 'POST', { action: paused ? 'pause' : 'resume', wa_lead_ids: ids });
  if (!res || res.error) { toast((res && res.error) || 'Could not update', 'err'); return; }
  toast(paused ? `Paused ${res.updated} — no follow-ups until resumed` : `Resumed ${res.updated}`);
  if (paused && ids.length === 1 && _onTodo(ids[0])) _advanceWa(ids[0]);
  else _reloadWaView();
}

function _reloadWaView() {
  if (!document.getElementById('section-whatsapp').classList.contains('active')) {
    if (document.getElementById('section-contacts')?.classList.contains('active')) {
      refreshLeadPanel('ct-detail');
      if (LT.ct) LT.ct.load();
    }
    return;
  }
  // The channel total and the no-campaign count can both have changed —
  // leads added, moved off, or a campaign deleted out from under them.
  refreshWaTotals().then(() => { renderWaHeader(); renderWaFilterNote(); });
  const tab = currentTab('whatsapp', 'todo');
  if (tab === 'todo') loadWaTodo();
  if (tab === 'leads') { LT.wl.load(); refreshLeadPanel('wl-panel'); refreshWaCounts(); }
  if (tab === 'campaigns') renderWaCampaigns();
}

// "Not on WhatsApp": marked, out of every queue, still on WhatsApp -- so the
// operator keeps sending and moves them all off in one go later.
async function markNotOnWhatsApp(ids, panelId = null) {
  if (!ids.length) return;
  const res = await api('/api/wa/leads/bulk', 'POST', { action: 'no_whatsapp', wa_lead_ids: ids });
  if (!res || res.error) { toast((res && res.error) || 'Could not mark them', 'err'); return; }
  toast(ids.length === 1 ? 'Marked not on WhatsApp — move it off later from Leads'
                         : `Marked ${res.updated} not on WhatsApp`);
  if (ids.length === 1 && _onTodo(ids[0])) { _advanceWa(ids[0]); return; }
  if (LT.wl) LT.wl.clear();
  _reloadWaView();
}

async function unmarkNotOnWhatsApp(ids) {
  if (!ids.length) return;
  const res = await api('/api/wa/leads/bulk', 'POST', { action: 'on_whatsapp', wa_lead_ids: ids });
  if (!res || res.error) { toast((res && res.error) || 'Could not update', 'err'); return; }
  toast(`${res.updated} back in the queue`);
  if (LT.wl) LT.wl.clear();
  _reloadWaView();
}

// Off WhatsApp for good, to Calling, Email or nowhere -- one lead or all of them.
async function moveWaLeadsOff(ids) {
  if (!ids.length) return;
  const [callCamps, emailCamps] = await Promise.all([api('/api/call-campaigns'), api('/api/campaigns')]);
  const n = ids.length;
  const box = 'onclick="event.stopPropagation()" style="margin-top:8px"';
  const choice = await chooseDialog({
    title: `Move ${n === 1 ? 'this lead' : `${n} leads`} off WhatsApp`,
    width: 520,
    body: `<p class="text-muted text-small" style="margin-bottom:12px">They come off WhatsApp for good, so a later scrape
        can't put the numbers back. Where should they go?</p>
      ${choiceCard({ name: 'wa-mv', value: 'call', title: 'Calling', checked: true,
        hint: 'Onto your call list, and into a campaign if you pick one.',
        extra: `<div ${box}>${campaignSelectHtml('wa-mv-call', Array.isArray(callCamps) ? callCamps : [],
          { allowNone: true, noneLabel: 'No campaign — just add to Calling' })}</div>` })}
      ${choiceCard({ name: 'wa-mv', value: 'email', title: 'Email',
        hint: 'Any with no email address on file stay here, still marked.',
        extra: `<div ${box}>${campaignSelectHtml('wa-mv-email', Array.isArray(emailCamps) ? emailCamps : [],
          { allowNone: true, noneLabel: "Don't enroll yet", allowNew: false })}</div>` })}
      ${choiceCard({ name: 'wa-mv', value: 'none', title: 'Nowhere for now',
        hint: 'They wait in Contacts, under Unassigned.' })}`,
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
  const res = await api('/api/wa/leads/bulk', 'POST',
                        { action: 'move', wa_lead_ids: ids, destination: choice, campaign_id: campaignId || null });
  if (!res || res.error) { toast((res && res.error) || 'Could not move them', 'err'); return; }
  const where = { call: 'Calling', email: 'Email', none: 'Contacts → Unassigned' }[choice];
  const held = [res.no_phone && `${res.no_phone} had no phone number`,
                res.no_email && `${res.no_email} had no email address`,
                res.opted_out && `${res.opted_out} asked not to be contacted`].filter(Boolean);
  toast(`Moved ${res.moved} to ${where}` + (held.length ? ` — ${held.join(', ')}, so still on WhatsApp` : ''),
        res.moved ? 'ok' : 'err');
  if (LT.wl) LT.wl.clear();
  if (ids.length === 1 && res.moved && _onTodo(ids[0])) _advanceWa(ids[0]);
  else _reloadWaView();
}

async function removeWaLeads(ids) {
  if (!ids.length) return;
  const ok = await chooseDialog({
    title: `Take ${ids.length} off WhatsApp?`,
    body: `<p class="text-small" style="line-height:1.6">They drop out of every WhatsApp list. Their message history is kept,
      they stay in Contacts, and you can add them back any time. (If the number just isn't on WhatsApp,
      mark it "Not on WhatsApp" instead.)</p>`,
    confirm: 'Take off WhatsApp', danger: true,
  });
  if (!ok) return;
  const res = await api('/api/wa/leads/bulk', 'POST', { action: 'remove', wa_lead_ids: ids });
  if (!res || res.error) { toast((res && res.error) || 'Could not remove them', 'err'); return; }
  toast(`Took ${res.updated} off WhatsApp`);
  if (ids.length === 1 && _onTodo(ids[0])) _advanceWa(ids[0]);
  else { if (LT.wl) LT.wl.clear(); _reloadWaView(); }
}

// The same stage over a selection. No date here — a date belongs to one
// conversation, and quietly giving fifty leads the same deadline would be
// worse than making you set them one at a time.
async function setWaLeadsStage(ids) {
  if (!ids || !ids.length) { toast('Select some leads first', 'err'); return; }
  const live = (await loadPipelineStages()).filter(s => !s.archived);
  const values = await chooseDialog({
    title: `Set a stage for ${ids.length} lead${ids.length === 1 ? '' : 's'}`,
    body: `<label class="field-label">Stage</label>
      <select id="wbs-stage" class="filter-select" style="width:100%;max-width:100%">
        <option value="">Nothing yet (clear it)</option>
        ${live.map(s => `<option value="${esc(s.key)}">${esc(s.label)}</option>`).join('')}
      </select>
      <div class="form-hint" style="margin-top:8px">Stages belong to the business, so this
        shows on Calling and Email too. Setting one stops their WhatsApp follow-ups; clearing
        one doesn't start them again.</div>`,
    confirm: 'Save',
    collect: () => ({ stage: document.getElementById('wbs-stage').value }),
  });
  if (!values) return;
  const res = await api('/api/wa/leads/bulk', 'POST',
                        { action: 'pipeline', wa_lead_ids: ids, stage: values.stage });
  if (!res || res.error) { toast((res && res.error) || 'Could not set that', 'err'); return; }
  toast(`Updated ${res.updated}`);
  LT.wl.load();
}

async function moveWaLeadsToCampaign(ids) {
  if (!ids.length) return;
  await loadWaCampaigns();
  const ok = await chooseDialog({
    title: `Move ${ids.length} to a campaign`,
    body: `<label class="field-label">Campaign</label>${campaignSelectHtml('wa-mc', _waCampaigns)}
      <div class="form-hint">Unsent messages switch to this campaign's templates straight away. Messages already sent stay as they were.</div>`,
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
    { key: 'stage', label: 'Stage', sort: true,
      render: r => waStagePill(r.stage) + (r.opened_at && ['ready', 'due'].includes(r.stage)
        ? ` ${pill('opened', 'amber', 'Opened in WhatsApp — say whether it sent')}` : '') },
    { key: 'pipeline_stage', label: 'Deal', sort: true,
      render: r => r.pipeline_stage
        ? pipelinePill(r) + (pipelineOverdue(r) ? ' <span style="color:var(--red)" title="Past its date">!</span>' : '')
        : '<span class="text-muted">—</span>' },
    { key: 'template_variant', label: 'Version', sort: true, cls: 'num', render: waVersionCell },
    { key: 'sent_date', label: 'Last sent', sort: true, cls: 'num', render: r => esc(shortDate(r.sent_date)) },
    { key: 'followup_count', label: 'Follow-ups', sort: true, cls: 'num', render: r => r.followup_count || 0 },
    { key: 'created_at', label: 'Added', sort: true, cls: 'num', render: r => esc(shortDate(r.created_at)) },
  ],
  mobile: r => mCard(`<span class="biz-name">${esc(r.company || 'Unnamed business')}</span>`,
    `${waStagePill(r.stage)}${r.opened_at && ['ready', 'due'].includes(r.stage) ? ` ${pill('opened', 'amber')}` : ''}
     ${r.campaign_name ? esc(r.campaign_name) : pill('No campaign', 'amber')}`),
  onRowClick: r => openLeadPanel(r.business_id, {
    channel: 'whatsapp', panelId: 'wl-panel', splitId: 'wl-split',
    onClose: () => { LT.wl.currentId = null; LT.wl.render(); },
  }),
  bulk: () => document.getElementById('wl-stage')?.value === 'no_whatsapp' ? `
    <button class="btn btn-primary btn-sm" onclick="moveWaLeadsOff(LT.wl.selectedIds())">Move off WhatsApp…</button>
    <button class="btn btn-ghost btn-sm" onclick="unmarkNotOnWhatsApp(LT.wl.selectedIds())">They're on WhatsApp after all</button>
    <button class="btn btn-danger btn-sm" onclick="removeWaLeads(LT.wl.selectedIds())">Take off WhatsApp</button>` : `
    <button class="btn btn-ghost btn-sm" onclick="setWaLeadsStage(LT.wl.selectedIds())">Set a stage…</button>
    <button class="btn btn-ghost btn-sm" onclick="moveWaLeadsToCampaign(LT.wl.selectedIds())">Move to campaign</button>
    <button class="btn btn-ghost btn-sm" onclick="waSetPaused(LT.wl.selectedIds(), true)">Pause follow-ups</button>
    <button class="btn btn-ghost btn-sm" onclick="waSetPaused(LT.wl.selectedIds(), false)">Resume</button>
    <button class="btn btn-ghost btn-sm" onclick="markNotOnWhatsApp(LT.wl.selectedIds())">Not on WhatsApp</button>
    <button class="btn btn-danger btn-sm" onclick="removeWaLeads(LT.wl.selectedIds())">Take off WhatsApp</button>`,
  menu: r => {
    const on = !['moved', 'removed'].includes(r.stage);
    const marked = r.stage === 'no_whatsapp';
    return [
      on && ['ready', 'due'].includes(r.stage) && { label: 'Work it in To do', run: `openWaLeadFromTable(LT.wl.rowById(${r.id}))` },
      marked && { label: 'Move off WhatsApp…', run: `moveWaLeadsOff([${r.id}])` },
      marked && { label: "It's on WhatsApp after all", run: `unmarkNotOnWhatsApp([${r.id}])` },
      on && { label: 'Move to another campaign…', run: `moveWaLeadsToCampaign([${r.id}])` },
      on && !marked && r.sent_date && !r.replied && { label: r.paused ? 'Resume follow-ups' : 'Pause follow-ups', run: `waSetPaused([${r.id}], ${!r.paused})` },
      on && ['waiting', 'due', 'paused'].includes(r.stage) && { label: 'They replied', run: `waMarkReplied(${r.id}, true)` },
      on && { label: r.pipeline_stage ? 'Change the stage…' : 'Set a stage…',
              run: `setPipelineStage(${r.business_id}, 'whatsapp', () => LT.wl.load())` },
      r.stage === 'replied' && { label: "Undo 'replied'", run: `waMarkReplied(${r.id}, false)` },
      on && r.sent_date && { label: "Didn't actually send?", run: `correctWaSentDate(${r.id})` },
      on && !marked && { label: 'Not on WhatsApp', run: `markNotOnWhatsApp([${r.id}])` },
      on && { label: 'Take off WhatsApp', run: `removeWaLeads([${r.id}])`, danger: true },
    ];
  },
  // The badge is the channel's total (refreshWaTotals), not this page's, so
  // it isn't touched here. What IS said here is when a filter is hiding
  // everything — an empty table under an active filter reads as "you have no
  // leads" unless it says otherwise.
  onLoad: () => renderWaFilterNote(),
});

function openWaLeadFromTable(r) {
  if (!r) return;
  const bucket = { ready: 'ready', due: 'due' }[r.stage];
  if (!bucket) return;
  _waBucket = bucket;
  _waCurrent = r.id;
  setTab('whatsapp', 'todo');
}

// ── Campaigns ────────────────────────────────────────────────────────────────

async function renderWaCampaigns() {
  await Promise.all([loadWaCampaigns(), loadCountries()]);
  const tbody = document.getElementById('wa-campaign-list');
  if (!_waCampaigns.length) {
    tbody.innerHTML = `<tr><td colspan="8"><div class="empty-state"><p>No WhatsApp campaigns yet.
      Create one — it holds the message templates its leads are written from.</p></div></td></tr>`;
    return;
  }
  tbody.innerHTML = _waCampaigns.map(c => {
    const actions = `
        <button class="btn btn-ghost btn-sm" onclick="openWaTemplates(${c.id})">Templates</button>
        <button class="btn btn-ghost btn-sm" onclick="openWhatsAppCampaign(${c.id})">Leads</button>
        <button class="btn btn-ghost btn-sm" onclick="openWaCampaignSettings(${c.id})">Settings</button>
        <div class="row-menu">
          <button class="btn btn-ghost btn-sm" onclick="toggleRowMenu(this)">⋯</button>
          <div class="row-menu-list">
            <button onclick="openWaImportModal(${c.id})">Add leads…</button>
            <button onclick="openWaCampaignSettings(${c.id})">Settings…</button>
            <button onclick="openWaTemplates(${c.id})">Edit the messages…</button>
            <button onclick="setWaCampaignStatus(${c.id}, '${c.status === 'archived' ? 'active' : 'archived'}')">${c.status === 'archived' ? 'Unarchive' : 'Archive'}</button>
            <button class="danger" onclick="deleteWaCampaign(${c.id})">Delete campaign</button>
          </div>
        </div>`;
    return `
    <tr class="${c.status === 'archived' ? 'text-muted' : ''}">
      <td><span class="biz-name">${esc(c.name)}</span>${c.status === 'archived' ? ' ' + pill('archived') : ''}
        <span class="sub">${esc([countryName(c.country), c.notes].filter(Boolean).join(' · '))}</span></td>
      <td class="num">${c.leads}</td>
      <td class="num">${c.ready}</td>
      <td class="num">${c.due ? `<span style="color:var(--amber)">${c.due}</span>` : 0}</td>
      <td class="num">${c.messaged}</td>
      <td class="num">${c.replied} <span class="text-muted">(${c.reply_rate}%)</span></td>
      <td class="num">${c.followup_days} days</td>
      <td class="nowrap">${actions}</td>
      <td class="m-card">${mCard(
        `<span class="biz-name">${esc(c.name)}</span>${c.status === 'archived' ? ' ' + pill('archived') : ''}`,
        `${c.leads} leads · ${c.ready} ready · ${c.due} due · ${c.replied} replied (${c.reply_rate}%)`,
        `<span class="m-actions">${actions}</span>`)}</td>
    </tr>`;
  }).join('');
}

function openWaTemplates(id) {
  setTab('whatsapp', 'templates', { load: false });
  renderWaTemplateEditor(id);
}

// How the campaign behaves, kept apart from what it says. The follow-up gap
// used to be reachable only through the Templates editor, which is where
// nobody looked for it -- "how often do I follow up" is a setting, not copy.
async function openWaCampaignSettings(id) {
  await Promise.all([loadWaCampaigns(), loadCountries()]);
  const c = _waCampaigns.find(x => String(x.id) === String(id));
  if (!c) { toast('Campaign not found', 'err'); return; }
  const values = await chooseDialog({
    title: 'Campaign settings',
    body: `<label class="field-label">Name</label>
      <input id="wcs-name" class="soft-input" value="${esc(c.name)}" />
      <label class="field-label">Country the numbers are in</label>
      ${countryPickerHtml('wcs-country', c.country)}
      <label class="field-label">Follow up every</label>
      <div class="flex items-center gap-2">
        <input id="wcs-gap" type="number" min="1" max="365" class="soft-input"
               style="max-width:90px" value="${esc(c.followup_days)}" />
        <span class="text-muted text-small">days, until they reply or you pause</span>
      </div>
      <div class="form-hint" style="margin-top:8px">Follow-ups never stop on their own.
        The messages themselves are under <b>Templates</b>.</div>`,
    confirm: 'Save',
    collect: () => {
      const name = document.getElementById('wcs-name').value.trim();
      if (!name) { toast('Give it a name', 'err'); return null; }
      const country = countryValue('wcs-country');
      if (!country) { toast('Pick a country from the list', 'err'); return null; }
      const gap = parseInt(document.getElementById('wcs-gap').value, 10);
      if (!gap || gap < 1) { toast('Follow up every how many days?', 'err'); return null; }
      return { name, country, followup_days: gap };
    },
  });
  if (!values) return;
  const res = await api(`/api/wa/campaigns/${id}`, 'PATCH', values);
  if (!res || res.error) { toast((res && res.error) || 'Could not save that', 'err'); return; }
  toast('Saved');
  await loadWaCampaigns();
  renderWaCampaigns();
}

async function openNewWaCampaign() {
  await Promise.all([loadWaCampaigns(), loadCountries()]);
  const values = await chooseDialog({
    title: 'New WhatsApp campaign',
    body: `<label class="field-label">Name</label>
      <input id="nwc-name" class="soft-input" placeholder="Dubai dental — websites" />
      <label class="field-label">Country</label>
      ${countryPickerHtml('nwc-country', _countries.used[0] || 'AE')}
      <label class="field-label">Start its messages from</label>
      <select id="nwc-copy" class="filter-select" style="width:100%;max-width:100%">
        <option value="">The starter templates</option>
        ${_waCampaigns.map(c => `<option value="${c.id}">A copy of “${esc(c.name)}”</option>`).join('')}
      </select>`,
    confirm: 'Create',
    collect: () => {
      const name = document.getElementById('nwc-name').value.trim();
      if (!name) { toast('Give it a name', 'err'); return null; }
      const country = countryValue('nwc-country');
      if (!country) { toast('Pick a country from the list', 'err'); return null; }
      return { name, country, copy_from: document.getElementById('nwc-copy').value || null };
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
      no campaign — move them into another one before sending to them.</p>`,
    confirm: 'Delete campaign', danger: true,
  });
  if (!ok) return;
  const res = await api(`/api/wa/campaigns/${id}`, 'DELETE');
  if (!res || res.error) { toast((res && res.error) || 'Could not delete', 'err'); return; }
  toast('Campaign deleted — leads kept');
  renderWaCampaigns();
}

// ── Templates ────────────────────────────────────────────────────────────────

const WA_ARM_LABELS = ['A', 'B', 'C', 'D'];

const WA_TEMPLATE_KINDS = [
  ['opener',   'Opening message', 'The first message every lead in this campaign gets. With more than one version, leads take turns: A, B, A, B…'],
  ['followup', 'Follow-up', 'Sent every few days until they reply or you pause. A lead gets the follow-up with the same letter as its opening message.'],
];

const WA_SAMPLE_LEAD = { company: 'Pearl Dental Clinic', city: 'Dubai', category: 'Dentist', rating: 4.8,
                         review_count: 126, website: 'https://pearldental.ae' };

let _waEdit = null;         // the campaign being edited, with unsaved edits
let _waEditSample = null;   // a real lead from it, when it has one, for previews
let _waLastFocus = null;    // where a placeholder button inserts

async function renderWaTemplateEditor(id) {
  const wrap = document.getElementById('wa-template-editor');
  if (!_waCampaigns.length) await loadWaCampaigns();
  await loadCountries();
  if (!id) {
    wrap.innerHTML = `<div class="card"><div class="empty-state"><p>Templates belong to a campaign. Create one first.</p>
      <button class="btn btn-primary" style="margin-top:12px" onclick="openNewWaCampaign()">+ New campaign</button></div></div>`;
    return;
  }
  const c = await api(`/api/wa/campaigns/${id}`);
  if (!c || c.error) { toast('Could not load that campaign', 'err'); return; }
  _waEdit = JSON.parse(JSON.stringify(c));
  // Which saved version each opener box came from, so deleting B tells the
  // server to re-deal B's unsent leads instead of quietly handing them C's text.
  _waEdit.openerFrom = (c.templates.opener || []).map((_, i) => WA_ARM_LABELS[i]);
  const leads = await api(`/api/wa/leads?wa_campaign_id=${id}&limit=1`) || [];
  _waEditSample = leads[0] || null;
  _drawWaTemplateEditor();
}

function _readWaTemplateEditor() {
  if (!_waEdit) return;
  const val = id => document.getElementById(id);
  if (val('wt-name')) _waEdit.name = val('wt-name').value;
  if (val('wt-country')) _waEdit.country = countryValue('wt-country') || _waEdit.country;
  if (val('wt-followup')) _waEdit.followup_days = parseInt(val('wt-followup').value, 10) || 1;
  if (val('wt-notes')) _waEdit.notes = val('wt-notes').value;
  WA_TEMPLATE_KINDS.forEach(([kind]) => {
    _waEdit.templates[kind] = (_waEdit.templates[kind] || ['']).map(
      (arm, i) => val(`wt-${kind}-${i}`)?.value ?? arm);
  });
  _readWaVariablesOnly();
}

// Only leads that were actually sent this arm's words. A lead with its own
// copy still carries an arm label — it says which template it would fall back
// to — so counting it here would report a reply to a message this arm never
// sent.
function _waStatsLine(label) {
  const rows = (_waEdit.stats || []).filter(s => s.arm === label && !s.own_copy);
  if (!rows.length) return '';
  return rows.map(r => `${r.sent} sent ${r.paraphrased ? 'AI-reworded' : 'as written'} · ${r.replied} replied (${r.reply_rate}%)`).join('  |  ');
}

// Said once, above the versions: how much of this campaign never used them.
// Without it the per-version figures look like the whole picture, and a
// campaign running on imported copy would be read as a template that nobody
// answers.
function _waOwnCopyNote() {
  const stats = _waEdit.stats || [];
  const own = stats.filter(s => s.own_copy);
  if (!own.length) return '';
  const sum = (rows, key) => rows.reduce((n, r) => n + (r[key] || 0), 0);
  const ownSent = sum(own, 'sent');
  const total = sum(stats, 'sent');
  if (!ownSent || !total) return '';
  const replied = sum(own, 'replied');
  const rate = Math.round(replied / ownSent * 1000) / 10;
  const share = Math.round(ownSent / total * 100);
  return `<div class="text-muted text-small" style="margin-bottom:10px">
      ${ownSent} of ${total} sent (${share}%) used copy written for the lead, not these versions —
      ${replied} replied (${rate}%).
      ${share >= 80 ? 'The version figures below cover what little is left, so treat them as a footnote.' : ''}
    </div>`;
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
    const retired = arms.some(a => /\{\{\s*signal_detail/.test(a));
    return `<div style="margin-top:22px">
      <div class="card-title">${esc(label)}</div>
      <div class="text-muted text-small" style="margin:2px 0 8px">${esc(hint)}</div>
      ${retired ? `<div class="text-small" style="color:var(--amber);margin-bottom:8px">{{signal_detail}} came from the old
        booking check, which is gone — it's empty for every new lead. Take it out.</div>` : ''}
      ${arms.map((arm, i) => `
        <div style="margin-bottom:12px">
          ${testing ? `<div class="flex items-center" style="margin-bottom:4px;gap:8px">
            ${pill(`Version ${WA_ARM_LABELS[i]}`, 'blue')}
            <span class="text-muted text-small">${kind === 'opener' ? esc(_waStatsLine(WA_ARM_LABELS[i])) : ''}</span>
            <button class="btn btn-ghost btn-sm ml-auto" onclick="removeWaArm('${kind}', ${i})">Remove</button>
          </div>` : ''}
          <textarea id="wt-${kind}-${i}" class="soft-input" style="min-height:${kind === 'followup' ? 70 : 100}px"
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
        <select class="filter-select" onchange="renderWaTemplateEditor(this.value)">
          ${_waCampaigns.map(x => `<option value="${x.id}" ${x.id === c.id ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}
        </select>
        <button class="btn btn-ghost btn-sm" onclick="openNewWaCampaign()">+ New campaign</button>
        <button class="btn btn-primary ml-auto" onclick="saveWaTemplates()">Save</button>
      </div>
      <div class="notice">Leads waiting in Ready to send are written from these templates as they are when you open them,
        so a saved change reaches all of them straight away — except messages you've edited or reworded on a lead.
        Messages already sent never change.</div>

      <div class="responsive-grid-3" style="gap:12px">
        <div><span class="field-label">Name</span><input id="wt-name" class="soft-input" value="${esc(c.name)}" /></div>
        <div><span class="field-label">Country</span>${countryPickerHtml('wt-country', c.country)}</div>
        <div><span class="field-label">Follow up every</span>
          <div class="flex items-center gap-2"><input id="wt-followup" type="number" min="1" max="365" class="soft-input"
            style="max-width:90px" value="${esc(c.followup_days)}" /> <span class="text-muted text-small">days, until they reply or you pause</span></div></div>
      </div>

      <span class="field-label">Placeholders — click to insert where you're typing</span>
      <div class="placeholder-list">${fieldButtons}</div>
      <div class="text-muted text-small">Add a fallback for anything that might be missing: <span class="mono">{{city|your area}}</span>.
        ${total ? `An amber count means some of this campaign's ${total} leads don't have that detail.` : ''}
        Previews use ${_waEditSample ? `a real lead from this campaign (${esc(_waEditSample.company)})` : 'a made-up clinic'}.</div>

      ${_waOwnCopyNote()}
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
  box.innerHTML = fillPlaceholders(ta.value, waFieldsFor(_waEditSample || WA_SAMPLE_LEAD, _waEdit.variables || {}),
                                   { highlight: true });
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
  const target = _waLastFocus && document.body.contains(_waLastFocus) ? _waLastFocus : document.getElementById('wt-opener-0');
  insertAtCursor(target, `{{${key}}}`);
}

function addWaArm(kind) {
  _readWaTemplateEditor();
  const max = _waEdit.max_arms || 4;
  if ((_waEdit.templates[kind] || []).length >= max) { toast(`${max} versions is the limit`, 'err'); return; }
  _waEdit.templates[kind] = [...(_waEdit.templates[kind] || []), ''];
  if (kind === 'opener') _waEdit.openerFrom.push(null);
  _drawWaTemplateEditor();
}

function removeWaArm(kind, idx) {
  _readWaTemplateEditor();
  const arms = [...(_waEdit.templates[kind] || [])];
  if (arms.length <= 1) return;
  if (!confirm(`Remove version ${WA_ARM_LABELS[idx]}? Messages already sent with it keep their results; `
             + 'leads still waiting to be sent move to another version when you save.')) return;
  arms.splice(idx, 1);
  _waEdit.templates[kind] = arms;
  if (kind === 'opener') _waEdit.openerFrom.splice(idx, 1);
  _drawWaTemplateEditor();
}

async function saveWaTemplates() {
  _readWaTemplateEditor();
  const templates = {};
  const openerFrom = [];
  for (const [kind, label] of WA_TEMPLATE_KINDS) {
    const arms = [];
    (_waEdit.templates[kind] || []).forEach((a, i) => {
      if (!(a || '').trim()) return;
      arms.push(a.trim());
      if (kind === 'opener') openerFrom.push(_waEdit.openerFrom[i] ?? null);
    });
    if (!arms.length) { toast(`"${label}" needs at least one message`, 'err'); return; }
    templates[kind] = arms;
  }
  const res = await api(`/api/wa/campaigns/${_waEdit.id}`, 'PATCH', {
    name: _waEdit.name, country: _waEdit.country, followup_days: _waEdit.followup_days,
    notes: _waEdit.notes, templates, variables: _waEdit.variables, opener_from: openerFrom,
  });
  if (!res || res.error) { toast((res && res.error) || 'Could not save', 'err'); return; }
  toast('Saved ✓ — waiting leads use it now');
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
  await Promise.all([loadWaCampaigns(), loadCountries()]);
  const active = _waCampaigns.filter(c => c.status !== 'archived');
  const selected = campaignId || _waFilter() || (active[0] && active[0].id) || '';
  document.getElementById('wa-add-campaign-wrap').innerHTML =
    campaignSelectHtml('wa-add-campaign', active, { selected });
  const chosen = active.find(c => String(c.id) === String(selected));
  document.getElementById('wa-add-country-wrap').innerHTML =
    countryPickerHtml('wa-import-country', (chosen && chosen.country) || _countries.used[0] || 'AE');
  const sel = document.getElementById('wa-add-campaign');
  sel.addEventListener('change', _waAddCampaignChanged);
  if (!active.length) { sel.value = '__new'; sel.dispatchEvent(new Event('change')); }
  _waAddSelected.clear();
  const search = document.getElementById('wa-add-search');
  if (search) search.value = '';
  openModal('modal-import-wa');
  setWaAddTab('existing');
}

function _waAddCampaignChanged() {
  const c = _waCampaigns.find(x => String(x.id) === document.getElementById('wa-add-campaign').value);
  if (c && c.country) setCountryPicker('wa-import-country', c.country);
}

function setWaAddTab(tab) {
  _waAddTab = tab;
  ['existing', 'json', 'import'].forEach(t => {
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

// The shape to hand a chat that's writing the copy. Kept here rather than in
// the placeholder so it can be copied in one click -- a spec that has to be
// retyped is a spec that drifts, and a drifted key imports as nothing.
const WA_JSON_SPEC = `Return a JSON array. One object per business, using exactly these keys:

[
  {
    "company":     "the business name",
    "phone":       "their phone number, as listed",
    "city":        "city",
    "website":     "their site, if they have one",
    "message":     "the opening WhatsApp message",
    "followup_1":  "first follow-up, sent a few days later",
    "followup_2":  "second follow-up",
    "followup_3":  "third follow-up"
  }
]

Rules:
- Keys exactly as spelled above, lowercase. Anything else is ignored.
- company and phone are required. Every other key is optional -- leave one
  out and that message falls back to the campaign's template.
- Write all four messages together so the follow-ups build on the opener
  rather than repeating it.
- Plain text only. No markdown, no emoji-only lines, no placeholders in
  curly braces unless you mean them to be filled in.
- Return only the JSON array. No commentary before or after.`;

function copyWaJsonSpec() {
  navigator.clipboard.writeText(WA_JSON_SPEC)
    .then(() => toast('Format copied — paste it into your chat'))
    .catch(() => toast('Could not copy it', 'err'));
}

// Reports what arrived, per slot. A model asked for four messages across a
// few hundred leads quietly drops some, and a follow-up that fell back to the
// template is worth seeing now rather than three weeks into the cadence.
function describeWaDrafts(d) {
  if (!d) return '';
  const bits = [];
  if (d.opener) bits.push(`${d.opener} opener${d.opener === 1 ? '' : 's'}`);
  (d.followups || []).forEach((n, i) => { if (n) bits.push(`${n} follow-up ${i + 1}`); });
  if (!bits.length) return '';
  let out = ` · ${bits.join(' · ')}`;
  if (d.kept_edits) out += ` · ${d.kept_edits} kept your own edits`;
  if (d.no_phone) out += ` · ${d.no_phone} had no phone, so their copy wasn't kept`;
  if (d.too_long) out += ` · ${d.too_long} message${d.too_long === 1 ? '' : 's'} too long, skipped`;
  return out;
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

async function submitWaAdd() {
  if (_waAddTab === 'existing' && !_waAddSelected.size) { toast('Select at least one lead', 'err'); return; }
  const country = countryValue('wa-import-country');
  if (!country) { toast('Pick the country from the list', 'err'); return; }
  const campaignId = await resolveCampaignSelect('wa-add-campaign', '/api/wa/campaigns', { country });
  if (!campaignId) { if (campaignId === '') toast('Pick the campaign these leads go into', 'err'); return; }
  if (_waAddTab === 'import') return importWaLeads(campaignId, country);
  if (_waAddTab === 'json') return importWaJson(campaignId, country);

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
  loadCountries(true);
  if (document.getElementById('section-whatsapp').classList.contains('active')) {
    loadWaCampaigns().then(_reloadWaView);
  }
}

// Opens the Scraper already aimed at WhatsApp and this campaign, so what it
// finds lands here and nowhere else.
function scrapeForWhatsApp() {
  const country = countryValue('wa-import-country') || 'AE';
  const sel = document.getElementById('wa-add-campaign');
  const campaign_id = sel && sel.value !== '__new' ? sel.value : '';
  closeModal('modal-import-wa');
  window._scraperPreset = { destination: 'whatsapp', country, campaign_id };
  showSection('scraper');
}

async function importWaLeads(campaignId, country) {
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

// Leads that arrive with their own copy. Parsed here rather than posted raw
// so a malformed paste is a message about the paste, not a 400 from the
// server -- and so a bare array and {"rows": [...]} both reach the API in the
// one shape it needs alongside the country and campaign.
async function importWaJson(campaignId, country) {
  const raw = (document.getElementById('wa-import-json').value || '').trim();
  if (!raw) { toast('Paste the JSON first', 'err'); return; }

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    toast(`That isn't valid JSON — ${e.message}`, 'err');
    return;
  }
  const rows = Array.isArray(parsed) ? parsed : parsed && parsed.rows;
  if (!Array.isArray(rows) || !rows.length) {
    toast('Expected an array of rows, or {"rows": [...]}', 'err');
    return;
  }
  if (rows.some(r => !r || typeof r !== 'object' || Array.isArray(r))) {
    toast('Every row must be an object with named keys like "company"', 'err');
    return;
  }
  const usable = rows.filter(r => (r.company || r.name || '').toString().trim()
                                || (r.phone || '').toString().trim());
  if (!usable.length) { toast('No row had a company or a phone number', 'err'); return; }

  const first = await api('/api/wa/import', 'POST',
    { rows: usable, country, wa_campaign_id: campaignId });
  await _finishWaImport(first, country, campaignId);
}

async function _finishWaImport(first, country, campaignId) {
  if (!first || first.error) { toast((first && first.error) || 'Import failed', 'err'); return; }
  const final = await confirmChannelConflicts(first, () =>
    api('/api/wa/import', 'POST', {
      rows: first.conflicts.map(c => c.row), country, wa_campaign_id: campaignId, confirm_conflicts: true,
    })
  );
  const inserted = (first.inserted || 0) + (final !== first ? (final.inserted || 0) : 0);
  toast(`Imported ${inserted} lead${inserted === 1 ? '' : 's'} ✓ — ready to send`
        + describeWaDrafts(_mergeWaDrafts(first, final)));
  closeModal('modal-import-wa');
  _afterWaAdd();
  notifyCrossOwnerOverlap(first);
}

// Held-back conflicts come back as a second import, so the two reports are
// added together before anything is shown -- otherwise the counts describe
// only the first half of what was actually brought in.
function _mergeWaDrafts(first, final) {
  const a = (first && first.drafts) || null;
  const b = final !== first ? ((final && final.drafts) || null) : null;
  if (!a) return b;
  if (!b) return a;
  return {
    opener: a.opener + b.opener,
    followups: (a.followups || []).map((n, i) => n + ((b.followups || [])[i] || 0)),
    too_long: a.too_long + b.too_long,
    kept_edits: a.kept_edits + b.kept_edits,
    no_phone: a.no_phone + b.no_phone,
  };
}
