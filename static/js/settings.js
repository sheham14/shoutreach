const AI_MODELS = {
  claude: [
    {
      id: "claude-haiku-4-5-20251001",
      label: "Haiku 4.5 (~$1/ Input MTok + $5/ Output MTok)",
    },
    {
      id: "claude-sonnet-4-6",
      label: "Sonnet 4.6 (~$3/ Input MTok + $15/ Output MTok)",
    },
    {
      id: "claude-opus-4-7",
      label: "Opus 4.7 (~$5/ Input MTok + $25/ Output MTok)",
    },
  ],
  gemini: [
    {
      id: "gemini-3.1-flash-lite",
      label: "Flash-Lite 3.1 — Limited free tier",
    },
    {
      id: "gemini-3.1-flash-preview",
      label: "Flash 3.1 Preview — Limited free tier",
    },
    { id: "gemini-3.1-pro", label: "Pro 3.1 — No free tier" },
  ],
  openai: [
    {
      id: "gpt-5.4-mini",
      label: "GPT-5.4 Mini (~$0.75/ Input MTok + $4.50/ Output MTok)",
    },
    {
      id: "gpt-5.4",
      label: "GPT-5.4 (~$2.5/ Input MTok + $15/ Output MTok)",
    },
    {
      id: "gpt-5.5",
      label: "GPT-5.5 (~$5/ Input MTok + $30/ Output MTok)",
    },
  ],
};

function aiProviderChanged(keepModel) {
  const provider = document.getElementById("cfg-ai-provider").value;
  ["claude", "gemini", "openai"].forEach((p) => {
    document.getElementById(`cfg-key-group-${p}`).style.display =
      p === provider ? "" : "none";
  });
  const modelSel = document.getElementById("cfg-ai-model");
  const current = keepModel ? modelSel.value : null;
  modelSel.innerHTML = (AI_MODELS[provider] || [])
    .map(
      (m) =>
        `<option value="${m.id}" ${m.id === current ? "selected" : ""}>${m.label}</option>`,
    )
    .join("");
}

// ── The page ─────────────────────────────────────────────────────────────────
//
// My settings is everyone's; Team setup, Activity log and Database are for
// admins, and aren't shown to anyone else -- they'd only answer "Forbidden".

let _settingsMe = null;

async function loadSettingsPage() {
  const me = await api('/api/users/me');
  _settingsMe = me && !me.error ? me : null;
  const admin = !!(_settingsMe && _settingsMe.is_admin);
  document.querySelectorAll('#section-settings .admin-only').forEach(el => { el.style.display = admin ? '' : 'none'; });
  const name = document.getElementById('cfg-me-name');
  if (name) name.textContent = (_settingsMe && _settingsMe.username) || '';
  let tab = window._settingsTab || currentTab('settings', 'mine');
  window._settingsTab = null;
  if (!admin) tab = 'mine';
  setTab('settings', tab);
  loadSettings();
  if (admin) { loadAccounts(); loadUsers(); loadWebsiteSettings(); }
  loadWebsiteFollowup();
}

// ── Website and bookings (Plan 2) ────────────────────────────────────────────

async function copyField(id) {
  const input = document.getElementById(id);
  try { await navigator.clipboard.writeText(input.value); toast('Copied'); }
  catch (_) { input.select(); toast('Select and copy it by hand', 'err'); }
}

async function loadWebsiteSettings() {
  const c = await api('/api/website-settings');
  if (!c || c.error) return;
  document.getElementById('cfg-web-lead-url').value = c.website_lead_url;
  document.getElementById('cfg-cal-url').value = c.cal_webhook_url;
  document.getElementById('cfg-web-key-state').textContent = c.has_website_key
    ? 'Set. It was shown once, when it was made.' : 'Not made yet — the sites can\'t send leads.';
  document.getElementById('cfg-web-key-btn').textContent = c.has_website_key ? 'Replace key' : 'Make a key';
  document.getElementById('cfg-cal-secret-state').textContent = c.has_cal_secret
    ? 'Set. It was shown once, when it was made.' : 'Not made yet — bookings can\'t be received.';
  document.getElementById('cfg-cal-secret-btn').textContent = c.has_cal_secret ? 'Replace secret' : 'Make a secret';
  for (const site of c.sites_list) {
    const sel = document.getElementById(`cfg-web-owner-${site}`);
    sel.innerHTML = '<option value="0">Nobody yet</option>' +
      c.users.map(u => `<option value="${u.id}">${esc(u.username)}</option>`).join('');
    sel.value = String(c.sites[site].owner_id || 0);
    document.getElementById(`cfg-web-country-${site}`).value = c.sites[site].country || '';
  }
  document.getElementById('cfg-web-wa-wait').value = c.wa_wait_minutes;
  document.getElementById('cfg-web-email-wait').value = c.email_wait_hours;
  document.getElementById('cfg-web-cal-link').value = c.cal_booking_link || '';
}

