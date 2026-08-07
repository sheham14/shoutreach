// ── Timezone list ─────────────────────────────────────────────────────────────

const TIMEZONES = [
  ["", "— Server default (UTC) —"],
  // North America
  ["America/St_Johns",               "Canada — Newfoundland (UTC-3:30)"],
  ["America/Halifax",                "Canada — Atlantic (UTC-4)"],
  ["America/Toronto",                "Canada/US — Eastern (UTC-5)"],
  ["America/Chicago",                "US — Central (UTC-6)"],
  ["America/Denver",                 "US — Mountain (UTC-7)"],
  ["America/Los_Angeles",            "US/Canada — Pacific (UTC-8)"],
  ["America/Anchorage",              "US — Alaska (UTC-9)"],
  ["Pacific/Honolulu",               "US — Hawaii (UTC-10)"],
  ["America/Mexico_City",            "Mexico — Central (UTC-6)"],
  ["America/Sao_Paulo",              "Brazil — São Paulo (UTC-3)"],
  ["America/Argentina/Buenos_Aires", "Argentina (UTC-3)"],
  // Europe
  ["Europe/London",    "UK — London (UTC+0)"],
  ["Europe/Dublin",    "Ireland — Dublin (UTC+0)"],
  ["Europe/Lisbon",    "Portugal — Lisbon (UTC+0)"],
  ["Europe/Paris",     "France (UTC+1)"],
  ["Europe/Berlin",    "Germany (UTC+1)"],
  ["Europe/Amsterdam", "Netherlands (UTC+1)"],
  ["Europe/Rome",      "Italy (UTC+1)"],
  ["Europe/Madrid",    "Spain (UTC+1)"],
  ["Europe/Stockholm", "Sweden (UTC+1)"],
  ["Europe/Warsaw",    "Poland (UTC+1)"],
  ["Europe/Athens",    "Greece (UTC+2)"],
  ["Europe/Helsinki",  "Finland (UTC+2)"],
  ["Europe/Moscow",    "Russia — Moscow (UTC+3)"],
  // Middle East / Africa
  ["Asia/Riyadh",          "Saudi Arabia (UTC+3)"],
  ["Asia/Dubai",           "UAE — Dubai (UTC+4)"],
  ["Africa/Cairo",         "Egypt (UTC+2)"],
  ["Africa/Johannesburg",  "South Africa (UTC+2)"],
  ["Africa/Lagos",         "Nigeria (UTC+1)"],
  ["Africa/Nairobi",       "Kenya (UTC+3)"],
  // Asia Pacific
  ["Asia/Kolkata",     "India (UTC+5:30)"],
  ["Asia/Dhaka",       "Bangladesh (UTC+6)"],
  ["Asia/Bangkok",     "Thailand (UTC+7)"],
  ["Asia/Singapore",   "Singapore (UTC+8)"],
  ["Asia/Hong_Kong",   "Hong Kong (UTC+8)"],
  ["Asia/Shanghai",    "China (UTC+8)"],
  ["Asia/Tokyo",       "Japan (UTC+9)"],
  ["Asia/Seoul",       "South Korea (UTC+9)"],
  ["Australia/Perth",     "Australia — Perth (UTC+8)"],
  ["Australia/Sydney",    "Australia — Sydney (UTC+10/11)"],
  ["Pacific/Auckland",    "New Zealand (UTC+12/13)"],
];

function _populateTimezoneSelect(elId, selectedValue) {
  const sel = document.getElementById(elId);
  if (!sel) return;
  sel.innerHTML = TIMEZONES.map(([val, label]) =>
    `<option value="${val}" ${val === (selectedValue || '') ? 'selected' : ''}>${label}</option>`
  ).join('');
}

async function loadCampaigns() {
  const campaigns = await api('/api/campaigns');
  const el = document.getElementById('campaigns-list');
  if (!campaigns.length) {
    el.innerHTML = '<div class="empty-state"><div class="icon">⚡</div><p>No campaigns yet. Create your first one.</p></div>';
    return;
  }
  el.innerHTML = campaigns.map(c => `
    <div class="card" style="margin-bottom:14px;padding:20px">
      <div class="flex items-center gap-3">
        <div>
          <div style="font-weight:600;font-size:15px;color:#fff">${esc(c.name)}</div>
          <div class="text-muted text-small mono">${c.contact_count} contacts · ${c.step_count} steps · ${c.daily_limit}/day limit</div>
        </div>
        <div class="ml-auto flex gap-2 items-center">
          ${statusBadge(c.status)}
          <button class="btn btn-ghost btn-sm" onclick="openCampaign(${c.id})">Manage →</button>
          ${c.status === 'active'
            ? `<button class="btn btn-amber btn-sm" onclick="pauseCampaign(${c.id})">⏸ Pause</button>`
            : `<button class="btn btn-primary btn-sm" onclick="activateCampaign(${c.id})">▶ Activate</button>`}
          <button class="btn btn-danger btn-sm" onclick="deleteCampaign(${c.id}, '${escj(c.name)}')">✕</button>
        </div>
      </div>
    </div>
  `).join('');
}

async function deleteCampaign(id, name) {
  if (!confirm(`Delete campaign "${name}"?\n\nThis will permanently remove the campaign, all its steps, enrollments, and send history.`)) return;
  await api(`/api/campaigns/${id}`, 'DELETE');
  toast('Campaign deleted');
  loadCampaigns();
}

function openNewCampaignModal() {
  _populateTimezoneSelect('nc-timezone', '');
  openModal('modal-new-campaign');
}

