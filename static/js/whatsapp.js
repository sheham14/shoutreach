// ── WhatsApp ─────────────────────────────────────────────────────────────────
//
// Sending is always manual. Every action in this file either reads state or
// stages one (a signal, a draft, a sent-date) -- the only thing that ever
// reaches WhatsApp is the operator's own tap on Send, after this code opens
// a wa.me link and gets out of the way. If you're tempted to make "Open in
// WhatsApp" fire automatically on a schedule, don't -- see
// docs/WhatsApp Module Handover.md for why that's off the table.

let _waBucket   = 'review';
let _waLeads    = [];
let _waTemplates = null;

const WA_SIGNAL_LABELS = {
  gap_found: 'Gap found', no_gap: 'No gap', unclear: 'Unclear',
};

async function loadWhatsApp() {
  await loadWaTemplates();
  await refreshWaCounts();
  await loadWaBucket();
}

// Four tiles, not eight — the full breakdown is still one tap away as the
// bucket tabs below, so the summary's job is just "how much is waiting on
// me," not a second copy of every number on the page.
async function loadWaSummary() {
  const s = await api('/api/wa/summary') || {};
  const tiles = [
    ['Total leads',    s.total],
    ['Needs review',   s.awaiting_review],
    ['Ready to send',  s.ready_to_send],
    ['Replied',        s.replied],
  ];
  document.getElementById('wa-summary').innerHTML = tiles.map(([label, value]) => `
    <div class="stat-card">
      <div class="stat-label">${label}</div>
      <div class="stat-value" style="font-size:22px">${value ?? 0}</div>
    </div>`).join('');
}

async function refreshWaCounts() {
  await loadWaSummary();
  const [review, ready, due, cadence, confirmed] = await Promise.all([
    api('/api/wa/leads?status=signal_ready&limit=1000'),
    api('/api/wa/leads?status=drafted&limit=1000'),
    api('/api/wa/followups-due'),
    api('/api/wa/leads?status=sent&limit=1000'),
    api('/api/wa/leads?status=confirmed&limit=1000'),
  ]);
  document.getElementById('wa-count-review').textContent   = (review || []).length;
  document.getElementById('wa-count-ready').textContent    = (ready || []).length;
  document.getElementById('wa-count-due').textContent      = (due || []).length;
  document.getElementById('wa-count-cadence').textContent  = (cadence || []).length;
  document.getElementById('wa-count-confirmed').textContent = (confirmed || []).length;
  const draftBtn = document.getElementById('wa-draft-batch-btn');
  draftBtn.style.display = (confirmed || []).length ? 'inline-flex' : 'none';
}

function setWaBucket(bucket) {
  _waBucket = bucket;
  ['review', 'ready', 'due', 'cadence', 'all'].forEach(b => {
    const el = document.getElementById(`wa-tab-${b}`);
    if (!el) return;
    el.classList.toggle('btn-primary', b === bucket);
    el.classList.toggle('btn-ghost', b !== bucket);
  });
  loadWaBucket();
}

async function loadWaBucket() {
  const titles = {
    review: 'Needs review', ready: 'Ready to send', due: 'Follow-up due',
    cadence: 'Waiting for reply', all: 'All leads',
  };
  document.getElementById('wa-list-title').textContent = titles[_waBucket] || 'Leads';

  if (_waBucket === 'due') {
    _waLeads = await api('/api/wa/followups-due') || [];
  } else {
    const statusByBucket = { review: 'signal_ready', ready: 'drafted', cadence: 'sent', all: '' };
    const status = statusByBucket[_waBucket];
    const q = status ? `?status=${status}` : '';
    _waLeads = await api(`/api/wa/leads${q}`) || [];
  }
  document.getElementById('wa-list-count').textContent =
    `${_waLeads.length} lead${_waLeads.length === 1 ? '' : 's'}`;
  _renderWaList();
}

