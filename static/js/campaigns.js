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
        </div>
      </div>
    </div>
  `).join('');
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
    <span class="mono text-muted" style="font-size:12px;padding:8px">${statusBadge(c.status)}</span>
    ${c.status === 'active'
      ? `<button class="btn btn-amber btn-sm" onclick="pauseCampaign(${id});openCampaign(${id})">⏸ Pause</button>`
      : `<button class="btn btn-primary btn-sm" onclick="activateCampaign(${id});openCampaign(${id})">▶ Activate</button>`}
  `;

  renderSteps(c.steps);
  renderCampaignContacts(c.contacts);
  showSection('campaign-detail');
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

function renderCampaignContacts(contacts) {
  const tbody = document.getElementById('cd-contacts');
  if (!contacts.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state"><p>No contacts enrolled</p></td></tr>';
    return;
  }
  tbody.innerHTML = contacts.slice(0, 100).map(c => `
    <tr>
      <td class="mono" style="font-size:12px">${esc(c.email)}</td>
      <td>${esc(c.first_name)} ${esc(c.last_name)}</td>
      <td class="mono">${c.current_step}</td>
      <td>${enrollBadge(c.status)}</td>
    </tr>
  `).join('');
}

function addStepUI() {
  const steps = document.querySelectorAll('.step-card');
  const nextNum = steps.length + 1;
  document.getElementById('step-modal-title').textContent = `Add Step ${nextNum}`;
  document.getElementById('step-num-input').value = nextNum;
  document.getElementById('step-delay').value = nextNum === 1 ? 0 : 3;
  document.getElementById('step-subject').value = '';
  document.getElementById('step-body').value = '';
  openModal('modal-step');
}

async function editStep(stepNum) {
  const steps = await api(`/api/campaigns/${currentCampaignId}/steps`);
  const s = steps.find(x => x.step_num === stepNum);
  if (!s) return;
  document.getElementById('step-modal-title').textContent = `Edit Step ${stepNum}`;
  document.getElementById('step-num-input').value  = stepNum;
  document.getElementById('step-delay').value      = s.delay_days;
  document.getElementById('step-subject').value    = s.subject;
  document.getElementById('step-body').value       = s.body_html;
  openModal('modal-step');
}

async function saveStep() {
  const stepNum  = +document.getElementById('step-num-input').value;
  const subject  = document.getElementById('step-subject').value.trim();
  const body     = document.getElementById('step-body').value.trim();
  const delay    = +document.getElementById('step-delay').value;

  if (!subject || !body) { toast('Subject and body are required', 'err'); return; }

  await api(`/api/campaigns/${currentCampaignId}/steps`, 'POST', {
    step_num: stepNum, subject, body_html: body, delay_days: delay
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
