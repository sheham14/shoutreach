async function loadLogs() {
  const logs = await api('/api/logs');
  const el = document.getElementById('log-feed');
  if (!logs.length) {
    el.innerHTML = '<div class="empty-state"><p>No activity yet</p></div>';
    return;
  }
  el.innerHTML = logs.map(l => `
    <div class="log-entry log-${l.level}">
      <span class="log-time">${(l.created_at||'').replace('T',' ').substring(0,19)}</span>
      <span class="log-msg">${esc(l.message)}</span>
    </div>
  `).join('');
}

async function clearAllLogs() {
  if (!confirm('Permanently delete every activity log entry? This cannot be undone — it does not touch contacts, campaigns, or sends, only this history.')) return;
  const res = await api('/api/logs', 'DELETE');
  if (!res || res.error) { toast((res && res.error) || 'Could not clear logs', 'err'); return; }
  toast(`Cleared ${res.deleted} log entr${res.deleted === 1 ? 'y' : 'ies'}`);
  loadLogs();
}