async function createCampaign() {
  const name = document.getElementById('nc-name').value.trim();
  if (!name) { toast('Enter a campaign name', 'err'); return; }
  const res = await api('/api/campaigns', 'POST', {
    name,
    daily_limit:     +document.getElementById('nc-limit').value,
    send_start_hour: +document.getElementById('nc-start').value,
    send_end_hour:   +document.getElementById('nc-end').value,
    min_delay_secs:  +document.getElementById('nc-mindelay').value,
    max_delay_secs:  +document.getElementById('nc-maxdelay').value,
    timezone:        document.getElementById('nc-timezone').value || null,
  });
  closeModal('modal-new-campaign');
  toast('Campaign created ✓');
  loadCampaigns();
  openCampaign(res.id);
}

async function activateCampaign(id) {
  const res = await api(`/api/campaigns/${id}/activate`, 'POST');
  if (!res.ok) { toast(res.error, 'err'); return; }
  toast('Campaign activated ▶');
  loadCampaigns();
}

async function pauseCampaign(id) {
  await api(`/api/campaigns/${id}/pause`, 'POST');
  toast('Campaign paused ⏸');
  loadCampaigns();
}

async function openCampaign(id) {
  currentCampaignId = id;
  const c = await api(`/api/campaigns/${id}`);

  document.getElementById('cd-name').textContent = c.name;

  const s = c.stats;
  document.getElementById('cd-stats').innerHTML = [
    ['Enrolled', s.total, ''],
    ['Sent',     s.sent,  ''],
    ['Replies',  s.replied, 'green'],
    ['Bounced',  s.bounced, s.bounced > 0 ? 'red' : ''],
    ['Queued',   s.queued,  'blue'],
  ].map(([label, val, cls]) => `
    <div class="stat-card">
      <div class="stat-label">${label}</div>
      <div class="stat-value ${cls}">${val}</div>
    </div>
  `).join('');

  document.getElementById('campaign-actions').innerHTML = `
    <button class="btn btn-ghost btn-sm" onclick="openEditCampaignModal()">⚙ Settings</button>
    <span class="mono text-muted" style="font-size:12px;padding:8px">${statusBadge(c.status)}</span>
    ${c.status === 'active'
      ? `<button class="btn btn-amber btn-sm" onclick="pauseCampaign(${id});openCampaign(${id})">⏸ Pause</button>`
      : `<button class="btn btn-primary btn-sm" onclick="activateCampaign(${id});openCampaign(${id})">▶ Activate</button>`}
  `;

  // Variant stats breakdown (only shown when A/B variants exist)
  const vsEl = document.getElementById('cd-variant-stats');
  const allVs = c.variant_stats || [];
  // Only show breakdown when at least one A/B variant (non-default) label exists
  const hasAB = allVs.some(v => v.variant_label && v.variant_label !== 'default');
  const vs = hasAB ? allVs : [];
  if (vs.length > 0) {
    vsEl.innerHTML = `
      <div style="margin-top:14px;padding:14px;background:var(--bg2);border-radius:8px;border:1px solid var(--border)">
        <div style="font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px">A/B Variant Breakdown</div>
        <table style="font-size:13px">
          <thead><tr>
            <th>Variant</th><th>Enrolled</th><th>Sent</th>
            <th style="color:var(--green)">Replies</th>
            <th style="color:var(--red)">Bounced</th>
            <th>Reply Rate</th>
          </tr></thead>
          <tbody>${vs.map(v => {
            const rate = v.sent > 0 ? ((v.replied / v.sent) * 100).toFixed(1) : '0.0';
            return `<tr>
              <td>${v.variant_label === 'default'
                ? '<span class="badge" style="opacity:.6">Pre-variant</span>'
                : `<span class="badge badge-blue">Variant ${esc(v.variant_label)}</span>`}</td>
              <td>${v.enrolled}</td>
              <td>${v.sent}</td>
              <td style="color:var(--green)">${v.replied}</td>
              <td style="${v.bounced > 0 ? 'color:var(--red)' : ''}">${v.bounced}</td>
              <td>${rate}%</td>
            </tr>`;
          }).join('')}</tbody>
        </table>
      </div>`;
  } else {
    vsEl.innerHTML = '';
  }

  renderSteps(c.steps);
  renderContactReport(c.report || []);
  loadCampaignAccountBadges();
  showSection('campaign-detail');
}

// ── Campaign variable key-value editor ────────────────────────────────────────

function addCampaignVar(key = '', value = '') {
  const row = document.createElement('div');
  row.className = 'ec-var-row';
  row.style.cssText = 'display:flex;gap:6px;align-items:center';
  row.innerHTML = `
    <input class="ec-var-key" placeholder="variable_name" value="${esc(key)}"
           style="flex:1;background:var(--bg3);border:1px solid var(--border2);border-radius:6px;
                  padding:6px 10px;color:var(--text);font-size:13px;font-family:var(--mono)" />
    <input class="ec-var-val" placeholder="value" value="${esc(value)}"
           style="flex:2;background:var(--bg3);border:1px solid var(--border2);border-radius:6px;
                  padding:6px 10px;color:var(--text);font-size:13px" />
    <button class="btn btn-danger btn-sm" onclick="this.closest('.ec-var-row').remove()">✕</button>`;
  document.getElementById('ec-vars-list').appendChild(row);
}

function _getCampaignVars() {
  const vars = {};
  document.querySelectorAll('#ec-vars-list .ec-var-row').forEach(row => {
    const k = row.querySelector('.ec-var-key').value.trim().replace(/\s+/g, '_');
    const v = row.querySelector('.ec-var-val').value;
    if (k) vars[k] = v;
  });
  return vars;
}

async function openEditCampaignModal() {
  const c = await api(`/api/campaigns/${currentCampaignId}`);
  document.getElementById('ec-name').value     = c.name;
  document.getElementById('ec-limit').value    = c.daily_limit;
  document.getElementById('ec-mindelay').value = c.min_delay_secs;
  document.getElementById('ec-maxdelay').value = c.max_delay_secs;
  document.getElementById('ec-start').value    = c.send_start_hour;
  document.getElementById('ec-end').value      = c.send_end_hour;
  document.getElementById('ec-bounce').value   = c.bounce_pause_pct;
  _populateTimezoneSelect('ec-timezone', c.timezone || '');
  // Render existing campaign variables
  const varsList = document.getElementById('ec-vars-list');
  varsList.innerHTML = '';
  const vars = c.variables || {};
  Object.entries(vars).forEach(([k, v]) => addCampaignVar(k, v));
  openModal('modal-edit-campaign');
}

