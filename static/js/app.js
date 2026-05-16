let currentCampaignId = null;
let allContacts = [];

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
});

function toggleFaq(header) {
  header.closest('.faq-item').classList.toggle('open');
}
