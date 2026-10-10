let _scraperPoll = null;
let _lastHeldStatus = null;

async function loadScraper() {
  // Arriving from WhatsApp's "Scrape Google Maps" button: aim the form at
  // WhatsApp so what this finds lands there. One-shot, so a later visit from
  // the sidebar opens on whatever was last picked rather than being re-forced.
  await loadCountries();
  const wrap = document.getElementById('sc-country-wrap');
  if (!document.getElementById('sc-country')) wrap.innerHTML = countryPickerHtml('sc-country', _countries.used[0] || 'AE');
  let presetCampaign = '';
  if (window._scraperPreset) {
    const { destination, country, campaign_id } = window._scraperPreset;
    window._scraperPreset = null;
    document.getElementById('sc-destination').value = destination;
    if (country) setCountryPicker('sc-country', country);
    presetCampaign = campaign_id ? String(campaign_id) : '';
  }
  await scraperDestinationChanged(presetCampaign);
  loadScrapeLists();
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
  const destination = document.getElementById('sc-destination').value;
  const country     = destination === 'whatsapp' ? countryValue('sc-country') : '';
  const campaignId  = ['email', 'csv'].includes(destination) ? '' : document.getElementById('sc-campaign').value;
  if (!niche || !city) { toast('Enter a niche and city', 'err'); return; }
  if (destination === 'whatsapp' && !country) { toast('Pick the country from the list', 'err'); return; }
  if (destination === 'whatsapp' && !campaignId) {
    toast('Pick the WhatsApp campaign these leads go into', 'err');
    return;
  }

  const res = await api('/api/scraper/start', 'POST',
    { niche, city, max_results: maxResults, auto_import: autoImport, destination, country,
      campaign_id: campaignId || null });
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

const _SCRAPER_DESTINATIONS = {
  email: {
    label: 'Email',
    hint: "Visits each business's website looking for an email address. Leads go to Email; "
        + "businesses with no email found stay in Contacts under Unassigned.",
    importLabel: "Import leads as they're found",
  },
  calling: {
    label: 'Calling',
    hint: "Uses the phone number from Google Maps and skips the websites, so it's much faster. "
        + "Leads go to Calling and nowhere else.",
    importLabel: "Import leads into Calling as they're found",
    campaigns: '/api/call-campaigns',
    campaignHint: 'Optional. Leads always land on your Calling list; this also puts them in a campaign.',
    campaignOptional: true,
  },
  whatsapp: {
    label: 'WhatsApp',
    hint: "Uses the phone number from Google Maps and skips the websites, so it's much faster. "
        + "Leads go to WhatsApp and nowhere else.",
    importLabel: "Import leads into WhatsApp as they're found",
    campaigns: '/api/wa/campaigns',
    campaignHint: "Their messages are written from this campaign's templates, ready to send.",
    campaignOptional: false,
  },
  csv: {
    label: 'CSV download',
    hint: "Visits each website for an email address, like an Email scrape, but puts the leads on no list. "
        + "They stay with the scrape for you to download as a CSV — from the Progress box while it runs, "
        + "or from Your scrapes & imports below. Import the file later from any channel.",
    noImportToggle: true,
  },
};

async function scraperDestinationChanged(selectCampaign = '') {
  const destination = document.getElementById('sc-destination').value;
  const spec = _SCRAPER_DESTINATIONS[destination] || _SCRAPER_DESTINATIONS.email;
  document.getElementById('sc-destination-hint').textContent = spec.hint;
  document.getElementById('sc-autoimport-label').textContent = spec.importLabel || '';
  document.getElementById('sc-autoimport-row').style.display = spec.noImportToggle ? 'none' : 'flex';
  document.getElementById('sc-country-group').style.display =
    destination === 'whatsapp' ? 'block' : 'none';

  const group = document.getElementById('sc-campaign-group');
  if (!spec.campaigns) { group.style.display = 'none'; return; }
  group.style.display = 'block';
  document.getElementById('sc-campaign-label').textContent = `${spec.label} campaign`;
  document.getElementById('sc-campaign-hint').textContent = spec.campaignHint;
  const sel = document.getElementById('sc-campaign');
  const campaigns = (await api(spec.campaigns)) || [];
  const list = Array.isArray(campaigns) ? campaigns : [];
  sel.innerHTML =
    (spec.campaignOptional ? '<option value="">No campaign</option>'
                           : (list.length ? '' : '<option value="">Create a campaign in WhatsApp first</option>')) +
    list.filter(c => c.status !== 'archived').map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
  if (selectCampaign) sel.value = selectCampaign;
  if (destination === 'whatsapp') {
    sel.onchange = () => {
      const c = list.find(x => String(x.id) === sel.value);
      if (c && c.country) setCountryPicker('sc-country', c.country);
    };
    sel.onchange();
  } else {
    sel.onchange = null;
  }
}

// ── Your scrapes & imports ───────────────────────────────────────────────────

const _SCRAPE_FOR = { email: 'Email', calling: 'Calling', whatsapp: 'WhatsApp', csv: 'CSV only' };

async function loadScrapeLists() {
  const tbody = document.getElementById('sc-lists');
  if (!tbody) return;
  const [lists, held] = await Promise.all([api('/api/contacts/sources'), api('/api/scraper/held')]);
  // A CSV-only scrape files no leads, so it's in no import list -- it comes
  // from its own endpoint and sorts in by when it ran.
  const rows = [
    ...(Array.isArray(held) ? held : []).map(h => ({ ...h, kind: 'held' })),
    ...(Array.isArray(lists) ? lists : []),
  ].sort((a, b) => (a.kind === 'manual') - (b.kind === 'manual')
                   || String(b.created_at || '').localeCompare(String(a.created_at || '')));
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="5"><div class="empty-state"><p>No scrapes or imports yet.</p></div></td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(l => {
    const manual = l.kind === 'manual';
    const id = String(l.id);
    let title, forPill, actions;
    if (l.kind === 'held') {
      title = [l.niche, l.city].filter(Boolean).join(' — ') || `Scrape #${l.id}`;
      forPill = pill('CSV only', 'purple');
      actions = `<button class="btn btn-primary btn-sm" ${l.count ? '' : 'disabled'}
          onclick="downloadUrl('/api/scraper/jobs/${l.id}/csv')">⬇ Download CSV</button>`;
    } else {
      title = manual ? l.base : (l.name || l.base);
      forPill = manual ? '<span class="text-muted">—</span>'
        : `${pill(_SCRAPE_FOR[l.destination] || (l.destination ? l.destination : 'Contacts'))}${
            l.kind === 'import' ? ` <span class="text-muted text-small">import</span>` : ''}`;
      actions = `
        ${manual ? '' : `<button class="btn btn-ghost btn-sm" onclick="renameImport('${escj(id)}', '${escj(l.name || '')}',
            '${escj(`${l.base} · ${localDateTime(l.created_at)}`)}', loadScrapeLists)">Rename</button>`}
        <button class="btn btn-ghost btn-sm" onclick="openScrapeInContacts('${escj(id)}')">View</button>
        <button class="btn btn-primary btn-sm" onclick="addListToChannel('${escj(id)}', '${escj(title)}', ${l.count})">Add all to…</button>`;
    }
    const counted = l.kind === 'import' && l.new_count < l.count
      ? `${l.count} <span class="text-muted">(${l.new_count} new)</span>` : `${l.count}`;
    const when = l.created_at ? esc(localDateTime(l.created_at)) : '—';
    return `<tr>
      <td><span class="biz-name">${esc(title)}</span>${l.country ? `<span class="sub">${esc(countryName(l.country))}</span>` : ''}</td>
      <td>${forPill}</td>
      <td class="num">${counted}</td>
      <td class="num">${when}</td>
      <td class="nowrap" style="text-align:right">${actions}</td>
      <td class="m-card">${mCard(`<span class="biz-name">${esc(title)}</span>`,
        [l.kind === 'held' ? 'CSV only' : (manual ? '' : `for ${_SCRAPE_FOR[l.destination] || 'Contacts'}`),
         `${l.count} leads`, l.created_at ? when : ''].filter(Boolean).join(' · '),
        `<span class="m-actions">${actions}</span>`)}</td>
    </tr>`;
  }).join('');
}

function openScrapeInContacts(jobId) {
  window._contactsPresetSource = jobId;
  showSection('contacts');
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
    d.total ? `${d.progress} / ${d.total} businesses → ${(_SCRAPER_DESTINATIONS[d.destination] || _SCRAPER_DESTINATIONS.email).label}`
            : (d.status === 'idle' ? 'Idle' : (d.status || 'Idle'));
  document.getElementById('sc-found').textContent    = d.found    ?? '—';
  document.getElementById('sc-scraped').textContent  = d.progress ?? '—';
  const held = d.destination === 'csv';
  document.getElementById('sc-imported-label').textContent = held ? 'Ready to download' : 'Imported';
  document.getElementById('sc-imported').textContent = held ? (d.held ?? 0) : (d.imported ?? '—');
  const dl = document.getElementById('sc-download-box');
  dl.style.display = held && d.held ? 'block' : 'none';
  if (held && d.held) {
    document.getElementById('sc-download-btn').onclick = () => downloadUrl(`/api/scraper/jobs/${d.job_id}/csv`);
  }
  // A finished CSV scrape belongs in the list below straight away.
  if (held && ['done', 'stopped', 'error'].includes(d.status) && _lastHeldStatus !== d.status) loadScrapeLists();
  _lastHeldStatus = held ? d.status : null;
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
