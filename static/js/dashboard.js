async function runSchedulerNow() {
  const btn = document.getElementById('run-now-btn');
  btn.textContent = '⟳ Running…';
  btn.disabled = true;
  try {
    await api('/api/scheduler/run', { method: 'POST' });
    showToast('Scheduler ran — replies and queue refreshed', 'success');
    refreshDashboard();
  } catch (e) {
    showToast('Scheduler run failed', 'error');
  } finally {
    btn.textContent = '⟳ Run Now';
    btn.disabled = false;
  }
}

async function refreshDashboard() {
  const s = await api('/api/stats');
  document.getElementById('s-total').textContent   = s.total;
  document.getElementById('s-sent').textContent    = s.sent;
  document.getElementById('s-replied').textContent = s.replied;
  document.getElementById('s-rrate').textContent   = s.reply_rate + '%';
  document.getElementById('s-today').textContent   = s.today;
  document.getElementById('s-bounced').textContent = s.bounced;

  const campaigns = await api('/api/campaigns');
  const tbody = document.getElementById('dashboard-campaigns');
  if (!campaigns.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state"><p>No campaigns yet</p></td></tr>';
    return;
  }
  tbody.innerHTML = campaigns.map(c => `
    <tr>
      <td><strong>${esc(c.name)}</strong></td>
      <td>${statusBadge(c.status)}</td>
      <td class="mono">${c.contact_count}</td>
      <td class="mono">${c.sent_count}</td>
      <td class="mono">${c.reply_rate}%</td>
      <td class="mono">${c.daily_limit}/day</td>
      <td><button class="btn btn-ghost btn-sm" onclick="openCampaign(${c.id})">Open →</button></td>
    </tr>
  `).join('');
}
