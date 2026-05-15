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
