async function loadSettings() {
  const s = await api('/api/settings');
  document.getElementById('cfg-smtp-host').value = s.smtp_host || '';
  document.getElementById('cfg-smtp-port').value = s.smtp_port || '587';
  document.getElementById('cfg-smtp-user').value = s.smtp_user || '';
  document.getElementById('cfg-smtp-pass').value = s.smtp_pass || '';
  document.getElementById('cfg-from-name').value = s.smtp_from_name || '';
  document.getElementById('cfg-from-email').value = s.smtp_from_email || '';
  document.getElementById('cfg-imap-host').value = s.imap_host || '';
  document.getElementById('cfg-imap-user').value = s.imap_user || '';
  document.getElementById('cfg-imap-pass').value = s.imap_pass || '';
  document.getElementById('cfg-global-cap').value = s.global_daily_cap || '200';
  document.getElementById('cfg-base-url').value = s.app_base_url || 'http://localhost:5000';
}

async function saveSettings() {
  await api('/api/settings', 'POST', {
    smtp_host:       document.getElementById('cfg-smtp-host').value,
    smtp_port:       document.getElementById('cfg-smtp-port').value,
    smtp_user:       document.getElementById('cfg-smtp-user').value,
    smtp_pass:       document.getElementById('cfg-smtp-pass').value,
    smtp_from_name:  document.getElementById('cfg-from-name').value,
    smtp_from_email: document.getElementById('cfg-from-email').value,
    imap_host:       document.getElementById('cfg-imap-host').value,
    imap_user:       document.getElementById('cfg-imap-user').value,
    imap_pass:       document.getElementById('cfg-imap-pass').value,
    global_daily_cap: document.getElementById('cfg-global-cap').value,
    app_base_url:    document.getElementById('cfg-base-url').value,
  });
  toast('Settings saved ✓');
}

async function testSMTP() {
  const el = document.getElementById('smtp-test-result');
  el.textContent = 'Testing...';
  el.style.color = 'var(--muted)';
  const res = await api('/api/settings/test-smtp', 'POST');
  el.textContent = res.message;
  el.style.color = res.ok ? 'var(--green)' : 'var(--red)';
}

async function testIMAP() {
  const el = document.getElementById('imap-test-result');
  el.textContent = 'Testing...';
  el.style.color = 'var(--muted)';
  const res = await api('/api/settings/test-imap', 'POST');
  el.textContent = res.message;
  el.style.color = res.ok ? 'var(--green)' : 'var(--red)';
}
