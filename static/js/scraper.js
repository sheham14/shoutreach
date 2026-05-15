let _scraperPoll = null;

async function loadScraper() {
  await pollScraperStatus();
}

async function startScraper() {
  const niche      = document.getElementById('sc-niche').value.trim();
  const city       = document.getElementById('sc-city').value.trim();
  const maxResults = +document.getElementById('sc-max').value;
  const autoImport = document.getElementById('sc-autoimport').checked;
  if (!niche || !city) { toast('Enter a niche and city', 'err'); return; }
  const res = await api('/api/scraper/start', 'POST',
    { niche, city, max_results: maxResults, auto_import: autoImport });
  if (!res.ok) { toast(res.error || 'Failed to start scraper', 'err'); return; }
  document.getElementById('sc-log-feed').innerHTML = '';
  _setScraperUI('running');
  clearInterval(_scraperPoll);
  _scraperPoll = setInterval(pollScraperStatus, 2000);
}

async function stopScraper() {
  await api('/api/scraper/stop', 'POST');
}

async function resumeScraper() {
  await api('/api/scraper/resume', 'POST');
}

async function pollScraperStatus() {
  const d = await api('/api/scraper/status');
  const pct = d.total ? Math.round(d.progress / d.total * 100) : 0;
  document.getElementById('sc-progress-bar').style.width = pct + '%';
  document.getElementById('sc-progress-text').textContent =
    d.total ? `${d.progress} / ${d.total} businesses` : (d.status === 'idle' ? 'Idle' : d.status);
  document.getElementById('sc-found').textContent    = d.found    ?? '—';
  document.getElementById('sc-scraped').textContent  = d.progress ?? '—';
  document.getElementById('sc-imported').textContent = d.imported ?? '—';
  document.getElementById('sc-captcha-box').style.display =
    d.status === 'captcha' ? 'block' : 'none';

  if (d.logs && d.logs.length) {
    const levelColor = { INFO: 'var(--text)', WARN: 'var(--amber)', ERROR: 'var(--red)' };
    document.getElementById('sc-log-feed').innerHTML = [...d.logs].reverse().map(l =>
      `<div style="padding:6px 14px;border-bottom:1px solid var(--border);color:${levelColor[l.level]||'var(--text)'}">${esc(l.msg)}</div>`
    ).join('');
  }

  _setScraperUI(d.status || 'idle');
  if (['done','stopped','error','idle'].includes(d.status)) {
    clearInterval(_scraperPoll);
  }
}

function _setScraperUI(status) {
  const running = status === 'running' || status === 'captcha';
  document.getElementById('sc-start-btn').disabled = running;
  document.getElementById('sc-stop-btn').style.display = running ? 'inline-flex' : 'none';
}
