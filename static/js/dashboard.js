// Wakes the background scheduler instead of waiting out its 60s tick: one
// queue pass plus a reply/bounce scan. It does not bypass any gate -- the
// send window, the daily caps and the bounce breaker all still apply, so
// outside sending hours this checks for replies and sends nothing.
const RUN_NOW_LABEL = '⟳ Check for email replies & send';

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


// ── Dashboard ────────────────────────────────────────────────────────────────

const CHANNEL_META = {
  email:    { label: 'Email',    cls: 'channel-email',    section: 'email' },
  calling:  { label: 'Calling',  cls: 'channel-calling',  section: 'calling' },
  whatsapp: { label: 'WhatsApp', cls: 'channel-whatsapp', section: 'whatsapp' },
};

function _miniStats(items) {
  return items.map(([label, value, tone]) => `
    <div class="mini-stat">
      <div class="label">${esc(label)}</div>
      <div class="value ${tone || ''}">${esc(value ?? 0)}</div>
    </div>`).join('');
}

// Every to-do tile opens the exact list it counts, so the number is a door
// rather than a fact to go and find.
function _todoTile(channel, n, what, go) {
  const meta = CHANNEL_META[channel];
  return `<button type="button" class="todo ${n ? '' : 'quiet'}" onclick="${go}">
      <span class="channel ${meta.cls}">${meta.label}</span>
      <span class="n">${n ?? 0}</span>
      <span class="what">${esc(what)}</span>
    </button>`;
}

function updateSidebarCounts(todo) {
  if (!todo) return;
  const wa = (todo.wa_ready || 0) + (todo.wa_due || 0);
  const calls = todo.calls_due || 0;
  const waEl = document.getElementById('nav-count-whatsapp');
  const callEl = document.getElementById('nav-count-calling');
  const emailEl = document.getElementById('nav-count-email');
  if (waEl) waEl.textContent = wa ? wa : '';
  if (callEl) callEl.textContent = calls ? calls : '';
  if (emailEl) emailEl.textContent = todo.email_replies ? todo.email_replies : '';
}