async function saveWebsiteSettings() {
  const sites = {};
  for (const site of ['hexiv.co.uk', 'hexiv.co']) {
    sites[site] = {
      owner_id: +document.getElementById(`cfg-web-owner-${site}`).value || 0,
      country: document.getElementById(`cfg-web-country-${site}`).value.trim(),
    };
  }
  const res = await api('/api/website-settings', 'PUT', {
    sites,
    wa_wait_minutes: document.getElementById('cfg-web-wa-wait').value,
    email_wait_hours: document.getElementById('cfg-web-email-wait').value,
    cal_booking_link: document.getElementById('cfg-web-cal-link').value.trim(),
  });
  if (!res || res.error || res.ok === false) { toast((res && res.error) || 'Could not save', 'err'); return; }
  toast('Saved');
  loadWebsiteSettings();
}

// A key or secret is shown this once, in the app's own dialog, to copy.
async function _showOnce(title, value, where) {
  await chooseDialog({
    title,
    body: `<p class="text-small" style="margin-bottom:10px">${where} It won't be shown again.</p>
      <div class="flex gap-2"><input id="once-value" readonly class="mono" style="flex:1;font-size:12px"
        value="${esc(value)}" /><button class="btn btn-ghost btn-sm" onclick="copyField('once-value')">Copy</button></div>`,
    confirm: 'Done',
  });
}

async function rotateWebsiteKey() {
  const had = document.getElementById('cfg-web-key-btn').textContent.startsWith('Replace');
  if (had && !await chooseDialog({ title: 'Replace the website key?', danger: true, confirm: 'Replace',
      body: '<p>The sites stop sending leads until each one has the new key.</p>' })) return;
  const res = await api('/api/website-settings/key', 'POST');
  if (!res || !res.key) { toast((res && res.error) || 'Could not make a key', 'err'); return; }
  await _showOnce('Website key', res.key, 'Put this in each site\'s hosting as a server-only setting.');
  loadWebsiteSettings();
}

async function rotateCalSecret() {
  const had = document.getElementById('cfg-cal-secret-btn').textContent.startsWith('Replace');
  if (had && !await chooseDialog({ title: 'Replace the Cal.com secret?', danger: true, confirm: 'Replace',
      body: '<p>Bookings are refused until the new secret is pasted into Cal.com\'s webhook.</p>' })) return;
  const res = await api('/api/website-settings/cal-secret', 'POST');
  if (!res || !res.secret) { toast((res && res.error) || 'Could not make a secret', 'err'); return; }
  await _showOnce('Cal.com webhook secret', res.secret, 'Paste this into the webhook in Cal.com.');
  loadWebsiteSettings();
}

async function loadWebsiteFollowup() {
  const [prefs, emailCamps, waCamps, notify] = await Promise.all([
    api('/api/website-followup'), api('/api/campaigns'), api('/api/wa/campaigns'), api('/api/notify-email')]);
  const ne = document.getElementById('cfg-notify-email');
  if (ne) ne.value = (notify && notify.email) || '';
  const fill = (id, none, list, chosen) => {
    const sel = document.getElementById(id);
    if (!sel) return;
    sel.innerHTML = `<option value="">${none}</option>` + (Array.isArray(list) ? list : [])
      .map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
    sel.value = chosen ? String(chosen) : '';
  };
  fill('cfg-wf-email', "Don't email", emailCamps, prefs && prefs.email_campaign_id);
  fill('cfg-wf-wa', "Don't add to WhatsApp", waCamps, prefs && prefs.wa_campaign_id);
}

