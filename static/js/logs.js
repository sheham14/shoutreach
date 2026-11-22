async function loadLogs() {
  const logs = await api('/api/logs');
  const el = document.getElementById('log-feed');
  if (!logs.length) {
    el.innerHTML = '<div class="empty-state"><p>No activity yet</p></div>';
    return;
  }
  el.innerHTML = logs.map(l => `
    <div class="log-entry log-${l.level}">
      <span class="log-time">${esc(fmtWhen(l.created_at, 'utc'))}</span>
      <span class="log-msg">${esc(l.message)}</span>
    </div>
  `).join('');
}

async function clearAllLogs() {
  if (!await confirmDialog({
    title: 'Clear the whole activity log?',
    text: 'Every entry is deleted for good. It does not touch contacts, campaigns or sends, only this history.',
    confirm: 'Clear all logs', danger: true,
  })) return;
  const res = await api('/api/logs', 'DELETE');
  if (!res || res.error) { toast((res && res.error) || 'Could not clear logs', 'err'); return; }
  toast(`Cleared ${res.deleted} log entr${res.deleted === 1 ? 'y' : 'ies'}`);
  loadLogs();
}