async function saveCampaignSettings() {
  const payload = {
    name:             document.getElementById('ec-name').value.trim(),
    daily_limit:      +document.getElementById('ec-limit').value,
    min_delay_secs:   +document.getElementById('ec-mindelay').value,
    max_delay_secs:   +document.getElementById('ec-maxdelay').value,
    send_start_hour:  +document.getElementById('ec-start').value,
    send_end_hour:    +document.getElementById('ec-end').value,
    bounce_pause_pct: +document.getElementById('ec-bounce').value,
    timezone:         document.getElementById('ec-timezone').value || null,
    variables:        _getCampaignVars(),
  };
  if (!payload.name) { toast('Campaign name is required', 'err'); return; }
  await api(`/api/campaigns/${currentCampaignId}`, 'PATCH', payload);
  closeModal('modal-edit-campaign');
  toast('Settings saved ✓');
  openCampaign(currentCampaignId);
}

async function loadCampaignAccountBadges() {
  const assigned = await api(`/api/campaigns/${currentCampaignId}/accounts`);
  const el = document.getElementById('cd-accounts-list');
  el.innerHTML = assigned.length
    ? assigned.map(a => `<span class="badge badge-blue">${esc(a.name)} &lt;${esc(a.email)}&gt;</span>`).join('')
    : '<span class="text-muted text-small">None (using global SMTP)</span>';
}

async function openCampaignAccountsModal() {
  const [all, assigned] = await Promise.all([
    api('/api/accounts'),
    api(`/api/campaigns/${currentCampaignId}/accounts`),
  ]);
  const assignedIds = new Set(assigned.map(a => a.id));
  const container = document.getElementById('campaign-account-checkboxes');
  if (!all.length) {
    container.innerHTML = '<p class="text-muted text-small">No accounts configured yet. Add them in Settings.</p>';
  } else {
    container.innerHTML = all.map(a => `
      <label style="display:flex;align-items:center;gap:10px;cursor:pointer;font-size:13px">
        <input type="checkbox" value="${a.id}" ${assignedIds.has(a.id) ? 'checked' : ''} style="cursor:pointer" />
        <div>
          <span style="font-weight:500">${esc(a.name)}</span>
          <span class="mono text-muted" style="font-size:11px;margin-left:6px">${esc(a.email)}</span>
        </div>
      </label>
    `).join('');
  }
  openModal('modal-campaign-accounts');
}

async function saveCampaignAccounts() {
  const ids = [...document.querySelectorAll('#campaign-account-checkboxes input:checked')]
    .map(cb => +cb.value);
  await api(`/api/campaigns/${currentCampaignId}/accounts`, 'POST', { account_ids: ids });
  toast('Accounts saved ✓');
  closeModal('modal-campaign-accounts');
  loadCampaignAccountBadges();
}

function renderSteps(steps) {
  const container = document.getElementById('steps-container');
  if (!steps.length) {
    container.innerHTML = '<div class="empty-state"><p>No steps yet. Add your first email.</p></div>';
    return;
  }
  container.innerHTML = steps.map((s, i) => `
    ${i > 0 ? `<div class="step-connector">Wait ${s.delay_days} day${s.delay_days !== 1 ? 's' : ''}</div>` : ''}
    <div class="step-card">
      <div class="step-num">${s.step_num}</div>
      <div style="font-weight:600;font-size:13px;margin-bottom:4px">${esc(s.subject)}</div>
      <div class="text-muted text-small" style="font-family:var(--mono)">${esc(s.body_html.replace(/<[^>]+>/g,'').substring(0,80))}...</div>
      <div class="flex gap-2" style="margin-top:12px">
        <button class="btn btn-ghost btn-sm" onclick="editStep(${s.step_num})">Edit</button>
        <button class="btn btn-danger btn-sm" onclick="deleteStep(${s.step_num})">Delete</button>
      </div>
    </div>
  `).join('');
}

let enrollSelectedIds = new Set();
let _currentEnrollments = [];

function toggleEnrollSelect(enrollId, checked) {
  if (checked) enrollSelectedIds.add(enrollId);
  else enrollSelectedIds.delete(enrollId);
  _updateRemoveSelectedBtn();
  _rerenderEnrollSelectAll();
}

function toggleSelectAllEnrolled(checked) {
  _currentEnrollments.forEach(c =>
    checked ? enrollSelectedIds.add(c.enroll_id) : enrollSelectedIds.delete(c.enroll_id)
  );
  _updateRemoveSelectedBtn();
  // Re-render checkboxes without full table rebuild
  document.querySelectorAll('#cd-report input[type=checkbox][data-enroll]').forEach(cb => {
    cb.checked = checked;
  });
}

function _rerenderEnrollSelectAll() {
  const cb = document.getElementById('enroll-select-all');
  if (!cb) return;
  cb.checked = _currentEnrollments.length > 0 &&
    _currentEnrollments.every(c => enrollSelectedIds.has(c.enroll_id));
}

function _updateRemoveSelectedBtn() {
  const btn = document.getElementById('enroll-remove-selected-btn');
  if (!btn) return;
  const n = enrollSelectedIds.size;
  btn.disabled = n === 0;
  btn.textContent = n > 0 ? `✕ Remove Selected (${n})` : '✕ Remove Selected';
}

async function removeSelectedEnrolled() {
  const n = enrollSelectedIds.size;
  if (!n) return;
  if (!confirm(`Remove ${n} contact${n > 1 ? 's' : ''} from this campaign?`)) return;
  await Promise.all([...enrollSelectedIds].map(id => api(`/api/enrollments/${id}`, 'DELETE')));
  toast(`Removed ${n} contact${n > 1 ? 's' : ''}`);
  openCampaign(currentCampaignId);
}

