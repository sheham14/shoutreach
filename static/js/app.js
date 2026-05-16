let currentCampaignId = null;
let allContacts = [];

document.querySelectorAll('nav a').forEach(a => {
  a.addEventListener('click', e => {
    e.preventDefault();
    showSection(a.dataset.section);
  });
});

refreshDashboard();

function toggleFaq(header) {
  header.closest('.faq-item').classList.toggle('open');
}
