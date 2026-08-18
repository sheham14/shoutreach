// ── Cold calling ─────────────────────────────────────────────────────────────
//
// Built as a working queue rather than a CRM screen. The default view is what
// to do next -- callbacks due, then leads never called -- because deciding who
// to ring is friction at exactly the moment momentum matters, and "Save & next
// lead" keeps you in the loop instead of returning to a table between calls.

let _callBucket   = 'today';
let _callLeads    = [];
let _callOutcomes = [];
let _callLead     = null;      // the lead currently on screen
let _callScript   = null;
let _attemptLimit = 6;
let _callCampaigns = [];
let _callCampaignId = '';   // '' = every lead, ignoring campaigns

async function loadCalling() {
  await Promise.all([loadCallScript(), loadCallSources(), loadCallCampaigns()]);
  await loadCallQueue();
}

// ── Campaigns ────────────────────────────────────────────────────────────────
//
// A batch you decided to work ("10 clinics, St John's"), as distinct from a
// lead list, which groups by whenever the scrape happened to run.

async function loadCallCampaigns() {
  _callCampaigns = await api('/api/call-campaigns') || [];
  const sel = document.getElementById('cq-campaign');
  if (sel) {
    sel.innerHTML = '<option value="">All leads (no campaign)</option>' +
      _callCampaigns.map(c =>
        `<option value="${c.id}">${esc(c.name)} — ${c.remaining} left of ${c.total}</option>`
      ).join('');
    sel.value = _callCampaignId;
  }
  _renderCampaignCards();
  const del = document.getElementById('cq-campaign-delete');
  if (del) del.style.display = _callCampaignId ? 'inline-flex' : 'none';
}

function _renderCampaignCards() {
  const wrap = document.getElementById('cq-campaign-cards');
  if (!wrap) return;
  if (!_callCampaigns.length) {
    wrap.innerHTML = '<div class="text-muted text-small">No campaigns yet. Create one, then add leads from Contacts.</div>';
    return;
  }
  wrap.innerHTML = _callCampaigns.map(c => {
    const on = String(c.id) === String(_callCampaignId);
    const pct = c.total ? Math.round((c.closed / c.total) * 100) : 0;
    return `<div onclick="setCallCampaign('${c.id}')"
      style="cursor:pointer;border:1px solid ${on ? 'var(--accent)' : 'var(--border2)'};
             border-radius:8px;padding:12px;background:${on ? 'rgba(96,165,250,.06)' : 'transparent'}">
      <div style="font-size:13px;font-weight:600;margin-bottom:6px">${esc(c.name)}</div>
      <div style="height:5px;background:var(--bg3);border-radius:3px;overflow:hidden;margin-bottom:8px">
        <div style="height:100%;width:${pct}%;background:var(--green)"></div>
      </div>
      <div class="text-muted" style="font-size:11px;line-height:1.7">
        <div>${c.remaining} left of ${c.total}</div>
        <div>${c.due} due now · ${c.uncalled} never called</div>
        <div style="color:var(--green)">${c.booked} booked</div>
      </div>
    </div>`;
  }).join('');
}

function setCallCampaign(id) {
  _callCampaignId = id ? String(id) : '';
  const sel = document.getElementById('cq-campaign');
  if (sel) sel.value = _callCampaignId;
  const del = document.getElementById('cq-campaign-delete');
  if (del) del.style.display = _callCampaignId ? 'inline-flex' : 'none';
  _renderCampaignCards();
  loadCallQueue();
}

async function openNewCallCampaign() {
  const name = prompt('Name this campaign — e.g. "Dental clinics, St Johns"');
  if (!name || !name.trim()) return;
  const res = await api('/api/call-campaigns', 'POST', { name: name.trim() });
  if (!res || res.error) { toast((res && res.error) || 'Could not create it', 'err'); return; }
  toast('Campaign created — add leads from Contacts');
  _callCampaignId = String(res.id);
  await loadCallCampaigns();
  loadCallQueue();
}

