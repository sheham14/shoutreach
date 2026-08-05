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
  const originEl = document.getElementById("cfg-worker-origin");
  if (originEl) originEl.textContent = window.location.origin;

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
    .map(
      (a) => `
    <tr>
      <td>${esc(a.name)}</td>
      <td class="mono" style="font-size:12px">${esc(a.email)}</td>
      <td class="mono text-muted" style="font-size:11px">${esc(a.smtp_host)}</td>
      <td>${a.status === "active" ? '<span class="badge badge-green">active</span>' : '<span class="badge badge-gray">paused</span>'}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-ghost btn-sm" onclick="testAccountSMTPById(${a.id})">Test</button>
        <button class="btn btn-ghost btn-sm" onclick="openEditAccountModal(${a.id})">✎</button>
        <button class="btn btn-danger btn-sm" onclick="deleteAccount(${a.id})">✕</button>
      </td>
    </tr>
  `,
    )
    .join("");
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
  document.getElementById("acct-smtp-port").value = "587";
  document.getElementById("acct-smtp-result").textContent = "";
  document.getElementById("acct-imap-result").textContent = "";
  openModal("modal-account");
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
  document.getElementById("acct-smtp-pass").value = a.smtp_pass || "";
  document.getElementById("acct-imap-host").value = a.imap_host || "";
  document.getElementById("acct-imap-user").value = a.imap_user || "";
  document.getElementById("acct-imap-pass").value = a.imap_pass || "";
  document.getElementById("acct-smtp-result").textContent = "";
  document.getElementById("acct-imap-result").textContent = "";
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

async function testAccountSMTP() {
  if (!_accountEditId) {
    toast("Save the account first", "err");
    return;
  }
  await testAccountSMTPById(_accountEditId);
}

async function testAccountSMTPById(id) {
  const el = document.getElementById("acct-smtp-result") || {
    textContent: "",
    style: {},
  };
  el.textContent = "Testing...";
  const res = await api(`/api/accounts/${id}/test-smtp`, "POST");
  el.textContent = res.message;
  el.style.color = res.ok ? "var(--green)" : "var(--red)";
  if (!res.ok) toast(res.message, "err");
}

async function testAccountIMAP() {
  if (!_accountEditId) {
    toast("Save the account first", "err");
    return;
  }
  const el = document.getElementById("acct-imap-result");
  el.textContent = "Testing...";
  const res = await api(`/api/accounts/${_accountEditId}/test-imap`, "POST");
  el.textContent = res.message;
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