async function unenrollContact(enrollId) {
  if (!confirm('Remove this contact from the campaign?')) return;
  await api(`/api/enrollments/${enrollId}`, 'DELETE');
  toast('Contact removed');
  openCampaign(currentCampaignId);
}

function _clearStepModal() {
  document.getElementById('step-subject').value = '';
  document.getElementById('step-body').value = '';
  document.getElementById('step-variants-list').innerHTML = '';
  _setBaseFieldsVisible(true);
  _updateVariantWeightTotal();
  _lastFocusedCopyField = null;
  const vp = document.getElementById('var-panel');
  if (vp) vp.style.display = 'none';
  const vg = document.getElementById('var-gap-warning');
  if (vg) { vg.style.display = 'none'; vg.innerHTML = ''; }
  const sw = document.getElementById('spam-warning');
  if (sw) sw.style.display = 'none';
  const panel = document.getElementById('ai-review-panel');
  if (panel) panel.style.display = 'none';
}

function addStepUI() {
  const steps = document.querySelectorAll('.step-card');
  const nextNum = steps.length + 1;
  document.getElementById('step-modal-title').textContent = `Add Step ${nextNum}`;
  document.getElementById('step-num-input').value = nextNum;
  document.getElementById('step-delay').value = nextNum === 1 ? 0 : 3;
  _clearStepModal();
  openModal('modal-step');
  loadVariableCoverage();
}

async function editStep(stepNum) {
  const steps = await api(`/api/campaigns/${currentCampaignId}/steps`);
  const s = steps.find(x => x.step_num === stepNum);
  if (!s) return;
  document.getElementById('step-modal-title').textContent = `Edit Step ${stepNum}`;
  document.getElementById('step-num-input').value = stepNum;
  document.getElementById('step-delay').value     = s.delay_days;
  document.getElementById('step-subject').value   = s.subject;
  document.getElementById('step-body').value      = s.body_html;

  // Every step owns at least one variant now, so a single one is just "this
  // step's copy" -- show it in the plain editor instead of as a lone Variant A
  // block with a meaningless 100% weight next to it.
  document.getElementById('step-variants-list').innerHTML = '';
  const variants = s.variants || [];
  if (variants.length > 1) {
    variants.forEach(v => _addVariantBlock(v.label, v.subject, v.body_html, v.weight));
    _setBaseFieldsVisible(false);
  } else {
    if (variants.length === 1) {
      document.getElementById('step-subject').value = variants[0].subject;
      document.getElementById('step-body').value    = variants[0].body_html;
    }
    _setBaseFieldsVisible(true);
  }
  _updateVariantWeightTotal();
  _updateSpamWarning();
  openModal('modal-step');
  loadVariableCoverage().then(_updateVariableGapWarning);
}

async function saveStep() {
  const stepNum = +document.getElementById('step-num-input').value;
  const delay   = +document.getElementById('step-delay').value;

  const variants = _variantBlocks().map(block => ({
    label:     block.dataset.label,
    subject:   block.querySelector('.v-subject').value.trim(),
    body_html: block.querySelector('.v-body').value.trim(),
    weight:    parseInt(block.querySelector('.v-weight').value) || 0,
  }));

  let subject, body;
  if (variants.length) {
    // Validate every arm: a blank variant used to be dropped silently, so a
    // half-finished B would save as a one-arm step while still looking like a
    // configured test on screen.
    const blank = variants.find(v => !v.subject || !v.body_html);
    if (blank) {
      toast(`Variant ${blank.label} needs a subject and a body`, 'err');
      return;
    }
    if (variants.some(v => v.weight <= 0)) {
      toast('Every variant needs a weight above 0', 'err');
      return;
    }
    // The step's own copy mirrors the first arm; it is the fallback for any
    // send whose label cannot be resolved.
    subject = variants[0].subject;
    body    = variants[0].body_html;
  } else {
    subject = document.getElementById('step-subject').value.trim();
    body    = document.getElementById('step-body').value.trim();
    if (!subject || !body) { toast('Subject and body are required', 'err'); return; }
  }

  // Last line of defence. The panel makes gaps visible, but a variable typed
  // by hand never goes through it -- and a blank where a name should be is the
  // kind of thing you only notice in the sent folder.
  const gaps = _variableGaps();
  if (gaps.length) {
    const lines = gaps.map(g => g.filled === 0
      ? `  {{${g.key}}} — empty for ALL ${g.total} contacts`
      : `  {{${g.key}}} — empty for ${g.missing} of ${g.total} contacts`);
    const suggestion = `{{${gaps[0].key}|${_FALLBACK_SUGGESTIONS[gaps[0].key] || 'there'}}}`;
    const ok = confirm(
      `These variables have no fallback and are missing for some contacts:\n\n`
      + lines.join('\n')
      + `\n\nThose emails will send a blank where the value should be.\n`
      + `Add a fallback like ${suggestion} to fix it.\n\nSave anyway?`
    );
    if (!ok) return;
  }

  await api(`/api/campaigns/${currentCampaignId}/steps`, 'POST', {
    step_num: stepNum, subject, body_html: body, delay_days: delay, variants,
  });
  closeModal('modal-step');
  toast('Step saved ✓');
  openCampaign(currentCampaignId);
}

async function deleteStep(stepNum) {
  if (!confirm(`Delete step ${stepNum}?`)) return;
  await api(`/api/campaigns/${currentCampaignId}/steps/${stepNum}`, 'DELETE');
  toast('Step deleted');
  openCampaign(currentCampaignId);
}

// ── Variables: coverage, insertion, and the gap warning ──────────────────────
//
// Which variables are worth using depends entirely on the list. A scraped
// campaign has a company for every contact and a first name for none, so
// "Hi {{first_name}}," sends "Hi ," to all of them -- and nothing said so until
// the mail had gone. Coverage is counted over the contacts enrolled in THIS
// campaign, because the database as a whole is not the population being mailed.