function _waStatusBadge(l) {
  if (l.moved_to) return `<span class="badge badge-gray">Moved to ${esc(l.moved_to === 'call' ? 'Calling' : 'Email')}</span>`;
  if (l.replied)  return `<span class="badge badge-green">Replied</span>`;
  if (l.paused)   return `<span class="badge badge-amber">Paused</span>`;
  const map = {
    '': 'Checking their website…', signal_ready: 'Needs your review',
    confirmed: 'Being written up', drafted: 'Ready to send', sent: 'Sent, waiting',
  };
  return `<span class="badge badge-gray">${esc(map[l.wa_status] || l.wa_status || '—')}</span>`;
}

function _renderWaList() {
  const wrap = document.getElementById('wa-list');
  if (!_waLeads.length) {
    wrap.innerHTML = `<div class="empty-state"><p>Nothing here right now</p></div>`;
    return;
  }
  wrap.innerHTML = _waLeads.map(l => _renderWaCard(l)).join('');
}

function _cardTop(l) {
  const bits = [];
  if (l.rating != null) bits.push(`${esc(l.rating)}★`);
  if (l.website) bits.push(`<a href="${esc(l.website)}" target="_blank" rel="noopener" style="color:var(--blue)">website</a>`);
  return `
    <div class="wa-card-top">
      <div>
        <div class="wa-card-name">${esc(l.company || 'Unnamed business')}</div>
        ${bits.length ? `<div class="wa-card-meta">${bits.join(' &nbsp;·&nbsp; ')}</div>` : ''}
      </div>
      ${_waStatusBadge(l)}
    </div>
    <div class="wa-card-phone">📞 ${esc(l.wa_number || 'No number')}${l.number_type === 'landline'
      ? ' <span class="landline-note">(landline — may not have WhatsApp)</span>' : ''}</div>`;
}

function _moveButton(l) {
  return `<button class="btn btn-ghost btn-sm" onclick="moveWaLead(${l.id})">Not on WhatsApp</button>`;
}

function _renderWaCard(l) {
  const top = _cardTop(l);

  if (_waBucket === 'review' || (!l.signal_confirmed && l.wa_status === 'signal_ready')) {
    return `<div class="wa-card">
      ${top}
      <div class="wa-card-row">
        <select id="wa-sig-type-${l.id}" style="background:var(--bg3);border:1px solid var(--border2);
                border-radius:6px;padding:7px 10px;color:var(--text);font-size:13px;font-family:var(--font)">
          ${['gap_found', 'no_gap', 'unclear'].map(t =>
            `<option value="${t}" ${l.signal_type === t ? 'selected' : ''}>${WA_SIGNAL_LABELS[t]}</option>`).join('')}
        </select>
        ${infoDot("gap found = their website has no visible way to book online, which is the whole pitch. "
          + "no gap = they already have online booking, so a compliment message goes out instead. "
          + "unclear = their site didn't load properly or had almost nothing on it — open the website "
          + "link above and check by hand before picking one.")}
      </div>
      <div class="wa-card-body">
        <textarea id="wa-sig-detail-${l.id}"
                  placeholder="What did you actually see on their site?">${esc(l.signal_detail || '')}</textarea>
      </div>
      <div class="wa-card-actions">
        <button class="btn btn-primary btn-sm" onclick="confirmWaSignal(${l.id})">Confirm</button>
        ${_moveButton(l)}
      </div>
    </div>`;
  }

  if (_waBucket === 'due') {
    return `<div class="wa-card">
      ${top}
      <div class="wa-card-body">
        <textarea id="wa-msg-${l.id}">${esc(l.followup_draft || '')}</textarea>
      </div>
      <div class="wa-card-actions">
        <button class="btn btn-primary btn-sm" onclick="openWaLink(${l.id}, 'followup')">Open in WhatsApp</button>
        ${_moveButton(l)}
      </div>
      <div class="wa-card-footnote">${l.followup_count || 0} follow-up${l.followup_count === 1 ? '' : 's'} sent so far</div>
    </div>`;
  }

  if (l.wa_status === 'drafted' || l.wa_status === 'sent' || l.replied) {
    const sentInfo = l.sent_date
      ? `<div class="wa-card-footnote">Opened ${esc(l.sent_date.substring(0, 16))}
           <a href="#" onclick="event.preventDefault();correctWaSentDate(${l.id})" style="color:var(--blue)">didn't actually send?</a></div>`
      : '';
    const repliedToggle = (l.wa_status === 'sent' || l.replied)
      ? `<label style="display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted);cursor:pointer">
           <input type="checkbox" ${l.replied ? 'checked' : ''} onchange="toggleWaReplied(${l.id}, this.checked)" />
           They replied
         </label>`
      : '';
    const sendBtn = l.wa_status === 'drafted'
      ? `<button class="btn btn-primary btn-sm" onclick="openWaLink(${l.id}, 'opener')">Open in WhatsApp</button>`
      : '';
    const pauseBtn = !l.replied
      ? `<button class="btn btn-ghost btn-sm" onclick="toggleWaPaused(${l.id}, ${!l.paused})">${l.paused ? 'Resume follow-ups' : 'Pause follow-ups'}</button>
         ${infoDot("Pausing keeps the lead on file but stops it from ever showing up under “Follow-up due” "
           + "again, until you resume it or they reply. Use it for “not now, maybe later” — for "
           + "“never contact them again” use Not on WhatsApp or delete the lead instead.")}`
      : '';
    return `<div class="wa-card">
      ${top}
      <div class="wa-card-body">
        <textarea id="wa-msg-${l.id}">${esc(l.draft_message || '')}</textarea>
      </div>
      <div class="wa-card-actions">${sendBtn} ${pauseBtn} ${repliedToggle} ${!l.replied ? _moveButton(l) : ''}</div>
      ${sentInfo}
    </div>`;
  }

  // '' (awaiting signal) or 'confirmed' (awaiting draft) or moved -- nothing
  // to act on here yet.
  const note = l.signal_detail || (l.wa_status === '' ? 'Checked automatically, usually within a few minutes.' : '');
  return `<div class="wa-card">
    ${top}
    ${note ? `<div class="text-muted text-small" style="margin-bottom:8px">${esc(note)}</div>` : ''}
    ${l.moved_to ? '' : `<div class="wa-card-actions">${_moveButton(l)}</div>`}
  </div>`;
}

