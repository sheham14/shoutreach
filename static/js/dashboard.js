// Wakes the background scheduler instead of waiting out its 60s tick: one
// queue pass plus a reply/bounce scan. It does not bypass any gate -- the
// send window, the daily caps and the bounce breaker all still apply, so
// outside sending hours this checks for replies and sends nothing.
const RUN_NOW_LABEL = '⟳ Check for replies & send';

async function runSchedulerNow() {
  const btn = document.getElementById('run-now-btn');
  btn.textContent = '⟳ Checking…';
  btn.disabled = true;
  try {
    // api() takes (path, method, body) -- passing { method: 'POST' } made the
    // method an object, which fetch stringifies to "[object Object]" and
    // rejects as an invalid HTTP method. Combined with showToast not existing
    // (the helper is toast), both the success and failure paths threw and the
    // button did nothing at all.
    const res = await api('/api/scheduler/run', 'POST');
    if (res && res.error) {
      toast(res.error, 'err');
      return;
    }
    // request_run_now only sets a flag; the thread picks it up on its next
    // pass, so say "started" rather than claiming the work is already done.
    toast('Checking for replies and sending anything due…');
    setTimeout(refreshDashboard, 1500);
  } catch (e) {
    toast('Could not start the check', 'err');
  } finally {
    btn.textContent = RUN_NOW_LABEL;
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