let _varCoverage = null;         // { scope, total, variables: [...] }
let _lastFocusedCopyField = null;

// Suggested fallbacks. Only used to prefill the insert; the operator can edit
// or delete them, and the text is what ends up in the template either way.
const _FALLBACK_SUGGESTIONS = {
  first_name:   'there',
  last_name:    '',
  full_name:    'there',
  company:      'your business',
  phone:        '',
  website:      'your site',
  category:     'local',
  rating:       '',
  review_count: '',
  address:      '',
};

async function loadVariableCoverage() {
  const url = currentCampaignId
    ? `/api/campaigns/${currentCampaignId}/variable-coverage`
    : '/api/variable-coverage';
  const data = await api(url);
  _varCoverage = (data && data.variables) ? data : null;
  _renderVariableScope();
  return _varCoverage;
}

function _renderVariableScope() {
  const el = document.getElementById('var-panel-scope');
  if (!el || !_varCoverage) return;
  el.textContent = _varCoverage.total === 0
    ? 'no contacts to measure against yet'
    : (_varCoverage.scope === 'campaign'
        ? `coverage across ${_varCoverage.total} enrolled contact${_varCoverage.total === 1 ? '' : 's'}`
        : `coverage across all ${_varCoverage.total} contacts — none enrolled yet`);
}

async function toggleVariablePanel() {
  const panel = document.getElementById('var-panel');
  if (!panel) return;
  if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
  if (!_varCoverage) await loadVariableCoverage();
  _renderVariablePanel();
  panel.style.display = 'block';
}

function _renderVariablePanel() {
  const panel = document.getElementById('var-panel');
  if (!panel || !_varCoverage) return;

  panel.innerHTML = _varCoverage.variables.map(v => {
    const none  = v.total > 0 && v.filled === 0;
    const gap   = v.total > 0 && v.filled > 0 && v.filled < v.total;
    const color = none ? 'var(--red)' : (gap ? 'var(--amber)' : 'var(--green)');
    const count = v.total === 0 ? '—' : `${v.filled} of ${v.total}`;
    const note  = none ? 'none have this' : (gap ? 'needs a fallback' : 'all have this');
    return `
      <div style="display:flex;align-items:center;gap:10px;padding:6px 14px;border-bottom:1px solid var(--border)">
        <span class="mono" style="font-size:12px;min-width:150px">{{${esc(v.key)}}}</span>
        <span class="text-muted" style="font-size:11px;flex:1">${esc(v.label)}</span>
        <span class="mono" style="font-size:11px;color:${color};min-width:74px;text-align:right">${count}</span>
        <span style="font-size:10px;color:${color};min-width:96px">${note}</span>
        <button type="button" class="btn btn-ghost btn-sm"
                onclick="insertVariable('${esc(v.key)}', ${none || gap})">Insert</button>
      </div>`;
  }).join('');
}

// Remembering the last focused editor is what lets Insert land in the field the
// operator was actually typing in, rather than always the base body.
function _rememberCopyField(el) { _lastFocusedCopyField = el; }

document.addEventListener('focusin', e => {
  const t = e.target;
  if (!t) return;
  if (t.id === 'step-subject' || t.id === 'step-body'
      || t.classList?.contains('v-subject') || t.classList?.contains('v-body')) {
    _rememberCopyField(t);
  }
});

function insertVariable(key, withFallback) {
  const el = _lastFocusedCopyField
          || document.getElementById('step-body')
          || document.getElementById('step-subject');
  if (!el) return;

  const suggestion = _FALLBACK_SUGGESTIONS[key] || '';
  const token = (withFallback && suggestion)
    ? `{{${key}|${suggestion}}}`
    : `{{${key}}}`;

  const start = el.selectionStart ?? el.value.length;
  const end   = el.selectionEnd ?? el.value.length;
  el.value = el.value.slice(0, start) + token + el.value.slice(end);
  const caret = start + token.length;
  el.focus();
  el.setSelectionRange(caret, caret);

  _updateSpamWarning();
  _updateVariableGapWarning();
}

// Every {{name}} or {{name|fallback}} used anywhere in this step.
function _usedPlaceholders() {
  const text = [
    document.getElementById('step-subject')?.value || '',
    document.getElementById('step-body')?.value || '',
    ...[...document.querySelectorAll('.v-subject')].map(el => el.value),
    ...[...document.querySelectorAll('.v-body')].map(el => el.value),
  ].join('\n');

  const out = new Map();   // key -> hasFallback (true if every use has one)
  const re = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\|([^}]*))?\}\}/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const key = m[1];
    const hasFallback = m[2] !== undefined && (m[3] || '').trim() !== '';
    out.set(key, out.has(key) ? (out.get(key) && hasFallback) : hasFallback);
  }
  return out;
}

// The check that actually prevents "Hi ," going out: a variable can be typed by
// hand without ever opening the panel, so the gap has to be caught here too.
function _variableGaps() {
  if (!_varCoverage || !_varCoverage.total) return [];
  const byKey = Object.fromEntries(_varCoverage.variables.map(v => [v.key, v]));
  const gaps  = [];
  for (const [key, hasFallback] of _usedPlaceholders()) {
    const v = byKey[key];
    if (!v || hasFallback) continue;
    if (v.filled < v.total) {
      gaps.push({ key, missing: v.total - v.filled, total: v.total, filled: v.filled });
    }
  }
  return gaps;
}