async function confirmWaSignal(id) {
  const signal_type = document.getElementById(`wa-sig-type-${id}`).value;
  const signal_detail = document.getElementById(`wa-sig-detail-${id}`).value.trim();
  if (!signal_detail) { toast('Add what you actually saw before confirming', 'err'); return; }
  const res = await api(`/api/wa/leads/${id}/confirm`, 'POST', { signal_type, signal_detail });
  if (!res || res.error) { toast((res && res.error) || 'Could not confirm', 'err'); return; }
  toast('Signal confirmed');
  await refreshWaCounts();
  loadWaBucket();
}

async function runWaDraftBatch() {
  const res = await api('/api/wa/draft-batch', 'POST', {});
  if (!res || res.error) { toast((res && res.error) || 'Could not draft messages', 'err'); return; }
  const note = res.note ? ` — ${res.note}` : '';
  toast(`Drafted ${res.drafted} message${res.drafted === 1 ? '' : 's'}${note}`);
  await refreshWaCounts();
  loadWaBucket();
}

// Opens the wa.me link with whatever is currently in the row's textarea --
// so an edit made just before clicking is what actually gets sent, not
// whatever was drafted originally. Marks it sent immediately after the tap;
// see mark_wa_sent's own comment on why that's an approximation, not proof.
function openWaLink(id, kind) {
  const lead = _waLeads.find(l => l.id === id);
  if (!lead || !lead.wa_number) { toast('No WhatsApp number on file for this lead', 'err'); return; }
  const message = document.getElementById(`wa-msg-${id}`).value;
  const url = `https://wa.me/${lead.wa_number}?text=${encodeURIComponent(message)}`;
  window.open(url, '_blank', 'noopener');
  api(`/api/wa/leads/${id}/sent`, 'POST', { kind, message }).then(() => {
    refreshWaCounts();
    loadWaBucket();
  });
}

async function correctWaSentDate(id) {
  const current = prompt(
    "Didn't actually send? Clear the date, or enter the real one (YYYY-MM-DD HH:MM).\n\nLeave blank to clear.",
    ''
  );
  if (current === null) return;
  const sent_date = current.trim() || null;
  const res = await api(`/api/wa/leads/${id}/sent-date`, 'PUT', { sent_date });
  if (!res || res.error) { toast((res && res.error) || 'Could not update', 'err'); return; }
  toast(sent_date ? 'Sent date updated' : 'Sent date cleared');
  await refreshWaCounts();
  loadWaBucket();
}

