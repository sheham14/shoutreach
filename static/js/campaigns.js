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
          <button class="btn btn-danger btn-sm" onclick="deleteCampaign(${c.id}, '${esc(c.name)}')">✕</button>
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

function openNewCampaignModal() { openModal('modal-new-campaign'); }

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
  renderCampaignContacts(c.contacts);
  renderContactReport(c.report || []);
  loadCampaignAccountBadges();
  showSection('campaign-detail');
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

function renderCampaignContacts(contacts) {
  enrollSelectedIds.clear();
  _currentEnrollments = contacts.slice(0, 100);
  _updateRemoveSelectedBtn();
  _buildEnrollTable();
}

function _buildEnrollTable() {
  const tbody = document.getElementById('cd-contacts');
  if (!_currentEnrollments.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state"><p>No contacts enrolled</p></td></tr>';
    _rerenderEnrollSelectAll();
    return;
  }
  tbody.innerHTML = _currentEnrollments.map(c => {
    const checked = enrollSelectedIds.has(c.enroll_id) ? 'checked' : '';
    const variantCell = c.variant_label
      ? `<span class="badge badge-blue" style="font-size:11px">${esc(c.variant_label)}</span>`
      : '<span class="text-muted" style="font-size:11px">—</span>';
    return `<tr>
      <td><input type="checkbox" ${checked} onchange="toggleEnrollSelect(${c.enroll_id}, this.checked)" style="cursor:pointer"/></td>
      <td class="mono" style="font-size:12px">${esc(c.email)}</td>
      <td>${esc(c.first_name)} ${esc(c.last_name)}</td>
      <td class="mono">${c.current_step}</td>
      <td>${variantCell}</td>
      <td>${enrollBadge(c.status)}</td>
      <td><button class="btn btn-danger btn-sm" onclick="unenrollContact(${c.enroll_id})" title="Remove">✕</button></td>
    </tr>`;
  }).join('');
  _rerenderEnrollSelectAll();
}

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
  _buildEnrollTable();
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
  _updateVariantWeightTotal();
}

function addStepUI() {
  const steps = document.querySelectorAll('.step-card');
  const nextNum = steps.length + 1;
  document.getElementById('step-modal-title').textContent = `Add Step ${nextNum}`;
  document.getElementById('step-num-input').value = nextNum;
  document.getElementById('step-delay').value = nextNum === 1 ? 0 : 3;
  _clearStepModal();
  openModal('modal-step');
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
  // Load existing variants
  document.getElementById('step-variants-list').innerHTML = '';
  (s.variants || []).forEach(v => _addVariantBlock(v.label, v.subject, v.body_html, v.weight));
  _updateVariantWeightTotal();
  openModal('modal-step');
}

async function saveStep() {
  const stepNum = +document.getElementById('step-num-input').value;
  const subject = document.getElementById('step-subject').value.trim();
  const body    = document.getElementById('step-body').value.trim();
  const delay   = +document.getElementById('step-delay').value;

  if (!subject || !body) { toast('Subject and body are required', 'err'); return; }

  const variantBlocks = document.querySelectorAll('#step-variants-list .variant-block');
  const variants = [...variantBlocks].map(block => ({
    label:    block.dataset.label,
    subject:  block.querySelector('.v-subject').value.trim(),
    body_html: block.querySelector('.v-body').value.trim(),
    weight:   parseInt(block.querySelector('.v-weight').value) || 0,
  })).filter(v => v.subject && v.body_html);

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

// ── Variant builder ───────────────────────────────────────────────────────────

const _LABELS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';

function addStepVariant() {
  const existing = document.querySelectorAll('#step-variants-list .variant-block');
  const label = _LABELS[existing.length] || `V${existing.length + 1}`;
  _addVariantBlock(label, '', '', 50);
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
  // Re-label remaining blocks
  document.querySelectorAll('#step-variants-list .variant-block').forEach((block, i) => {
    const label = _LABELS[i] || `V${i + 1}`;
    block.dataset.label = label;
    block.querySelector('span').textContent = `Variant ${label}`;
    block.querySelector('.v-subject').placeholder = `Subject for Variant ${label}`;
    block.querySelector('.v-body').placeholder = `Body for Variant ${label}`;
  });
  _updateVariantWeightTotal();
}

function _updateVariantWeightTotal() {
  const blocks = document.querySelectorAll('#step-variants-list .variant-block');
  const el = document.getElementById('variant-weight-total');
  if (!blocks.length) { el.textContent = ''; return; }
  const total = [...blocks].reduce((s, b) => s + (parseInt(b.querySelector('.v-weight').value) || 0), 0);
  el.textContent = `Total: ${total}`;
  el.style.color = Math.abs(total - 100) <= 1 ? 'var(--green)' : 'var(--amber)';
}

// ── Contact Report ────────────────────────────────────────────────────────────

function renderContactReport(rows) {
  // Wire up export link
  const exportBtn = document.getElementById('report-export-btn');
  if (exportBtn) exportBtn.href = `/api/campaigns/${currentCampaignId}/export`;

  const countEl = document.getElementById('report-count');
  if (countEl) countEl.textContent = rows.length ? `${rows.length} contacts` : '';

  const tbody = document.getElementById('cd-report');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-state"><p>No contacts enrolled</p></td></tr>';
    return;
  }

  const STATUS_ROW_STYLE = {
    replied:  'background:rgba(34,197,94,.08)',
    bounced:  'background:rgba(239,68,68,.08)',
    completed:'background:rgba(148,163,184,.06)',
  };

  tbody.innerHTML = rows.map(r => {
    const name = [r.first_name, r.last_name].filter(Boolean).join(' ') || '—';
    const variant = r.variant_label
      ? `<span class="badge badge-blue" style="font-size:11px">${esc(r.variant_label)}</span>`
      : '<span class="text-muted" style="font-size:11px">—</span>';
    const nextSend = r.next_send_at
      ? r.next_send_at.replace('T', ' ').substring(0, 16)
      : (r.status === 'completed' ? 'Done' : '—');
    const rowStyle = STATUS_ROW_STYLE[r.status] || '';
    return `<tr style="${rowStyle}">
      <td class="mono" style="font-size:12px">${esc(r.email || '—')}</td>
      <td>${esc(name)}</td>
      <td>${esc(r.company || '—')}</td>
      <td>${variant}</td>
      <td class="mono" style="text-align:center">${r.steps_sent}</td>
      <td class="mono" style="text-align:center">${r.current_step}</td>
      <td>${enrollBadge(r.status)}</td>
      <td class="mono text-muted" style="font-size:11px">${esc(nextSend)}</td>
    </tr>`;
  }).join('');
}