function _updateVariableGapWarning() {
  const el = document.getElementById('var-gap-warning');
  if (!el) return;
  const gaps = _variableGaps();
  if (!gaps.length) { el.style.display = 'none'; el.innerHTML = ''; return; }

  el.style.display = 'block';
  el.innerHTML = '⚠ ' + gaps.map(g =>
    g.filled === 0
      ? `<span class="mono">{{${esc(g.key)}}}</span> is empty for <strong>all ${g.total}</strong> of these contacts`
      : `<span class="mono">{{${esc(g.key)}}}</span> is empty for <strong>${g.missing} of ${g.total}</strong>`
  ).join('; ') + ' — add a fallback like <span class="mono">{{'
    + esc(gaps[0].key) + '|' + esc(_FALLBACK_SUGGESTIONS[gaps[0].key] || 'there')
    + '}}</span> or those emails send a blank.';
}

// ── Spam checker ─────────────────────────────────────────────────────────────

const _SPAM_WORDS = [
  'free','guarantee','guaranteed','winner','won','prize','congratulations',
  'urgent','act now','limited time','don\'t miss','click here','buy now',
  'order now','purchase now','earn money','make money','extra income',
  'work from home','no risk','risk free','risk-free','no cost','no fees',
  'special offer','special promotion','exclusive deal','discount','lowest price',
  'best price','cheap','this is not spam','not spam','satisfaction guaranteed',
  'money back','money-back','increase sales','double your','billion dollar',
  'weight loss','as seen on','dear friend','apply now','sign up free',
  'call now','order today','trial offer','while supplies last','incredible deal',
  'amazing offer','once in a lifetime','you have been selected','you\'ve been chosen',
];

function _updateSpamWarning() {
  const text = [
    document.getElementById('step-subject')?.value || '',
    document.getElementById('step-body')?.value || '',
    ...[...document.querySelectorAll('.v-subject')].map(el => el.value),
    ...[...document.querySelectorAll('.v-body')].map(el => el.value),
  ].join(' ').toLowerCase();

  // Variable gaps are re-checked on the same keystrokes as the spam words, so
  // the warning tracks the copy instead of only appearing at save time.
  _updateVariableGapWarning();

  const found = _SPAM_WORDS.filter(w => text.includes(w));
  const el    = document.getElementById('spam-warning');
  if (!el) return;
  if (found.length) {
    document.getElementById('spam-words').textContent = found.map(w => `"${w}"`).join(', ');
    el.style.display = 'block';
  } else {
    el.style.display = 'none';
  }
}

// ── Email preview ─────────────────────────────────────────────────────────────

let _previewDebounce = null;

function openStepPreview() {
  openModal('modal-preview');
  refreshPreview();
}

async function refreshPreview() {
  clearTimeout(_previewDebounce);
  _previewDebounce = setTimeout(async () => {
    const subject  = document.getElementById('step-subject')?.value || '';
    const body     = document.getElementById('step-body')?.value || '';
    const contact  = {
      first_name: document.getElementById('preview-first-name').value,
      last_name:  document.getElementById('preview-last-name').value,
      company:    document.getElementById('preview-company').value,
      email:      'preview@example.com',
    };
    contact.full_name = `${contact.first_name} ${contact.last_name}`.trim();

    document.getElementById('preview-loading').style.display = 'block';
    const res = await api('/api/preview', 'POST', { subject, body_html: body, contact });
    document.getElementById('preview-loading').style.display = 'none';

    document.getElementById('preview-subject').textContent = res.subject || '(no subject)';
    const iframe = document.getElementById('preview-iframe');
    iframe.srcdoc = res.body_html || '';
    // Auto-size iframe to content
    iframe.onload = () => {
      try {
        iframe.style.height = iframe.contentDocument.body.scrollHeight + 32 + 'px';
      } catch(e) {}
    };
  }, 300);
}

// ── Variant builder ───────────────────────────────────────────────────────────

const _LABELS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';

// The step editor shows exactly as many copy editors as the step has arms.
//
// Copy used to live in a base subject/body PLUS an optional variant list, so a
// two-arm test presented three editors and it was not obvious which one got
// sent to whom. The base is now simply the step's only arm: adding a variant
// promotes what you have already written to Variant A and gives you an empty
// Variant B, and deleting back down to one arm folds it into the plain editor.
function _variantBlocks() {
  return [...document.querySelectorAll('#step-variants-list .variant-block')];
}

function _setBaseFieldsVisible(visible) {
  const el = document.getElementById('step-no-variant-fields');
  if (el) el.style.display = visible ? '' : 'none';
  const hint = document.getElementById('step-variants-hint');
  if (hint) {
    hint.textContent = visible
      ? 'One version of this email goes to everyone. Add a variant to split traffic and A/B test it.'
      : 'Each contact is assigned one variant when they are enrolled and keeps it for the whole sequence.';
  }
}

function addStepVariant() {
  const existing = _variantBlocks();

  if (existing.length === 0) {
    // Promote what is already written rather than stranding it: without this
    // the copy in the plain editor would silently stop being the thing sent.
    const subject = document.getElementById('step-subject').value;
    const body    = document.getElementById('step-body').value;
    _addVariantBlock('A', subject, body, 50);
    _addVariantBlock('B', '', '', 50);
    _setBaseFieldsVisible(false);
  } else {
    const label = _LABELS[existing.length] || `V${existing.length + 1}`;
    _addVariantBlock(label, '', '', 50);
  }
  _updateVariantWeightTotal();
}

