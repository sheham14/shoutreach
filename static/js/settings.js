async function loadSettings() {
  const s = await api('/api/settings');
  document.getElementById('cfg-global-cap').value = s.global_daily_cap || '200';
  document.getElementById('cfg-base-url').value = s.app_base_url || 'http://localhost:5000';
  document.getElementById('cfg-include-unsub').checked = (s.include_unsubscribe ?? '1') === '1';
  document.getElementById('cfg-company-address').value = s.company_address || '';
}

async function saveSettings() {
  await api('/api/settings', 'POST', {
    global_daily_cap:    document.getElementById('cfg-global-cap').value,
    app_base_url:        document.getElementById('cfg-base-url').value,
    include_unsubscribe: document.getElementById('cfg-include-unsub').checked ? '1' : '0',
    company_address:     document.getElementById('cfg-company-address').value.trim(),
  });
  toast('Settings saved ✓');
}

// ── Email Accounts ────────────────────────────────────────────────────────────

let _accountEditId = null;

async function loadAccounts() {
  const accounts = await api('/api/accounts');
  const tbody = document.getElementById('accounts-table');
  if (!accounts.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state"><p>No accounts configured.</p></td></tr>';
    return;
  }
  tbody.innerHTML = accounts.map(a => `
    <tr>
      <td>${esc(a.name)}</td>
      <td class="mono" style="font-size:12px">${esc(a.email)}</td>
      <td class="mono text-muted" style="font-size:11px">${esc(a.smtp_host)}</td>
      <td>${a.status === 'active' ? '<span class="badge badge-green">active</span>' : '<span class="badge badge-gray">paused</span>'}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-ghost btn-sm" onclick="testAccountSMTPById(${a.id})">Test</button>
        <button class="btn btn-ghost btn-sm" onclick="openEditAccountModal(${a.id})">✎</button>
        <button class="btn btn-danger btn-sm" onclick="deleteAccount(${a.id})">✕</button>
      </td>
    </tr>
  `).join('');
}

function openAddAccountModal() {
  _accountEditId = null;
  document.getElementById('account-modal-title').textContent = 'Add Email Account';
  ['acct-name','acct-email','acct-from-name','acct-smtp-host','acct-smtp-user','acct-smtp-pass',
   'acct-imap-host','acct-imap-user','acct-imap-pass'].forEach(id => {
    document.getElementById(id).value = '';
  });
  document.getElementById('acct-smtp-port').value = '587';
  document.getElementById('acct-smtp-result').textContent = '';
  document.getElementById('acct-imap-result').textContent = '';
  openModal('modal-account');
}

async function openEditAccountModal(id) {
  const accounts = await api('/api/accounts');
  const a = accounts.find(x => x.id === id);
  if (!a) return;
  _accountEditId = id;
  document.getElementById('account-modal-title').textContent = 'Edit Account';
  document.getElementById('acct-name').value      = a.name      || '';
  document.getElementById('acct-email').value     = a.email     || '';
  document.getElementById('acct-from-name').value = a.from_name || '';
  document.getElementById('acct-smtp-host').value = a.smtp_host || '';
  document.getElementById('acct-smtp-port').value = a.smtp_port || 587;
  document.getElementById('acct-smtp-user').value = a.smtp_user || '';
  document.getElementById('acct-smtp-pass').value = a.smtp_pass || '';
  document.getElementById('acct-imap-host').value = a.imap_host || '';
  document.getElementById('acct-imap-user').value = a.imap_user || '';
  document.getElementById('acct-imap-pass').value = a.imap_pass || '';
  document.getElementById('acct-smtp-result').textContent = '';
  document.getElementById('acct-imap-result').textContent = '';
  openModal('modal-account');
}

function _accountPayload() {
  return {
    name:      document.getElementById('acct-name').value.trim(),
    email:     document.getElementById('acct-email').value.trim(),
    from_name: document.getElementById('acct-from-name').value.trim(),
    smtp_host: document.getElementById('acct-smtp-host').value.trim(),
    smtp_port: +document.getElementById('acct-smtp-port').value,
    smtp_user: document.getElementById('acct-smtp-user').value.trim(),
    smtp_pass: document.getElementById('acct-smtp-pass').value,
    imap_host: document.getElementById('acct-imap-host').value.trim(),
    imap_user: document.getElementById('acct-imap-user').value.trim(),
    imap_pass: document.getElementById('acct-imap-pass').value,
  };
}

async function saveAccount() {
  const payload = _accountPayload();
  if (!payload.name || !payload.email) { toast('Name and email are required', 'err'); return; }
  if (_accountEditId) {
    await api(`/api/accounts/${_accountEditId}`, 'PUT', payload);
    toast('Account updated ✓');
  } else {
    await api('/api/accounts', 'POST', payload);
    toast('Account added ✓');
  }
  closeModal('modal-account');
  loadAccounts();
}

async function deleteAccount(id) {
  if (!confirm('Delete this account? It will be removed from all campaigns.')) return;
  await api(`/api/accounts/${id}`, 'DELETE');
  toast('Account deleted');
  loadAccounts();
}

async function testAccountSMTP() {
  if (!_accountEditId) { toast('Save the account first', 'err'); return; }
  await testAccountSMTPById(_accountEditId);
}

async function testAccountSMTPById(id) {
  const el = document.getElementById('acct-smtp-result') || { textContent: '', style: {} };
  el.textContent = 'Testing...';
  const res = await api(`/api/accounts/${id}/test-smtp`, 'POST');
  el.textContent = res.message;
  el.style.color = res.ok ? 'var(--green)' : 'var(--red)';
  if (!res.ok) toast(res.message, 'err');
}

async function testAccountIMAP() {
  if (!_accountEditId) { toast('Save the account first', 'err'); return; }
  const el = document.getElementById('acct-imap-result');
  el.textContent = 'Testing...';
  const res = await api(`/api/accounts/${_accountEditId}/test-imap`, 'POST');
  el.textContent = res.message;
  el.style.color = res.ok ? 'var(--green)' : 'var(--red)';
}