async function toggleWaReplied(id, replied) {
  const res = await api(`/api/wa/leads/${id}/replied`, 'POST', { replied });
  if (!res || res.error) { toast((res && res.error) || 'Could not update', 'err'); return; }
  toast(replied ? 'Marked replied — out of the follow-up cadence' : 'Reopened');
  await refreshWaCounts();
  loadWaBucket();
}

async function toggleWaPaused(id, paused) {
  const res = await api(`/api/wa/leads/${id}/pause`, 'POST', { paused });
  if (!res || res.error) { toast((res && res.error) || 'Could not update', 'err'); return; }
  toast(paused ? 'Paused — no more follow-ups until resumed' : 'Resumed');
  await refreshWaCounts();
  loadWaBucket();
}

async function moveWaLead(id) {
  const dest = prompt('Move this lead to "call" or "email"?', 'call');
  if (!dest) return;
  const destination = dest.trim().toLowerCase();
  if (!['call', 'email'].includes(destination)) { toast('Type "call" or "email"', 'err'); return; }
  if (!confirm(`Move this lead to ${destination === 'call' ? 'Calling' : 'Email'}? It will drop out of the WhatsApp cadence.`)) return;
  const res = await api(`/api/wa/leads/${id}/move`, 'POST', { destination });
  if (!res || res.error) { toast((res && res.error) || 'Could not move it', 'err'); return; }
  toast(`Moved to ${destination === 'call' ? 'Calling' : 'Email'}`);
  await refreshWaCounts();
  loadWaBucket();
}

// ── Templates ────────────────────────────────────────────────────────────────

const WA_ARM_LABELS = ['A', 'B', 'C', 'D'];

const WA_TEMPLATE_KINDS = [
  ['gap',      'Gap found (no online booking seen)'],
  ['no_gap',   'No gap (they already have online booking)'],
  ['followup', 'Follow-up'],
];

async function loadWaTemplates() {
  _waTemplates = (await api('/api/wa/templates')) || {};
}

function toggleWaTemplateEditor() {
  const wrap = document.getElementById('wa-template-editor');
  const show = wrap.style.display === 'none';
  wrap.style.display = show ? 'block' : 'none';
  if (show) _renderWaTemplateEditor();
}

// Whatever is currently typed into the editor, so re-rendering it (to add or
// remove a version) never throws away an unsaved edit.
function _readWaTemplateEditor() {
  const t = _waTemplates || {};
  const out = {
    followup_days: parseInt(document.getElementById('wa-followup-days')?.value, 10) || 3,
  };
  WA_TEMPLATE_KINDS.forEach(([kind]) => {
    out[kind] = (t[kind] || ['']).map(
      (arm, i) => document.getElementById(`wa-tpl-${kind}-${i}`)?.value ?? arm
    );
  });
  return out;
}

function addWaArm(kind) {
  _waTemplates = { ..._waTemplates, ..._readWaTemplateEditor() };
  const max = _waTemplates.max_arms || 4;
  if ((_waTemplates[kind] || []).length >= max) {
    toast(`${max} versions is the limit`, 'err');
    return;
  }
  _waTemplates[kind] = [...(_waTemplates[kind] || []), ''];
  _renderWaTemplateEditor();
}

function removeWaArm(kind, idx) {
  _waTemplates = { ..._waTemplates, ..._readWaTemplateEditor() };
  const arms = [...(_waTemplates[kind] || [])];
  if (arms.length <= 1) return;
  // Leads already sent keep the label they were drafted under, so removing a
  // version stops it being used from now on without rewriting what happened.
  if (!confirm(`Remove version ${WA_ARM_LABELS[idx]}? Messages already sent with it keep their results.`)) return;
  arms.splice(idx, 1);
  _waTemplates[kind] = arms;
  _renderWaTemplateEditor();
}

function _waStatsFor(label) {
  return ((_waTemplates || {}).stats || []).filter(s => s.arm === label);
}