async function saveWebsiteFollowup() {
  const [res, res2] = await Promise.all([
    api('/api/website-followup', 'PUT', {
      email_campaign_id: document.getElementById('cfg-wf-email').value || null,
      wa_campaign_id: document.getElementById('cfg-wf-wa').value || null,
    }),
    api('/api/notify-email', 'PUT', { email: document.getElementById('cfg-notify-email').value.trim() }),
  ]);
  const bad = [res, res2].find(r => !r || r.ok === false || r.error);
  if (bad) { toast((bad && bad.error) || 'Could not save', 'err'); return; }
  toast('Saved');
}

// From Activity log or Database, back to a Settings tab.
function openSettingsTab(name) {
  window._settingsTab = name;
  showSection('settings');
}

function changeMyPassword() {
  if (!_settingsMe) return;
  openChangePasswordModal(_settingsMe.id, _settingsMe.username);
}

async function loadSettings() {
  // Both shell variants show the origin; PowerShell quotes it, cmd does not.
  ["cfg-worker-origin", "cfg-worker-origin-cmd"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = window.location.origin;
  });

  const s = await api("/api/settings");
  document.getElementById("email-checking-toggle").checked =
    (s.email_checking_enabled ?? "1") === "1";
  document.getElementById("cfg-global-cap").value = s.global_daily_cap || "200";
  document.getElementById("cfg-base-url").value =
    s.app_base_url || "http://localhost:5000";
  document.getElementById("cfg-include-unsub").checked =
    (s.include_unsubscribe ?? "1") === "1";
  document.getElementById("cfg-company-address").value =
    s.company_address || "";
  document.getElementById("cfg-ai-enabled").checked =
    s.ai_features_enabled === "1";
  const provider = s.ai_provider || "claude";
  document.getElementById("cfg-ai-provider").value = provider;
  aiProviderChanged(false);
  // Restore saved model after populating the dropdown
  const savedModel = s.ai_model || "";
  if (savedModel) {
    const opt = document.querySelector(
      `#cfg-ai-model option[value="${savedModel}"]`,
    );
    if (opt) opt.selected = true;
  }
  document.getElementById("cfg-anthropic-key").value = s.anthropic_api_key
    ? "●●●●●●●●●●●●"
    : "";
  document.getElementById("cfg-gemini-key").value = s.gemini_api_key
    ? "●●●●●●●●●●●●"
    : "";
  document.getElementById("cfg-openai-key").value = s.openai_api_key
    ? "●●●●●●●●●●●●"
    : "";
  document.getElementById("cfg-google-key").value = s.google_api_key
    ? "●●●●●●●●●●●●"
    : "";
  renderAuditLinks(await loadAuditLinks(true));
}

// ── Audit links ───────────────────────────────────────────────────────────────

function renderAuditLinks(links) {
  const wrap = document.getElementById("cfg-audit-links");
  wrap.innerHTML = "";
  links.forEach(l => addAuditLinkRow(l.label, l.url, false));
  if (!links.length) addAuditLinkRow("", "", false);
}

function addAuditLinkRow(label = "", url = "", focus = true) {
  const row = document.createElement("div");
  row.className = "audit-link-row";
  row.style.cssText = "display:flex;gap:6px;align-items:center";
  row.innerHTML = `
    <input class="al-label soft-input" placeholder="Semrush" value="${esc(label)}" style="flex:1" />
    <input class="al-url soft-input mono" placeholder="https://…{domain}" value="${esc(url)}" style="flex:3;font-size:12.5px" />
    <button class="btn btn-ghost btn-sm" onclick="this.closest('.audit-link-row').remove()" title="Remove" aria-label="Remove">${icon('x')}</button>`;
  document.getElementById("cfg-audit-links").appendChild(row);
  if (focus) row.querySelector(".al-label").focus();
}

async function saveAuditLinks() {
  const links = [...document.querySelectorAll("#cfg-audit-links .audit-link-row")]
    .map(r => ({ label: r.querySelector(".al-label").value.trim(), url: r.querySelector(".al-url").value.trim() }))
    .filter(l => l.label || l.url);
  const res = await api("/api/audit-links", "PUT", { links });
  if (!res || res.error) { toast((res && res.error) || "Could not save the links", "err"); return; }
  _auditLinks = res.links;
  renderAuditLinks(res.links);
  toast("Audit links saved");
}

