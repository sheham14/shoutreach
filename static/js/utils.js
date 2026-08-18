function showSection(name) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav a').forEach(a => a.classList.remove('active'));
  document.getElementById('section-' + name)?.classList.add('active');
  document.querySelector(`nav a[data-section="${name}"]`)?.classList.add('active');

  if (name === 'dashboard')  refreshDashboard();
  if (name === 'campaigns')  loadCampaigns();
  if (name === 'contacts')   loadContacts();
  if (name === 'calling')    loadCalling();
  if (name === 'logs')       loadLogs();
  if (name === 'settings')   { loadSettings(); loadAccounts(); loadUsers(); }
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

// ── CSRF token bootstrap ──────────────────────────────────────────────────────
let _csrfToken = null;
let _csrfPromise = null;

async function _getCsrfToken() {
  if (_csrfToken) return _csrfToken;
  if (!_csrfPromise) {
    _csrfPromise = fetch('/api/csrf', { credentials: 'same-origin' })
      .then(r => r.ok ? r.json() : { csrf_token: '' })
      .then(d => { _csrfToken = d.csrf_token || ''; return _csrfToken; })
      .catch(() => { _csrfToken = ''; return ''; });
  }
  return _csrfPromise;
}

async function api(path, method = 'GET', body = null) {
  const opts = {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
  };
  if (method !== 'GET' && method !== 'HEAD') {
    opts.headers['X-CSRF-Token'] = await _getCsrfToken();
  }
  if (body) opts.body = JSON.stringify(body);

  let res = await fetch(path, opts);

  // If the token expired (server rotated session) try once more.
  if (res.status === 403 && (method !== 'GET' && method !== 'HEAD')) {
    _csrfToken = null; _csrfPromise = null;
    opts.headers['X-CSRF-Token'] = await _getCsrfToken();
    res = await fetch(path, opts);
  }

  if (res.status === 401) {
    window.location.href = '/login';
    return { error: 'Unauthorized' };
  }

  let data = {};
  try { data = await res.json(); } catch (_) { data = {}; }

  if (!res.ok && !data.error) {
    data.error = `Request failed (${res.status})`;
  }
  return data;
}

// HTML-text escape (for text content inside elements).
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/`/g, '&#96;');
}

// JavaScript string-literal escape — for values embedded in inline onclick
// handlers like onclick="doThing('${escj(name)}')". Returns the value safe
// to drop inside a single-quoted JS string inside an HTML attribute.
function escj(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, '\\x27')
    .replace(/"/g, '\\x22')
    .replace(/</g, '\\x3c')
    .replace(/>/g, '\\x3e')
    .replace(/&/g, '\\x26')
    .replace(/\r?\n/g, '\\n');
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

// no_website was missing from this map, so the strongest lead signal the
// scraper produces rendered as a grey "no_website" string — the one status
// worth spotting looked like the least important thing in the row.
const CONTACT_STATUS_META = {
  active:       { cls: 'badge-green', label: 'Active' },
  bounced:      { cls: 'badge-red',   label: 'Bounced' },
  unsubscribed: { cls: 'badge-gray',  label: 'Unsubscribed' },
  deleted:      { cls: 'badge-gray',  label: 'Deleted' },
  form_only:    { cls: 'badge-amber', label: 'Form only' },
  no_email:     { cls: 'badge-blue',  label: 'No email' },
  no_website:   { cls: 'badge-red',   label: 'No website' },
};

function contactStatusBadge(s) {
  const meta = CONTACT_STATUS_META[s] || { cls: 'badge-gray', label: s };
  return `<span class="badge ${meta.cls}">${esc(meta.label)}</span>`;
}

// The pill that rides next to the company name, rather than in the status
// column you have to scroll for. Only the states that change what you do:
// no website is the strongest pitch for a web-design offer, and a business
// with a site but no findable address is a call rather than an email.
// What a call left behind. Terminal outcomes drop a lead out of every calling
// queue, so without this a "not interested" contact was indistinguishable in
// Contacts from one nobody had ever dialled.
const CALL_STATUS_META = {
  no_answer:      { cls: 'badge-gray',   label: 'No answer' },
  voicemail:      { cls: 'badge-gray',   label: 'Voicemail' },
  callback:       { cls: 'badge-blue',   label: 'Callback' },
  interested:     { cls: 'badge-green',  label: 'Interested' },
  proposal_sent:  { cls: 'badge-green',  label: 'Proposal sent' },
  booked:         { cls: 'badge-green',  label: 'Booked' },
  not_interested: { cls: 'badge-red',    label: 'Not interested' },
  wrong_number:   { cls: 'badge-amber',  label: 'Wrong number' },
  do_not_call:    { cls: 'badge-red',    label: 'Do not call' },
};

function callStatusBadge(s) {
  if (!s) return '<span class="text-muted" style="font-size:11px">—</span>';
  // Custom outcomes are not in the map above, so fall back to reading the key
  // rather than printing raw snake_case at the user: follow_up_later becomes
  // "Follow up later" without needing to fetch the outcome list here.
  const meta = CALL_STATUS_META[s]
    || { cls: 'badge-gray', label: String(s).replace(/_/g, ' ').replace(/^./, m => m.toUpperCase()) };
  return `<span class="badge ${meta.cls}">${esc(meta.label)}</span>`;
}

function contactSignalPill(c) {
  if (c.status === 'no_website') {
    return `<span class="badge badge-red" style="margin-left:6px;font-size:10px"
                  title="Google Maps lists this business with no website at all">No website</span>`;
  }
  if (c.status === 'form_only' || c.status === 'no_email') {
    return `<span class="badge badge-amber" style="margin-left:6px;font-size:10px"
                  title="Has a website but no email address was found — reachable by phone">No email</span>`;
  }
  return '';
}
