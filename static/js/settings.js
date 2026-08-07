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

async function loadSettings() {
  // Both shell variants show the origin; PowerShell quotes it, cmd does not.
  ["cfg-worker-origin", "cfg-worker-origin-cmd"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = window.location.origin;
  });

  const s = await api("/api/settings");
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
}

async function saveSettings() {
  const claudeKey = document.getElementById("cfg-anthropic-key").value;
  const geminiKey = document.getElementById("cfg-gemini-key").value;
  const openaiKey = document.getElementById("cfg-openai-key").value;
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
  await api("/api/settings", "POST", payload);
  toast("Settings saved ✓");
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
      <td class="mono text-muted" style="font-size:11px">${(u.created_at || "").substring(0, 10)}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-ghost btn-sm" onclick="openChangePasswordModal(${u.id}, '${escj(u.username)}')">Change password</button>
        ${u.id !== me.id ? `<button class="btn btn-danger btn-sm" onclick="deleteUser(${u.id}, '${escj(u.username)}')">✕</button>` : ""}
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
  const res = await api("/api/users", "POST", { username, password, is_admin });
  if (res.error) {
    toast(res.error, "err");
    return;
  }
  toast("User created ✓");
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
  toast("Password changed ✓");
  closeModal("modal-change-password");
}

async function deleteUser(uid, username) {
  if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return;
  await api(`/api/users/${uid}`, "DELETE");
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
      <td class="mono" style="font-size:12px">${esc(a.email)}</td>
      <td class="mono text-muted" style="font-size:11px">${esc(a.smtp_host)}</td>
      <td>${paused ? '<span class="badge badge-gray">paused</span>' : '<span class="badge badge-green">active</span>'}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-ghost btn-sm" onclick="testAccountSMTPById(${a.id})">Test</button>
        <button class="btn btn-ghost btn-sm" onclick="toggleAccountStatus(${a.id}, ${paused})"
                title="${paused ? "Resume sending from this account" : "Stop sending from this account, without deleting it"}">
          ${paused ? "▶ Activate" : "⏸ Pause"}
        </button>
        <button class="btn btn-ghost btn-sm" onclick="openEditAccountModal(${a.id})">✎</button>
        <button class="btn btn-danger btn-sm" onclick="deleteAccount(${a.id})">✕</button>
        <span id="acct-row-result-${a.id}" class="mono" style="font-size:11px;margin-left:8px"></span>
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
  toast(next === "active" ? "Account activated ✓" : "Account paused — it will not send");
  loadAccounts();
}

function openAddAccountModal() {
  _accountEditId = null;
  document.getElementById("account-modal-title").textContent =
    "Add Email Account";
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
  document.getElementById("account-modal-title").textContent = "Edit Account";
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
    toast("Account updated ✓");
  } else {
    await api("/api/accounts", "POST", payload);
    toast("Account added ✓");
  }
  closeModal("modal-account");
  loadAccounts();
}

async function deleteAccount(id) {
  if (!confirm("Delete this account? It will be removed from all campaigns."))
    return;
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
    el.style.color = "var(--red)";
    return;
  }
  el.textContent = "Testing...";
  el.style.color = "var(--muted)";
  const res = await api("/api/accounts/test-smtp", "POST", {
    id: _accountEditId,
    smtp_host: p.smtp_host,
    smtp_port: p.smtp_port,
    smtp_user: p.smtp_user,
    smtp_pass: p.smtp_pass,
  });
  el.textContent = res.message || res.error || "No response";
  el.style.color = res.ok ? "var(--green)" : "var(--red)";
}

// Tests a saved account from the list. This used to write its result into the
// modal's result span, which is not on screen when the modal is closed, and
// only raised a toast on failure -- so a successful test from the list looked
// like the button did nothing at all.
async function testAccountSMTPById(id) {
  const el = document.getElementById(`acct-row-result-${id}`);
  if (el) {
    el.textContent = "testing…";
    el.style.color = "var(--muted)";
  }
  const res = await api(`/api/accounts/${id}/test-smtp`, "POST");
  const ok = !!(res && res.ok);
  const msg = (res && (res.message || res.error)) || "No response";
  if (el) {
    el.textContent = ok ? "✓ SMTP ok" : "✕ failed";
    el.style.color = ok ? "var(--green)" : "var(--red)";
    // Clear the marker after a while so a stale ✓ is never mistaken for the
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
    el.style.color = "var(--red)";
    return;
  }
  el.textContent = "Testing...";
  el.style.color = "var(--muted)";
  const res = await api("/api/accounts/test-imap", "POST", {
    id: _accountEditId,
    imap_host: p.imap_host,
    imap_user: p.imap_user,
    imap_pass: p.imap_pass,
  });
  el.textContent = res.message || res.error || "No response";
  el.style.color = res.ok ? "var(--green)" : "var(--red)";
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
  if (!confirm('Rotate the worker key?\n\nAny worker still using the old key will stop being able to connect until you update it.')) return;
  const res = await api('/api/settings/worker-key', 'POST');
  const input = document.getElementById('cfg-worker-key');
  input.type = 'text';
  input.value = res.key || '';
  document.getElementById('cfg-worker-reveal').textContent = 'Hide';
  _workerKeyShown = true;
  toast('Key rotated — update your worker');
}