async function saveSettings() {
  const claudeKey = document.getElementById("cfg-anthropic-key").value;
  const geminiKey = document.getElementById("cfg-gemini-key").value;
  const openaiKey = document.getElementById("cfg-openai-key").value;
  const googleKey = document.getElementById("cfg-google-key").value;
  const payload = {
    global_daily_cap: document.getElementById("cfg-global-cap").value,
    app_base_url: document.getElementById("cfg-base-url").value,
    include_unsubscribe: document.getElementById("cfg-include-unsub").checked
      ? "1"
      : "0",
    company_address: document
      .getElementById("cfg-company-address")
      .value.trim(),
    ai_features_enabled: document.getElementById("cfg-ai-enabled").checked
      ? "1"
      : "0",
    ai_provider: document.getElementById("cfg-ai-provider").value,
    ai_model: document.getElementById("cfg-ai-model").value,
  };
  if (claudeKey && !claudeKey.startsWith("●"))
    payload.anthropic_api_key = claudeKey.trim();
  if (geminiKey && !geminiKey.startsWith("●"))
    payload.gemini_api_key = geminiKey.trim();
  if (openaiKey && !openaiKey.startsWith("●"))
    payload.openai_api_key = openaiKey.trim();
  if (googleKey && !googleKey.startsWith("●"))
    payload.google_api_key = googleKey.trim();
  await api("/api/settings", "POST", payload);
  window._aiEnabled = payload.ai_features_enabled === "1";
  toast("Settings saved");
}

// ── Users ─────────────────────────────────────────────────────────────────────

async function loadUsers() {
  const me = await api("/api/users/me");
  // Show sidebar username
  const usernameEl = document.getElementById("sidebar-username");
  if (usernameEl) usernameEl.textContent = me.username;

  if (!me.is_admin) return; // non-admins don't see the users card
  document.getElementById("settings-users-card").style.display = "block";

  const users = await api("/api/users");
  const tbody = document.getElementById("users-table");
  tbody.innerHTML = users
    .map(
      (u) => `
    <tr>
      <td style="font-weight:500">${esc(u.username)}</td>
      <td>${u.is_admin ? '<span class="badge badge-blue">Admin</span>' : '<span class="badge">User</span>'}</td>
      <td class="tnum text-muted" style="font-size:11px">${esc(fmtWhen(u.created_at, 'utc', { dateOnly: true }))}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-ghost btn-sm" onclick="openChangePasswordModal(${u.id}, '${escj(u.username)}')">Change password</button>
        ${u.id !== me.id ? `<button class="btn btn-icon btn-sm" title="Delete user" aria-label="Delete user" onclick="deleteUser(${u.id}, '${escj(u.username)}')">${icon('x')}</button>` : ""}
      </td>
    </tr>
  `,
    )
    .join("");
}

function openAddUserModal() {
  document.getElementById("new-user-username").value = "";
  document.getElementById("new-user-password").value = "";
  document.getElementById("new-user-admin").checked = false;
  openModal("modal-add-user");
}

async function saveNewUser() {
  const username = document.getElementById("new-user-username").value.trim();
  const password = document.getElementById("new-user-password").value;
  const is_admin = document.getElementById("new-user-admin").checked;
  if (!username || !password) {
    toast("Username and password required", "err");
    return;
  }
  if (password.length < 12) {
    toast("Password must be at least 12 characters", "err");
    return;
  }
  const res = await api("/api/users", "POST", { username, password, is_admin });
  if (res.error) {
    toast(res.error, "err");
    return;
  }
  toast("User created");
  closeModal("modal-add-user");
  loadUsers();
}

async function openChangePasswordModal(uid, username) {
  document.getElementById("chpw-uid").value = uid;
  document.getElementById("chpw-username").textContent = username;
  document.getElementById("chpw-password").value = "";
  document.getElementById("chpw-current").value = "";
  // Show the "current password" field only when changing one's own password.
  const me = await api("/api/users/me");
  const isSelf = me && me.id === uid;
  document.getElementById("chpw-current-group").style.display = isSelf ? "" : "none";
  openModal("modal-change-password");
}