function _renderWaArmStats(label) {
  const rows = _waStatsFor(label);
  if (!rows.length) return '';
  const parts = rows.map(r => {
    const how = r.paraphrased ? 'AI-reworded' : 'as written';
    return `${r.sent} sent ${how} · ${r.replied} replied (${r.reply_rate}%)`;
  });
  return `<div class="text-muted text-small" style="margin-top:4px">${esc(parts.join('  |  '))}</div>`;
}

function _renderWaTemplateEditor() {
  const wrap = document.getElementById('wa-template-editor');
  const t = _waTemplates || {};

  const kinds = WA_TEMPLATE_KINDS.map(([kind, label]) => {
    const arms = (t[kind] && t[kind].length) ? t[kind] : [''];
    const testing = arms.length > 1;
    const boxes = arms.map((arm, i) => `
      <div style="margin-bottom:8px">
        ${testing ? `<div class="flex items-center" style="margin-bottom:4px">
          <span class="badge badge-blue">Version ${WA_ARM_LABELS[i]}</span>
          <button class="btn btn-ghost btn-sm ml-auto" onclick="removeWaArm('${kind}', ${i})">Remove</button>
        </div>` : ''}
        <textarea id="wa-tpl-${kind}-${i}" style="min-height:${kind === 'followup' ? 60 : 80}px">${esc(arm)}</textarea>
        ${testing ? _renderWaArmStats(WA_ARM_LABELS[i]) : ''}
      </div>`).join('');

    return `
      <div class="form-group">
        <label>${esc(label)}</label>
        ${boxes}
        <button class="btn btn-ghost btn-sm" onclick="addWaArm('${kind}')">
          ${testing ? '+ Add another version' : '+ Test a second version'}
        </button>
      </div>`;
  }).join('');

  const untested = _renderWaArmStats('-');

  wrap.innerHTML = `
    <div class="card" style="padding:18px">
      <div class="flex items-center" style="margin-bottom:10px">
        <div class="card-title">Your message templates</div>
        ${infoDot('These are your own messages. Everyone using this app writes their own, and nobody else can see or change yours.')}
      </div>
      <p class="text-muted text-small" style="margin-bottom:14px">
        Placeholders: <span class="mono">{{business_name}}</span> and <span class="mono">{{signal_detail}}</span>
        (the confirmed observation — not used in the follow-up). These are what gets drafted for every
        lead, then optionally reworded by AI for variety — the placeholders are always filled in first.
      </p>
      <div class="text-muted text-small" style="margin-bottom:16px;padding:10px 12px;background:var(--bg3);border-radius:6px">
        <strong>Testing two versions:</strong> add a second version of any message and new leads are
        dealt out between them, one after the other. Reply rates appear under each once messages
        have gone out, so you can keep the one that actually works. A lead keeps the version it
        started on, follow-ups included.
      </div>
      ${kinds}
      ${untested ? `<div class="form-group">
        <label>Sent before you started testing</label>${untested}</div>` : ''}
      <div class="form-group">
        <label>Follow-up interval (days)</label>
        <input type="number" id="wa-followup-days" min="1" style="max-width:120px"
               value="${esc(t.followup_days ?? 3)}" />
        <div class="form-hint">A sent lead surfaces under "Follow-up due" this many days after its last send, forever, until replied or paused. This is your own setting.</div>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-primary" onclick="saveWaTemplates()">Save</button>
        <button class="btn btn-ghost" onclick="toggleWaTemplateEditor()">Close</button>
      </div>
    </div>`;
}

async function saveWaTemplates() {
  const edited = _readWaTemplateEditor();
  const templates = {};
  for (const [kind, label] of WA_TEMPLATE_KINDS) {
    const arms = (edited[kind] || []).map(a => (a || '').trim()).filter(Boolean);
    if (!arms.length) {
      toast(`"${label}" needs at least one message`, 'err');
      return;
    }
    templates[kind] = arms;
  }
  const res = await api('/api/wa/templates', 'PUT',
                        { templates, followup_days: edited.followup_days });
  if (!res || res.error) { toast((res && res.error) || 'Could not save', 'err'); return; }
  toast('Templates saved');
  await loadWaTemplates();
  toggleWaTemplateEditor();
}