async function refreshDashboard() {
  const d = await api(`/api/dashboard?since=${encodeURIComponent(startOfLocalDay())}`);
  if (!d || d.error) { toast((d && d.error) || 'Could not load the dashboard', 'err'); return; }
  const t = d.todo || {};
  updateSidebarCounts(t);

  document.getElementById('dash-todo').innerHTML = [
    _todoTile('whatsapp', t.wa_ready, 'messages ready to send', "openWhatsAppTodo('ready')"),
    _todoTile('whatsapp', t.wa_due, 'follow-ups due', "openWhatsAppTodo('due')"),
    _todoTile('whatsapp', t.wa_sent_today, 'sent today', "openWhatsAppTodo('ready')"),
    _todoTile('calling', t.calls_due, 'callbacks due now', "openCallingTodo('today')"),
    _todoTile('calling', t.calls_new, 'never called', "openCallingTodo('new')"),
    _todoTile('email', t.email_replies, 'email replies to answer', "openEmailTodo()"),
  ].join('');

  const e = d.email || {};
  document.getElementById('dash-email').innerHTML = _miniStats([
    ['Sent', e.sent], ['Replies', e.replied, 'green'],
    ['Reply rate', `${e.reply_rate ?? 0}%`, 'green'], ['Sent today', e.today],
  ]);
  // The shared daily cap, which every operator's campaigns draw on (B-10).
  const cap = document.getElementById('dash-cap');
  if (cap) cap.textContent = e.all_accounts_limit
    ? `Sent today, all accounts: ${e.all_accounts_today ?? 0} of ${e.all_accounts_limit}` : '';
  _renderSender(d.sender);
  const c = d.calling || {};
  document.getElementById('dash-calling').innerHTML = _miniStats([
    ['Calls today', c.calls_today], ['Due now', c.due, c.due ? 'amber' : ''],
    ['Interested', c.interested, 'green'], ['Booked', c.booked, 'green'],
  ]);
  const w = d.whatsapp || {};
  document.getElementById('dash-whatsapp').innerHTML = _miniStats([
    ['Messaged', w.messaged], ['Replied', w.replied, 'green'],
    ['Reply rate', `${w.reply_rate ?? 0}%`, 'green'], ['Due', w.due, w.due ? 'amber' : ''],
  ]);

  _renderWebsiteBlock(d.website || []);

  const tbody = document.getElementById('dashboard-campaigns');
  const campaigns = d.campaigns || [];
  if (!campaigns.length) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="empty-state">
      <p>No campaigns yet. Start one from Email, Calling or WhatsApp.</p></div></td></tr>`;
    return;
  }
  tbody.innerHTML = campaigns.map(cp => {
    const meta = CHANNEL_META[cp.channel];
    const statusTone = { active: 'green', paused: 'amber', archived: '' }[cp.status] || '';
    return `<tr class="clickable" onclick="openAnyCampaign('${cp.channel}', ${cp.id})">
      <td><span class="biz-name">${esc(cp.name)}</span></td>
      <td><span class="${meta.cls}">${meta.label}</span></td>
      <td>${pill(cp.status, statusTone)}</td>
      <td class="num">${cp.leads}</td>
      <td><div class="bar" title="${cp.progress}%"><i style="width:${cp.progress}%"></i></div></td>
      <td class="nowrap">${esc(cp.result)}${cp.reply_rate ? ` <span class="text-muted">(${cp.reply_rate}%)</span>` : ''}</td>
      <td><button class="btn btn-ghost btn-sm">Open →</button></td>
      <td class="m-card">${mCard(`<span class="biz-name">${esc(cp.name)}</span>`,
        `<span class="${meta.cls}">${meta.label}</span> ${pill(cp.status, statusTone)} · ${cp.leads} leads · ${esc(cp.result)}`)}</td>
    </tr>`;
  }).join('');
}

// "Email sender last ran 2 minutes ago", or a warning when it hasn't run
// for ten minutes or the thread is down. last_pass_at is a UTC database-style
// timestamp ("YYYY-MM-DD HH:MM:SS").
function _renderSender(sender) {
  const el = document.getElementById('dash-sender');
  if (!el || !sender) return;
  const at = sender.last_pass_at ? new Date(sender.last_pass_at.replace(' ', 'T') + 'Z') : null;
  const mins = at ? Math.max(0, Math.round((Date.now() - at.getTime()) / 60000)) : null;
  const ago = mins === null ? '' : mins < 1 ? 'just now' : mins === 1 ? '1 minute ago'
    : mins < 120 ? `${mins} minutes ago` : `${Math.round(mins / 60)} hours ago`;
  const stale = !sender.alive || (mins !== null && mins > 10);
  el.style.color = '';
  if (!stale) {
    el.className = 'text-small text-muted';
    el.textContent = at ? `Email sender last ran ${ago}` : 'Email sender starting…';
    return;
  }
  el.className = 'text-small';
  el.style.color = 'var(--amber)';
  el.textContent = !sender.alive
    ? '⚠ The email sender isn\'t running. Reload this page to restart it; if this stays, check the server.'
    : `⚠ The email sender last ran ${ago}. Nothing is being sent.`;
}

// "From the website": recent enquiries and where each stands. Hidden until
// there's at least one, so an install without the websites never sees it.
const _WEB_STATUS_TONE = { Booked: 'green', Moved: 'amber', 'Booking cancelled': 'red',
                           'Followed up': '', "Didn't book yet": 'amber' };

function _renderWebsiteBlock(rows) {
  const card = document.getElementById('dash-website-card');
  if (!card) return;
  card.style.display = rows.length ? '' : 'none';
  document.getElementById('dash-website').innerHTML = rows.map(r => {
    const who = r.company || r.business_name || r.name;
    return `<tr class="clickable" onclick="openWebsiteLead(${r.business_id})">
      <td><span class="biz-name">${esc(who)}</span>${r.name && r.name !== who ? `<span class="sub">${esc(r.name)}</span>` : ''}</td>
      <td class="text-muted">${esc(r.site)}</td>
      <td class="nowrap">${esc(fmtWhen(r.created_at, 'utc'))}</td>
      <td>${pill(r.status, _WEB_STATUS_TONE[r.status] || '')}</td>
      <td class="m-card">${mCard(`<span class="biz-name">${esc(who)}</span> ${pill(r.status, _WEB_STATUS_TONE[r.status] || '')}`,
        `${esc(r.site)} · ${esc(fmtWhen(r.created_at, 'utc'))}`)}</td>
    </tr>`;
  }).join('');
}

function openWebsiteLead(businessId) {
  showSection('contacts');
  openContactDetail(businessId);
}

function openAnyCampaign(channel, id) {
  if (channel === 'email') { showSection('email'); openCampaign(id); return; }
  if (channel === 'calling') { openCallingCampaign(id); return; }
  if (channel === 'whatsapp') { openWhatsAppCampaign(id); }
}
