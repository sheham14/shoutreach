let _scraperPoll = null;

async function loadScraper() {
  await pollScraperStatus();
  // Keep polling while the section is open so the worker indicator stays
  // honest even when no job is running -- otherwise you only learn the worker
  // is down by pressing Start and watching nothing happen.
  clearInterval(_scraperPoll);
  _scraperPoll = setInterval(pollScraperStatus, 3000);
}

async function startScraper() {
  const niche      = document.getElementById('sc-niche').value.trim();
  const city       = document.getElementById('sc-city').value.trim();
  const maxResults = +document.getElementById('sc-max').value;
  const autoImport = document.getElementById('sc-autoimport').checked;
  if (!niche || !city) { toast('Enter a niche and city', 'err'); return; }

  const res = await api('/api/scraper/start', 'POST',
    { niche, city, max_results: maxResults, auto_import: autoImport });
  if (!res.ok) { toast(res.error || 'Failed to queue the scrape', 'err'); return; }

  // Queued with no worker connected is a real outcome, not an error -- it will
  // run as soon as the worker starts. Say so rather than looking like nothing
  // happened.
  if (res.warning) toast(res.warning, 'err');
  else toast('Scrape queued — Chrome will open on your machine');

  document.getElementById('sc-log-feed').innerHTML = '';
  _setScraperUI('running');
  clearInterval(_scraperPoll);
  _scraperPoll = setInterval(pollScraperStatus, 2000);
}

async function stopScraper() {
  await api('/api/scraper/stop', 'POST');
  toast('Stop requested');
}

async function resumeScraper() {
  await api('/api/scraper/resume', 'POST');
  toast('Resuming');
}

function _renderWorkerBanner(d) {
  const dot    = document.getElementById('sc-worker-dot');
  const title  = document.getElementById('sc-worker-title');
  const detail = document.getElementById('sc-worker-detail');
  const help   = document.getElementById('sc-worker-help');
  const urlEl  = document.getElementById('sc-worker-url');
  if (!dot) return;

  if (urlEl) urlEl.textContent = window.location.origin;
  const urlElCmd = document.getElementById('sc-worker-url-cmd');
  if (urlElCmd) urlElCmd.textContent = window.location.origin;

  if (d.worker_online) {
    dot.style.background = 'var(--green)';
    title.textContent = 'Worker connected';
    const secs = d.worker_last_seen ?? 0;
    detail.textContent = `Running on your machine — last seen ${secs}s ago. `
      + 'Chrome will open there when a scrape starts.';
    help.style.display = 'none';
  } else {
    dot.style.background = 'var(--amber)';
    title.textContent = 'No worker connected';
    detail.textContent = d.worker_last_seen == null
      ? 'This server cannot open a browser. Start the worker on your own machine.'
      : `Last seen ${d.worker_last_seen}s ago. Start the worker to continue.`;
    help.style.display = 'block';
  }
}

async function pollScraperStatus() {
  const d = await api('/api/scraper/status');
  _renderWorkerBanner(d);

  const pct = d.total ? Math.round(d.progress / d.total * 100) : 0;
  document.getElementById('sc-progress-bar').style.width = pct + '%';
  document.getElementById('sc-progress-text').textContent =
    d.total ? `${d.progress} / ${d.total} businesses`
            : (d.status === 'idle' ? 'Idle' : (d.status || 'Idle'));
  document.getElementById('sc-found').textContent    = d.found    ?? '—';
  document.getElementById('sc-scraped').textContent  = d.progress ?? '—';
  document.getElementById('sc-imported').textContent = d.imported ?? '—';
  document.getElementById('sc-captcha-box').style.display =
    d.status === 'captcha' ? 'block' : 'none';

  // Heartbeat freshness -- the log line itself is usually proof enough the
  // job is alive, but a slow page load can go 10-20s between lines, and a
  // silent worker crash otherwise only surfaces after the 3-minute auto-fail.
  // This gives an earlier, calibrated "is it actually stuck" signal.
  const hbEl = document.getElementById('sc-heartbeat');
  const jobActive = d.status === 'running' || d.status === 'captcha';
  if (jobActive && d.heartbeat_secs != null) {
    const stale = d.heartbeat_secs > 30;
    hbEl.style.display = 'block';
    hbEl.style.color = stale ? 'var(--amber)' : 'var(--muted)';
    hbEl.textContent = stale
      ? `⚠ No update in ${d.heartbeat_secs}s — may be stuck (auto-fails after 3 min of silence)`
      : `Last update ${d.heartbeat_secs}s ago`;
  } else {
    hbEl.style.display = 'none';
  }

  if (d.error) {
    document.getElementById('sc-progress-text').textContent = d.error;
  }

  if (d.logs && d.logs.length) {
    const levelColor = { INFO: 'var(--text)', WARN: 'var(--amber)', ERROR: 'var(--red)' };
    document.getElementById('sc-log-feed').innerHTML = [...d.logs].reverse().map(l =>
      `<div style="padding:6px 14px;border-bottom:1px solid var(--border);color:${levelColor[l.level]||'var(--text)'}">${esc(l.msg)}</div>`
    ).join('');
  }

  _setScraperUI(d.status || 'idle', d.worker_online);

  // Drop back to the slower idle cadence once the job settles, but never stop
  // entirely -- the worker indicator has to keep updating.
  if (['done', 'stopped', 'error', 'idle'].includes(d.status)) {
    clearInterval(_scraperPoll);
    _scraperPoll = setInterval(pollScraperStatus, 3000);
  }
}

function _setScraperUI(status, workerOnline) {
  const running = status === 'running' || status === 'captcha'
                  || status === 'queued' || status === 'claimed';
  const startBtn = document.getElementById('sc-start-btn');
  startBtn.disabled = running || workerOnline === false;
  startBtn.title = (workerOnline === false && !running)
    ? 'Start the worker on your machine first'
    : '';
  document.getElementById('sc-stop-btn').style.display = running ? 'inline-flex' : 'none';
}