async function deleteCallCampaign() {
  if (!_callCampaignId) return;
  const c = _callCampaigns.find(x => String(x.id) === _callCampaignId);
  // Worth stating plainly: this removes the grouping, not the leads.
  if (!confirm(`Delete the campaign "${c ? c.name : ''}"?\n\n`
             + `The ${c ? c.total : 0} contacts and their call history are kept — `
             + `only the grouping goes.`)) return;
  const res = await api(`/api/call-campaigns/${_callCampaignId}`, 'DELETE');
  if (!res || res.error) { toast((res && res.error) || 'Could not delete', 'err'); return; }
  toast('Campaign deleted — leads kept');
  _callCampaignId = '';
  await loadCallCampaigns();
  loadCallQueue();
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
    const el = document.getElementById(`cq-tab-${b}`);
    if (!el) return;
    el.classList.toggle('btn-primary', b === bucket);
    el.classList.toggle('btn-ghost', b !== bucket);
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

  _renderCallTable();

  // Drop straight into the first lead so the common case is zero clicks --
  // except when reviewing closed-out leads, where there is nothing to dial and
  // opening one would just put a live outcome form in front of you.
  if (_callLeads.length && _callBucket !== 'worked') openCallLead(_callLeads[0].id);
  else {
    _callLead = null;
    document.getElementById('cq-lead-card').style.display = 'none';
    _renderScriptFor(null);
  }
}

function _renderCallTable() {
  const titles = { today: 'Due now', new: 'Never called', upcoming: 'Scheduled',
                   all: 'All callable', worked: 'Worked — closed out' };
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
                onclick="openCallLead(${l.id})">
      <td>${esc(l.company || l.email || '—')}${contactSignalPill(l)}</td>
      <td class="mono" style="font-size:12px">${esc(l.phone || '—')}</td>
      <td class="mono" style="font-size:12px${over ? ';color:var(--amber)' : ''}"
          ${over ? `title="Past ${_attemptLimit} attempts — probably time to let it go"` : ''}>${l.call_attempts}</td>
      <td class="mono text-muted" style="font-size:11px">${due}</td>
      <td>${_callBucket === 'worked'
            ? `${callStatusBadge(l.call_status)} <button class="btn btn-ghost btn-sm"
                 onclick="event.stopPropagation();reopenCallLead(${l.id})"
                 title="Put this lead back in the queue">↩ Reopen</button>`
            : `<button class="btn btn-ghost btn-sm" onclick="event.stopPropagation();openCallLead(${l.id})">Open</button>`}</td>
    </tr>`;
  }).join('');
}

async function openCallLead(id) {
  const data = await api(`/api/calls/contact/${id}`);
  if (!data || !data.contact) { toast('Could not load that lead', 'err'); return; }

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

  document.getElementById('cq-notes').focus();
}

let _chosenOutcome = null;

function _renderOutcomeButtons(selected) {
  _chosenOutcome = selected;
  const wrap = document.getElementById('cq-outcomes');
  wrap.innerHTML = _callOutcomes.map(o => {
    const on = o.key === selected;
    const danger = o.terminal && o.key !== 'booked';
    const colour = o.key === 'booked' ? 'var(--green)' : (danger ? 'var(--red)' : 'var(--blue)');
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
  if (o && o.wants_next_call) {
    wrap.style.display = 'block';
    label.textContent = o.key === 'booked' ? 'Meeting date & time' : 'Next call';
    hint.textContent  = o.key === 'booked'
      ? 'You can add this to your calendar after saving.'
      : 'Puts this lead back in "Due now" at that time.';
    if (!document.getElementById('cq-next-at').value) {
      // Default to tomorrow, same time — the common case for a callback.
      const d = new Date(Date.now() + 864e5);
      d.setSeconds(0, 0);
      document.getElementById('cq-next-at').value =
        new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 16);
    }
  } else {
    wrap.style.display = 'none';
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
  loadCallQueue();
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
  if (!sections.length) { body.innerHTML = '<div class="empty-state"><p>No script yet — press Edit script.</p></div>'; return; }

  // Each section collapses independently: mid-call you need to reach the right
  // objection in a second, not scroll a wall of text.
  body.innerHTML = sections.map((s, i) => `
    <details ${i === 0 ? 'open' : ''} style="margin-bottom:8px;border:1px solid var(--border);border-radius:6px">
      <summary style="cursor:pointer;padding:8px 12px;font-size:13px;font-weight:600;list-style:revert">${esc(s.title || 'Section')}</summary>
      <div style="padding:0 12px 10px;font-size:13px;line-height:1.55;white-space:pre-wrap;color:var(--text2)">${
        s.body ? esc(_fillScript(s.body, contact))
               : '<span class="text-muted">Empty — add your words under Edit script.</span>'}</div>
    </details>`).join('');
}

function toggleScriptEditor() {
  const el = document.getElementById('cq-script-editor');
  if (el.style.display !== 'none') { el.style.display = 'none'; return; }
  _renderScriptEditor();
  el.style.display = 'block';
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
        <button class="btn btn-ghost btn-sm" onclick="toggleScriptEditor()">Close</button>
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
      <textarea class="cq-sec-body" style="min-height:90px"
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
  toggleScriptEditor();
}