// ── Import ────────────────────────────────────────────────────────────────────

// One "Add leads" door with the same routes in as Calling: pick from leads you
// already have, bring a CSV or paste, or go and scrape new ones.
let _waAddTab = 'existing';
let _waAddSelected = new Set();
let _waAddRows = [];
let _waAddTotal = 0;
let _waAddTimer = null;

function openWaImportModal(tab = 'existing') {
  _waAddSelected.clear();
  const search = document.getElementById('wa-add-search');
  if (search) search.value = '';
  openModal('modal-import-wa');
  setWaAddTab(tab);
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

async function submitWaAdd() {
  if (_waAddTab === 'import') return importWaLeads();
  if (!_waAddSelected.size) { toast('Select at least one lead', 'err'); return; }

  const country = document.getElementById('wa-import-country').value;
  const first = await api('/api/wa/add-existing', 'POST',
                          { business_ids: [..._waAddSelected], country });
  if (!first || first.error) { toast((first && first.error) || 'Could not add them', 'err'); return; }
  const final = await confirmChannelConflicts(first, () =>
    api('/api/wa/add-existing', 'POST', {
      business_ids: first.conflicts.map(c => c.business_id), country, confirm_conflicts: true,
    })
  );

  const total = k => (first[k] || 0) + (final !== first ? (final[k] || 0) : 0);
  const skipped = [
    total('no_phone')  && `${total('no_phone')} had no phone number`,
    total('ruled_out') && `${total('ruled_out')} were already ruled out as not on WhatsApp`,
    total('opted_out') && `${total('opted_out')} asked not to be contacted`,
    total('already')   && `${total('already')} were already on WhatsApp`,
  ].filter(Boolean);
  toast(`Added ${total('added')} to WhatsApp` + (skipped.length ? ` — ${skipped.join(', ')}` : ''));
  closeModal('modal-import-wa');
  await refreshWaCounts();
  loadWaBucket();
}

// Opens the Scraper already aimed at WhatsApp, so what it finds lands here and
// nowhere else. The country carries across so it doesn't have to be picked twice.
function scrapeForWhatsApp() {
  const country = document.getElementById('wa-import-country')?.value || 'AE';
  closeModal('modal-import-wa');
  window._scraperPreset = { destination: 'whatsapp', country };
  showSection('scraper');
}

async function importWaLeads() {
  const country = document.getElementById('wa-import-country').value;
  const fileInput = document.getElementById('wa-import-file');
  const paste = document.getElementById('wa-import-paste').value.trim();

  if (fileInput.files.length) {
    const form = new FormData();
    form.append('file', fileInput.files[0]);
    form.append('country', country);
    const csrf = await _getCsrfToken();
    const res = await fetch('/api/wa/import', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRF-Token': csrf }, body: form,
    });
    if (res.status === 401) { window.location.href = '/login'; return; }
    const first = await res.json();
    await _finishWaImport(first, country);
    return;
  }

  if (paste) {
    const rows = paste.split('\n').map(l => l.trim()).filter(Boolean).map(line => {
      const [company, phone, website] = line.split(',').map(x => (x || '').trim());
      return { company, phone: phone || '', website: website || '' };
    }).filter(r => r.company);
    if (!rows.length) { toast('Nothing to import — one business per line', 'err'); return; }
    const first = await api('/api/wa/import', 'POST', { rows, country });
    await _finishWaImport(first, country);
    return;
  }

  toast('Select a file or paste some businesses', 'err');
}

async function _finishWaImport(first, country) {
  if (!first || first.error) { toast((first && first.error) || 'Import failed', 'err'); return; }
  const final = await confirmChannelConflicts(first, () =>
    api('/api/wa/import', 'POST', {
      rows: first.conflicts.map(c => c.row), country, confirm_conflicts: true,
    })
  );
  const inserted = (first.inserted || 0) + (final !== first ? (final.inserted || 0) : 0);
  toast(`Imported ${inserted} lead${inserted === 1 ? '' : 's'} ✓`);
  closeModal('modal-import-wa');
  await refreshWaCounts();
  loadWaBucket();
  notifyCrossOwnerOverlap(first);
}