async function submitChangePassword() {
  const uid = +document.getElementById("chpw-uid").value;
  const password = document.getElementById("chpw-password").value;
  const current  = document.getElementById("chpw-current").value;
  if (!password) {
    toast("Enter a new password", "err");
    return;
  }
  if (password.length < 12) {
    toast("New password must be at least 12 characters", "err");
    return;
  }
  const me = await api("/api/users/me");
  const isSelf = me && me.id === uid;
  if (isSelf && !current) {
    toast("Enter your current password", "err");
    return;
  }
  const body = { password };
  if (isSelf) body.current_password = current;
  const res = await api(`/api/users/${uid}/password`, "POST", body);
  if (res.error) {
    toast(res.error, "err");
    return;
  }
  toast("Password changed");
  closeModal("modal-change-password");
}

async function deleteUser(uid, username) {
  if (!await confirmDialog({
    title: `Delete user "${username}"?`,
    text: 'This cannot be undone.',
    confirm: 'Delete user', danger: true,
  })) return;
  const res = await api(`/api/users/${uid}`, "DELETE");
  // Refused while they still own leads or campaigns — say so rather than
  // reporting success and leaving the account sitting there.
  if (!res || res.error) {
    toast((res && res.error) || "Could not delete that user", "err");
    return;
  }
  toast("User deleted");
  loadUsers();
}

// ── Email Accounts ────────────────────────────────────────────────────────────

let _accountEditId = null;

async function loadAccounts() {
  const accounts = await api("/api/accounts");
  const tbody = document.getElementById("accounts-table");
  if (!accounts.length) {
    tbody.innerHTML =
      '<tr><td colspan="5" class="empty-state"><p>No accounts configured.</p></td></tr>';
    return;
  }
  tbody.innerHTML = accounts
    .map((a) => {
      const paused = a.status !== "active";
      return `
    <tr>
      <td>${esc(a.name)}</td>
      <td class="tnum" style="font-size:12px">${esc(a.email)}</td>
      <td class="tnum text-muted" style="font-size:11px">${esc(a.smtp_host)}</td>
      <td>${paused ? '<span class="badge badge-gray">paused</span>' : '<span class="badge badge-green">active</span>'}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-ghost btn-sm" onclick="testAccountSMTPById(${a.id})">Test</button>
        <button class="btn btn-ghost btn-sm" onclick="toggleAccountStatus(${a.id}, ${paused})"
                title="${paused ? "Resume sending from this account" : "Stop sending from this account, without deleting it"}">
          ${paused ? `${icon("play", { size: 14 })} Activate` : `${icon("pause", { size: 14 })} Pause`}
        </button>
        <button class="btn btn-ghost btn-sm" onclick="openEditAccountModal(${a.id})" title="Edit account" aria-label="Edit account">${icon('pencil', { size: 14 })}</button>
        <button class="btn btn-icon btn-sm" title="Delete account" aria-label="Delete account" onclick="deleteAccount(${a.id})">${icon('x')}</button>
        <span id="acct-row-result-${a.id}" class="tnum" style="font-size:11px;margin-left:8px"></span>
      </td>
    </tr>
  `;
    })
    .join("");
}

// A paused account is skipped when a campaign picks its next sender
// (get_next_account_for_campaign filters on status), so this genuinely stops
// sending from it -- previously the only way to do that was to delete it and
// lose the credentials.
async function toggleAccountStatus(id, currentlyPaused) {
  const next = currentlyPaused ? "active" : "paused";
  const res = await api(`/api/accounts/${id}`, "PUT", { status: next });
  if (res && res.error) {
    toast(res.error, "err");
    return;
  }
  toast(next === "active" ? "Account activated" : "Account paused — it will not send");
  loadAccounts();
}

function openAddAccountModal() {
  _accountEditId = null;
  document.getElementById("account-modal-title").textContent =
    "Add an email account";
  [
    "acct-name",
    "acct-email",
    "acct-from-name",
    "acct-smtp-host",
    "acct-smtp-user",
    "acct-smtp-pass",
    "acct-imap-host",
    "acct-imap-user",
    "acct-imap-pass",
  ].forEach((id) => {
    document.getElementById(id).value = "";
  });
  ["acct-smtp-pass", "acct-imap-pass"].forEach((id) => {
    document.getElementById(id).placeholder = "app-specific password";
  });
  document.getElementById("acct-smtp-port").value = "587";
  document.getElementById("acct-smtp-result").textContent = "";
  document.getElementById("acct-imap-result").textContent = "";
  _resetPasswordReveal();
  openModal("modal-account");
}