function _addVariantBlock(label, subject, body, weight) {
  const div = document.createElement('div');
  div.className = 'variant-block';
  div.dataset.label = label;
  div.style.cssText = 'border:1px solid var(--border2);border-radius:8px;padding:14px;position:relative';
  div.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
      <span style="font-weight:600;font-size:13px;background:var(--bg3);border-radius:4px;padding:2px 8px">Variant ${esc(label)}</span>
      <div style="display:flex;align-items:center;gap:6px;margin-left:auto">
        <label style="font-size:12px;color:var(--muted)">Weight</label>
        <input class="v-weight" type="number" value="${weight}" min="1" max="999"
               oninput="_updateVariantWeightTotal()"
               style="width:64px;background:var(--bg3);border:1px solid var(--border2);
                      border-radius:4px;padding:4px 8px;color:var(--text);font-size:12px" />
        <span style="font-size:12px;color:var(--muted)">%</span>
        <button class="btn btn-danger btn-sm" onclick="removeVariantBlock(this)">✕</button>
      </div>
    </div>
    <div class="form-group" style="margin-bottom:8px">
      <input class="v-subject" placeholder="Subject for Variant ${esc(label)}" value="${esc(subject)}"
             style="width:100%;background:var(--bg3);border:1px solid var(--border2);
                    border-radius:6px;padding:8px 12px;color:var(--text);font-size:13px;
                    font-family:var(--font);box-sizing:border-box" />
    </div>
    <textarea class="v-body" placeholder="Body for Variant ${esc(label)}"
              style="width:100%;min-height:120px;background:var(--bg3);border:1px solid var(--border2);
                     border-radius:6px;padding:8px 12px;color:var(--text);font-size:13px;
                     font-family:var(--font);box-sizing:border-box;resize:vertical">${esc(body)}</textarea>
  `;
  document.getElementById('step-variants-list').appendChild(div);
}

function removeVariantBlock(btn) {
  btn.closest('.variant-block').remove();

  // Down to a single arm is not a test. Fold it back into the plain editor
  // rather than leaving one lonely "Variant A" block, so the editor always
  // reflects how many different emails actually go out.
  const remaining = _variantBlocks();
  if (remaining.length === 1) {
    const only = remaining[0];
    document.getElementById('step-subject').value = only.querySelector('.v-subject').value;
    document.getElementById('step-body').value    = only.querySelector('.v-body').value;
    only.remove();
  }
  if (_variantBlocks().length === 0) {
    _setBaseFieldsVisible(true);
  }

  // Re-label whatever is left so the labels stay contiguous from A.
  _variantBlocks().forEach((block, i) => {
    const label = _LABELS[i] || `V${i + 1}`;
    block.dataset.label = label;
    block.querySelector('span').textContent = `Variant ${label}`;
    block.querySelector('.v-subject').placeholder = `Subject for Variant ${label}`;
    block.querySelector('.v-body').placeholder = `Body for Variant ${label}`;
  });
  _updateVariantWeightTotal();
  _updateSpamWarning();
}

function _updateVariantWeightTotal() {
  const blocks = document.querySelectorAll('#step-variants-list .variant-block');
  const el = document.getElementById('variant-weight-total');
  if (!blocks.length) { el.textContent = ''; return; }
  const total = [...blocks].reduce((s, b) => s + (parseInt(b.querySelector('.v-weight').value) || 0), 0);
  el.textContent = `Total: ${total}`;
  el.style.color = Math.abs(total - 100) <= 1 ? 'var(--green)' : 'var(--amber)';
}

// ── AI Copy Review ────────────────────────────────────────────────────────────

let _aiRewrite = null; // holds { subject, body } from last review

async function openAIReview() {
  const panel   = document.getElementById('ai-review-panel');
  const loading = document.getElementById('ai-review-loading');
  const result  = document.getElementById('ai-review-result');

  const subject = document.getElementById('step-subject').value.trim();
  const body    = document.getElementById('step-body').value.trim();

  _aiRewrite = null;
  panel.style.display   = 'block';
  loading.style.display = 'block';
  result.innerHTML      = '';

  let data;
  try {
    data = await api('/api/ai/review', 'POST', { subject, body });
  } catch (e) {
    loading.style.display = 'none';
    result.innerHTML = `<span style="color:var(--red)">Error: ${esc(String(e))}</span>`;
    return;
  }
  loading.style.display = 'none';

  if (data.error) {
    const hints = {
      'Invalid API key':   'Double-check your API key in Settings → AI Features.',
      'not configured':    'Add your API key in Settings → AI Features.',
      'not enabled':       'Enable AI features in Settings → AI Features.',
      'Model not found':   'The selected model ID may be wrong or not yet available — check Settings → AI Features.',
      'credits exhausted': 'Top up your account balance with the provider.',
      'Rate limit':        'You\'ve hit the rate limit — wait a moment and try again.',
      'timed out':         'The provider is slow right now — try again in a moment.',
      'Network error':     'Check your internet connection and try again.',
      'non-JSON':          'The model returned an unexpected response — try again or switch to a different model.',
    };
    let hint = '';
    for (const [key, msg] of Object.entries(hints)) {
      if (data.error.includes(key)) { hint = msg; break; }
    }
    result.innerHTML = `<span style="color:var(--red)">${esc(data.error)}</span>` +
      (hint ? `<br><span class="text-muted" style="font-size:12px">${hint}</span>` : '');
    return;
  }

  const scoreColor = data.score >= 7 ? 'var(--green)' : data.score >= 4 ? 'var(--amber)' : 'var(--red)';
  const riskColor  = data.deliverability_risk === 'low' ? 'var(--green)' : data.deliverability_risk === 'medium' ? 'var(--amber)' : 'var(--red)';
  const listItems  = (arr) => (arr || []).map(s => `<li style="margin:3px 0">${esc(s)}</li>`).join('');

  const rw = data.rewrite;
  if (rw && (rw.subject || rw.body)) _aiRewrite = rw;

  const rewriteHtml = _aiRewrite ? `
    <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">
      <div style="font-size:12px;font-weight:600;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:.04em">Suggested Rewrite</div>
      ${_aiRewrite.subject ? `<div style="font-size:12px;margin-bottom:4px"><span style="color:var(--muted)">Subject:</span> ${esc(_aiRewrite.subject)}</div>` : ''}
      ${_aiRewrite.body ? `<div style="font-size:12px;color:var(--muted);white-space:pre-wrap;max-height:240px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-top:4px">${esc(_aiRewrite.body)}</div>` : ''}
      <div class="flex gap-2" style="margin-top:10px">
        <button class="btn btn-primary btn-sm" onclick="applyAIRewrite()">Apply Rewrite</button>
        <button class="btn btn-ghost btn-sm" onclick="dismissAIRewrite()">Dismiss</button>
      </div>
    </div>` : '';

  result.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
      <span style="font-size:22px;font-weight:700;color:${scoreColor}">${data.score}/10</span>
      <span style="font-size:13px;color:var(--muted)">${esc(data.summary || '')}</span>
      <span style="margin-left:auto;font-size:11px;color:${riskColor}">Deliverability: ${esc(data.deliverability_risk || '?')}</span>
    </div>
    ${(data.strengths||[]).length ? `<div style="margin-bottom:8px"><strong style="font-size:12px;color:var(--green)">Strengths</strong><ul style="margin:4px 0 0 16px;padding:0;font-size:12px">${listItems(data.strengths)}</ul></div>` : ''}
    ${(data.issues||[]).length ? `<div style="margin-bottom:8px"><strong style="font-size:12px;color:var(--amber)">Issues</strong><ul style="margin:4px 0 0 16px;padding:0;font-size:12px">${listItems(data.issues)}</ul></div>` : ''}
    ${(data.suggestions||[]).length ? `<div style="margin-bottom:8px"><strong style="font-size:12px;color:var(--blue,#60a5fa)">Suggestions</strong><ul style="margin:4px 0 0 16px;padding:0;font-size:12px">${listItems(data.suggestions)}</ul></div>` : ''}
    ${rewriteHtml}
  `;
}

