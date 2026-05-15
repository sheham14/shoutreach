function showSection(name) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav a').forEach(a => a.classList.remove('active'));
  document.getElementById('section-' + name)?.classList.add('active');
  document.querySelector(`nav a[data-section="${name}"]`)?.classList.add('active');

  if (name === 'dashboard')  refreshDashboard();
  if (name === 'campaigns')  loadCampaigns();
  if (name === 'contacts')   loadContacts();
  if (name === 'logs')       loadLogs();
  if (name === 'settings')   loadSettings();
  if (name === 'scraper')    loadScraper();
  if (name === 'database')   loadDbTables();
}

function toast(msg, type = 'ok') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `show ${type}`;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.className = '', 3000);
}

function openModal(id)  { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }

async function api(path, method = 'GET', body = null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  return res.json();
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function statusBadge(s) {
  const map = {
    active: 'badge-green', draft: 'badge-gray', paused: 'badge-amber',
    completed: 'badge-blue', error: 'badge-red'
  };
  return `<span class="badge ${map[s]||'badge-gray'}">${s}</span>`;
}

function enrollBadge(s) {
  const map = {
    queued: 'badge-blue', completed: 'badge-green', replied: 'badge-purple',
    bounced: 'badge-red', unsubscribed: 'badge-gray'
  };
  return `<span class="badge ${map[s]||'badge-gray'}">${s}</span>`;
}

function contactStatusBadge(s) {
  const map = { active: 'badge-green', bounced: 'badge-red', unsubscribed: 'badge-gray', deleted: 'badge-gray' };
  return `<span class="badge ${map[s]||'badge-gray'}">${s}</span>`;
}
