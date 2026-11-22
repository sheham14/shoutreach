let currentCampaignId = null;

document.querySelectorAll('nav a').forEach(a => {
  a.addEventListener('click', e => {
    e.preventDefault();
    showSection(a.dataset.section);
  });
});

refreshDashboard();

// Load current user info (sidebar username)
api('/api/users/me').then(me => {
  const el = document.getElementById('sidebar-username');
  if (el && me.username) el.textContent = me.username;
  // "Reword with AI" is only offered while AI is switched on in Settings.
  window._aiEnabled = !!me.ai_enabled;
  // Running the sender by hand is admin-only on the server (audit F-25).
  const runNow = document.getElementById('run-now-btn');
  if (runNow) runNow.hidden = !me.is_admin;
});

function toggleFaq(header) {
  header.closest('.faq-item').classList.toggle('open');
}