function applyAIRewrite() {
  if (!_aiRewrite) return;
  if (_aiRewrite.subject) document.getElementById('step-subject').value = _aiRewrite.subject;
  if (_aiRewrite.body)    document.getElementById('step-body').value    = _aiRewrite.body;
  _updateSpamWarning();
  dismissAIRewrite();
  toast('Rewrite applied — review and save when ready');
}

function dismissAIRewrite() {
  _aiRewrite = null;
  const panel = document.getElementById('ai-review-panel');
  if (panel) panel.style.display = 'none';
}

// ── Contact Report ────────────────────────────────────────────────────────────

function renderContactReport(rows) {
  const exportBtn = document.getElementById('report-export-btn');
  if (exportBtn) exportBtn.href = `/api/campaigns/${currentCampaignId}/export`;

  enrollSelectedIds.clear();
  _currentEnrollments = rows;
  _updateRemoveSelectedBtn();

  const countEl = document.getElementById('report-count');
  if (countEl) countEl.textContent = rows.length ? `${rows.length} contacts` : '';

  const tbody = document.getElementById('cd-report');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty-state"><p>No contacts enrolled yet — click + Enroll to add some.</p></td></tr>';
    _rerenderEnrollSelectAll();
    return;
  }

  const STATUS_ROW_STYLE = {
    replied:  'background:rgba(34,197,94,.08)',
    bounced:  'background:rgba(239,68,68,.08)',
    completed:'background:rgba(148,163,184,.06)',
  };

  tbody.innerHTML = rows.map(r => {
    const checked = enrollSelectedIds.has(r.enroll_id) ? 'checked' : '';
    const name    = [r.first_name, r.last_name].filter(Boolean).join(' ') || '—';
    const variant = r.variant_label
      ? `<span class="badge badge-blue" style="font-size:11px">${esc(r.variant_label)}</span>`
      : '<span class="text-muted" style="font-size:11px">—</span>';
    const nextSend = r.next_send_at
      ? r.next_send_at.replace('T', ' ').substring(0, 16)
      : (r.status === 'completed' ? 'Done' : '—');
    const rowStyle = STATUS_ROW_STYLE[r.status] || '';
    const statusSel = `
      <select onchange="setEnrollmentStatus(${r.enroll_id}, this.value, this)"
              style="font-size:11px;padding:2px 4px;background:var(--bg3);border:1px solid var(--border2);
                     border-radius:4px;color:var(--text);cursor:pointer">
        <option value="queued"    ${r.status==='queued'    ? 'selected':''}>Queued</option>
        <option value="paused"    ${r.status==='paused'    ? 'selected':''}>Paused</option>
        <option value="replied"   ${r.status==='replied'   ? 'selected':''}>Replied</option>
        <option value="completed" ${r.status==='completed' ? 'selected':''}>Completed</option>
      </select>`;
    return `<tr style="${rowStyle}">
      <td><input type="checkbox" data-enroll="${r.enroll_id}" ${checked}
                 onchange="toggleEnrollSelect(${r.enroll_id}, this.checked)" style="cursor:pointer"/></td>
      <td class="mono" style="font-size:12px">${esc(r.email || '—')}</td>
      <td>${esc(name)}</td>
      <td>${esc(r.company || '—')}</td>
      <td>${variant}</td>
      <td class="mono" style="text-align:center">${r.steps_sent}</td>
      <td class="mono" style="text-align:center">${r.current_step}</td>
      <td>${statusSel}</td>
      <td class="mono text-muted" style="font-size:11px">${esc(nextSend)}</td>
      <td><button class="btn btn-danger btn-sm" onclick="unenrollContact(${r.enroll_id})" title="Remove">✕</button></td>
    </tr>`;
  }).join('');

  _rerenderEnrollSelectAll();
}

async function setEnrollmentStatus(enrollId, status, selectEl) {
  const res = await api(`/api/enrollments/${enrollId}/status`, 'PATCH', { status });
  if (res.ok) {
    toast(`Status set to ${status}`);
    // Re-colour the row to match new status
    const row = selectEl.closest('tr');
    const colors = { replied:'rgba(34,197,94,.08)', bounced:'rgba(239,68,68,.08)', completed:'rgba(148,163,184,.06)' };
    row.style.background = colors[status] || '';
  } else {
    toast(res.error || 'Failed to update status', 'err');
    openCampaign(currentCampaignId); // re-render to reset dropdown
  }
}
