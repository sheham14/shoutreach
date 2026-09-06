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

async function loadWaSummary() {
  const s = await api('/api/wa/summary') || {};
  const tiles = [
    ['Total',            s.total],
    ['Awaiting signal',  s.pending_signal],
    ['Needs review',     s.awaiting_review],
    ['Awaiting draft',   s.awaiting_draft],
    ['Ready to send',    s.ready_to_send],
    ['In cadence',       s.in_cadence],
    ['Replied',          s.replied],
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
    cadence: 'In cadence', all: 'All leads',
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
  _renderWaTable();
}

function _waSignalPill(l) {
  if (!l.signal_type) return '';
  const cls = l.signal_type === 'gap_found' ? 'badge-red'
            : l.signal_type === 'no_gap'    ? 'badge-green' : 'badge-amber';
  return `<span class="badge ${cls}" style="margin-left:6px;font-size:10px">${WA_SIGNAL_LABELS[l.signal_type] || l.signal_type}</span>`;
}

function _waStatusBadge(l) {
  if (l.moved_to) return `<span class="badge badge-gray">Moved to ${esc(l.moved_to === 'call' ? 'Calling' : 'Email')}</span>`;
  if (l.replied)  return `<span class="badge badge-green">Replied</span>`;
  if (l.paused)   return `<span class="badge badge-amber">Paused</span>`;
  const map = {
    '': 'Awaiting signal check', signal_ready: 'Needs review',
    confirmed: 'Awaiting draft', drafted: 'Ready to send', sent: 'Sent',
  };
  return `<span class="badge badge-gray">${esc(map[l.wa_status] || l.wa_status || '—')}</span>`;
}

function _renderWaTable() {
  const tbody = document.getElementById('wa-table');
  if (!_waLeads.length) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="empty-state"><p>Nothing here right now</p></div></td></tr>`;
    return;
  }
  tbody.innerHTML = _waLeads.map(l => _renderWaRow(l)).join('');
}

function _businessCell(l) {
  const bits = [];
  if (l.website) bits.push(`<a href="${esc(l.website)}" target="_blank" rel="noopener" style="color:var(--blue)">${esc(l.website)}</a>`);
  if (l.rating != null) bits.push(`${esc(l.rating)}★`);
  return `<div>${esc(l.company || '—')}</div>
          <div class="text-muted text-small">${bits.join(' &nbsp;·&nbsp; ')}</div>`;
}

function _moveButton(l) {
  return `<button class="btn btn-ghost btn-sm" onclick="moveWaLead(${l.id})"
            title="The number turned out not to be on WhatsApp">Not on WhatsApp</button>`;
}

function _renderWaRow(l) {
  const business = `<td>${_businessCell(l)}</td>`;
  const number   = `<td class="mono" style="font-size:12px">${esc(l.wa_number || '—')}${l.number_type === 'landline' ? ' <span class="text-muted" style="font-size:10px">(landline)</span>' : ''}</td>`;
  const status   = `<td>${_waStatusBadge(l)}</td>`;

  if (_waBucket === 'review' || (!l.signal_confirmed && l.wa_status === 'signal_ready')) {
    const middle = `<td>
      <select id="wa-sig-type-${l.id}" style="margin-bottom:6px;background:var(--bg3);border:1px solid var(--border2);
              border-radius:6px;padding:5px 8px;color:var(--text);font-size:12px;font-family:var(--font)">
        ${['gap_found', 'no_gap', 'unclear'].map(t =>
          `<option value="${t}" ${l.signal_type === t ? 'selected' : ''}>${WA_SIGNAL_LABELS[t]}</option>`).join('')}
      </select>
      <textarea id="wa-sig-detail-${l.id}" style="min-height:50px;font-size:12px"
                placeholder="What did you actually see on their site?">${esc(l.signal_detail || '')}</textarea>
    </td>`;
    const actions = `<td><button class="btn btn-primary btn-sm" onclick="confirmWaSignal(${l.id})">Confirm</button>${_moveButton(l)}</td>`;
    return `<tr>${business}${number}${middle}${status}${actions}</tr>`;
  }

  if (_waBucket === 'due') {
    const middle = `<td><textarea id="wa-msg-${l.id}" style="min-height:60px;font-size:13px">${esc(l.followup_draft || '')}</textarea></td>`;
    const actions = `<td>
      <button class="btn btn-primary btn-sm" onclick="openWaLink(${l.id}, 'followup')">Open in WhatsApp</button>
      <div class="text-muted text-small" style="margin-top:4px">${l.followup_count || 0} follow-up${l.followup_count === 1 ? '' : 's'} so far</div>
      ${_moveButton(l)}
    </td>`;
    return `<tr>${business}${number}${middle}${status}${actions}</tr>`;
  }

  if (l.wa_status === 'drafted' || l.wa_status === 'sent' || l.replied) {
    const middle = `<td><textarea id="wa-msg-${l.id}" style="min-height:60px;font-size:13px">${esc(l.draft_message || '')}</textarea></td>`;
    const sentInfo = l.sent_date
      ? `<div class="text-muted text-small">Sent ${esc(l.sent_date.substring(0, 16))}
           <a href="#" onclick="event.preventDefault();correctWaSentDate(${l.id})" style="color:var(--blue)">(fix)</a></div>`
      : '';
    const repliedToggle = l.wa_status === 'sent' || l.replied
      ? `<label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);cursor:pointer;margin-top:4px">
           <input type="checkbox" ${l.replied ? 'checked' : ''} onchange="toggleWaReplied(${l.id}, this.checked)" />
           Replied
         </label>`
      : '';
    const sendBtn = l.wa_status === 'drafted'
      ? `<button class="btn btn-primary btn-sm" onclick="openWaLink(${l.id}, 'opener')">Open in WhatsApp</button>`
      : '';
    const pauseBtn = !l.replied
      ? `<button class="btn btn-ghost btn-sm" onclick="toggleWaPaused(${l.id}, ${!l.paused})">${l.paused ? 'Resume' : 'Pause'}</button>`
      : '';
    const actions = `<td>${sendBtn} ${pauseBtn}${sentInfo}${repliedToggle}${!l.replied ? _moveButton(l) : ''}</td>`;
    return `<tr>${business}${number}${middle}${status}${actions}</tr>`;
  }

  // '' (awaiting signal) or 'confirmed' (awaiting draft) or moved -- nothing
  // to act on here yet.
  const middle = `<td class="text-muted text-small">${esc(l.signal_detail || (l.wa_status === '' ? 'Checked automatically, usually within a few minutes' : ''))}</td>`;
  const actions = `<td>${l.moved_to ? '' : _moveButton(l)}</td>`;
  return `<tr>${business}${number}${middle}${status}${actions}</tr>`;
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

async function loadWaTemplates() {
  const [templates, settings] = await Promise.all([
    api('/api/wa/templates'), api('/api/settings'),
  ]);
  _waTemplates = { ...(templates || {}), followup_days: (settings || {}).wa_followup_days || 3 };
}

function toggleWaTemplateEditor() {
  const wrap = document.getElementById('wa-template-editor');
  const show = wrap.style.display === 'none';
  wrap.style.display = show ? 'block' : 'none';
  if (show) _renderWaTemplateEditor();
}

function _renderWaTemplateEditor() {
  const wrap = document.getElementById('wa-template-editor');
  const t = _waTemplates || {};
  wrap.innerHTML = `
    <div class="card" style="padding:18px">
      <div class="card-title" style="margin-bottom:10px">Message templates</div>
      <p class="text-muted text-small" style="margin-bottom:14px">
        Placeholders: <span class="mono">{{business_name}}</span> and <span class="mono">{{signal_detail}}</span>
        (the confirmed observation — not used in the follow-up). These are what gets drafted for every
        lead, then optionally paraphrased for variety — the placeholders are always filled in first.
      </p>
      <div class="form-group">
        <label>Gap found (no online booking seen)</label>
        <textarea id="wa-tpl-gap" style="min-height:80px">${esc(t.gap || '')}</textarea>
      </div>
      <div class="form-group">
        <label>No gap (they already have online booking)</label>
        <textarea id="wa-tpl-no_gap" style="min-height:80px">${esc(t.no_gap || '')}</textarea>
      </div>
      <div class="form-group">
        <label>Follow-up</label>
        <textarea id="wa-tpl-followup" style="min-height:60px">${esc(t.followup || '')}</textarea>
      </div>
      <div class="form-group">
        <label>Follow-up interval (days)</label>
        <input type="number" id="wa-followup-days" min="1" style="max-width:120px"
               value="${esc(t.followup_days ?? 3)}" />
        <div class="form-hint">A sent lead surfaces under "Follow-up due" this many days after its last send, forever, until replied or paused.</div>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-primary" onclick="saveWaTemplates()">Save</button>
        <button class="btn btn-ghost" onclick="toggleWaTemplateEditor()">Close</button>
      </div>
    </div>`;
}

async function saveWaTemplates() {
  const templates = {
    gap: document.getElementById('wa-tpl-gap').value,
    no_gap: document.getElementById('wa-tpl-no_gap').value,
    followup: document.getElementById('wa-tpl-followup').value,
  };
  const days = parseInt(document.getElementById('wa-followup-days').value, 10) || 3;
  const [res] = await Promise.all([
    api('/api/wa/templates', 'PUT', { templates }),
    api('/api/settings', 'POST', { wa_followup_days: String(days) }),
  ]);
  if (!res || res.error) { toast((res && res.error) || 'Could not save', 'err'); return; }
  toast('Templates saved');
  await loadWaTemplates();
  toggleWaTemplateEditor();
}

// ── Import ────────────────────────────────────────────────────────────────────

function openWaImportModal() { openModal('modal-import-wa'); }

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
}