// Never carry a revealed password over from the last time the modal was open.
function _resetPasswordReveal() {
  [
    ["acct-smtp-pass", "acct-smtp-pass-toggle"],
    ["acct-imap-pass", "acct-imap-pass-toggle"],
  ].forEach(([inputId, buttonId]) => {
    const input = document.getElementById(inputId);
    const btn = document.getElementById(buttonId);
    if (input) input.type = "password";
    if (btn) btn.textContent = "Show";
  });
}

async function openEditAccountModal(id) {
  const accounts = await api("/api/accounts");
  const a = accounts.find((x) => x.id === id);
  if (!a) return;
  _accountEditId = id;
  document.getElementById("account-modal-title").textContent = "Edit account";
  document.getElementById("acct-name").value = a.name || "";
  document.getElementById("acct-email").value = a.email || "";
  document.getElementById("acct-from-name").value = a.from_name || "";
  document.getElementById("acct-smtp-host").value = a.smtp_host || "";
  document.getElementById("acct-smtp-port").value = a.smtp_port || 587;
  document.getElementById("acct-smtp-user").value = a.smtp_user || "";
  document.getElementById("acct-imap-host").value = a.imap_host || "";
  document.getElementById("acct-imap-user").value = a.imap_user || "";
  // Blank, not the mask. Prefilling ●●●●●● meant pressing Show revealed six
  // bullet characters, which reads as though that IS the password. Blank with
  // a placeholder says what is actually true: leave it and nothing changes.
  ["acct-smtp-pass", "acct-imap-pass"].forEach((id) => {
    const el = document.getElementById(id);
    el.value = "";
    el.placeholder = "leave blank to keep current password";
  });
  document.getElementById("acct-smtp-result").textContent = "";
  document.getElementById("acct-imap-result").textContent = "";
  _resetPasswordReveal();
  openModal("modal-account");
}

function _accountPayload() {
  return {
    name: document.getElementById("acct-name").value.trim(),
    email: document.getElementById("acct-email").value.trim(),
    from_name: document.getElementById("acct-from-name").value.trim(),
    smtp_host: document.getElementById("acct-smtp-host").value.trim(),
    smtp_port: +document.getElementById("acct-smtp-port").value,
    smtp_user: document.getElementById("acct-smtp-user").value.trim(),
    smtp_pass: document.getElementById("acct-smtp-pass").value,
    imap_host: document.getElementById("acct-imap-host").value.trim(),
    imap_user: document.getElementById("acct-imap-user").value.trim(),
    imap_pass: document.getElementById("acct-imap-pass").value,
  };
}

async function saveAccount() {
  const payload = _accountPayload();
  if (!payload.name || !payload.email) {
    toast("Name and email are required", "err");
    return;
  }
  if (_accountEditId) {
    await api(`/api/accounts/${_accountEditId}`, "PUT", payload);
    toast("Account updated");
  } else {
    await api("/api/accounts", "POST", payload);
    toast("Account added");
  }
  closeModal("modal-account");
  loadAccounts();
}

async function deleteAccount(id) {
  if (!await confirmDialog({
    title: 'Delete this sending account?',
    text: 'It is removed from every campaign that uses it.',
    confirm: 'Delete account', danger: true,
  })) return;
  await api(`/api/accounts/${id}`, "DELETE");
  toast("Account deleted");
  loadAccounts();
}

// Tests what is on screen, including a password you just pasted. It used to
// send only the account id, so the server tested the SAVED credentials -- a
// freshly pasted app password reported "authentication failed" for the old
// one, and a new account could not be tested at all before saving.
async function testAccountSMTP() {
  const el = document.getElementById("acct-smtp-result");
  const p = _accountPayload();
  if (!p.smtp_host) {
    el.textContent = "Enter an SMTP host first";
    el.style.color = "var(--danger)";
    return;
  }
  el.textContent = "Testing...";
  el.style.color = "var(--text-2)";
  const res = await api("/api/accounts/test-smtp", "POST", {
    id: _accountEditId,
    smtp_host: p.smtp_host,
    smtp_port: p.smtp_port,
    smtp_user: p.smtp_user,
    smtp_pass: p.smtp_pass,
  });
  el.textContent = res.message || res.error || "No response";
  el.style.color = res.ok ? "var(--success)" : "var(--danger)";
}

// Tests a saved account from the list. This used to write its result into the
// modal's result span, which is not on screen when the modal is closed, and
// only raised a toast on failure -- so a successful test from the list looked
// like the button did nothing at all.
async function testAccountSMTPById(id) {
  const el = document.getElementById(`acct-row-result-${id}`);
  if (el) {
    el.textContent = "testing…";
    el.style.color = "var(--text-2)";
  }
  const res = await api(`/api/accounts/${id}/test-smtp`, "POST");
  const ok = !!(res && res.ok);
  const msg = (res && (res.message || res.error)) || "No response";
  if (el) {
    el.innerHTML = ok ? `${icon("check", { size: 14 })} SMTP ok` : `${icon("x", { size: 14 })} failed`;
    el.style.color = ok ? "var(--success)" : "var(--danger)";
    // Clear the marker after a while so a stale "SMTP ok" is never mistaken for the
    // result of a later change.
    setTimeout(() => {
      if (el.isConnected) el.textContent = "";
    }, 15000);
  }
  toast(msg, ok ? "ok" : "err");
}

// Reveal a password field so a pasted app password can be eyeballed before
// saving -- mistyped credentials are otherwise indistinguishable from a
// provider-side rejection.
function togglePasswordField(inputId, buttonId) {
  const input = document.getElementById(inputId);
  const btn = document.getElementById(buttonId);
  if (!input) return;
  const hidden = input.type === "password";
  input.type = hidden ? "text" : "password";
  if (btn) btn.textContent = hidden ? "Hide" : "Show";
}

async function testAccountIMAP() {
  const el = document.getElementById("acct-imap-result");
  const p = _accountPayload();
  if (!p.imap_host) {
    el.textContent = "Enter an IMAP host first";
    el.style.color = "var(--danger)";
    return;
  }
  el.textContent = "Testing...";
  el.style.color = "var(--text-2)";
  const res = await api("/api/accounts/test-imap", "POST", {
    id: _accountEditId,
    imap_host: p.imap_host,
    imap_user: p.imap_user,
    imap_pass: p.imap_pass,
  });
  el.textContent = res.message || res.error || "No response";
  el.style.color = res.ok ? "var(--success)" : "var(--danger)";
}

// ── Lead scraper worker key ──────────────────────────────────────────────────
// The worker runs on the operator's own machine (the server has no display for
// CAPTCHA solving), so it authenticates with this key instead of a session.

let _workerKeyShown = false;

async function _fetchWorkerKey() {
  const res = await api('/api/settings/worker-key');
  return res.key || '';
}

async function revealWorkerKey() {
  const input = document.getElementById('cfg-worker-key');
  const btn = document.getElementById('cfg-worker-reveal');
  if (_workerKeyShown) {
    input.type = 'password';
    input.value = '••••••••••••';
    btn.textContent = 'Show';
    _workerKeyShown = false;
    return;
  }
  input.value = await _fetchWorkerKey();
  input.type = 'text';
  btn.textContent = 'Hide';
  _workerKeyShown = true;
}

async function copyWorkerKey() {
  const key = await _fetchWorkerKey();
  try {
    await navigator.clipboard.writeText(key);
    toast('Worker key copied');
  } catch (e) {
    // Clipboard needs a secure context; fall back to showing it to copy by hand.
    const input = document.getElementById('cfg-worker-key');
    input.type = 'text';
    input.value = key;
    input.select();
    toast('Select and copy the key', 'err');
  }
}

async function rotateWorkerKey() {
  if (!await confirmDialog({
    title: 'Rotate your worker key?',
    text: 'Your worker will stop being able to connect until you give it the new key. Only your worker is affected.',
    confirm: 'Rotate key', danger: true,
  })) return;
  const res = await api('/api/settings/worker-key', 'POST');
  const input = document.getElementById('cfg-worker-key');
  input.type = 'text';
  input.value = res.key || '';
  document.getElementById('cfg-worker-reveal').textContent = 'Hide';
  _workerKeyShown = true;
  toast('Key rotated — update your worker');
}

async function toggleEmailChecking(enabled) {
  const res = await api("/api/settings", "POST", { email_checking_enabled: enabled ? "1" : "0" });
  if (!res || res.error) {
    toast((res && res.error) || "Could not save", "err");
    document.getElementById("email-checking-toggle").checked = !enabled; // revert
    return;
  }
  toast(enabled
    ? "Will check for replies & bounces every 5 minutes"
    : "Automatic checking off — the sidebar button still checks on demand");
}
