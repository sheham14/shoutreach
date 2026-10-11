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
  // Arriving with a preset for the importer (a channel's "Import a file…")
  // opens that tab; otherwise whichever tab was open last.
  const preset = window._getLeadsPreset;
  window._getLeadsPreset = null;
  setTab('getleads', preset ? 'import' : currentTab('getleads', 'scrape'));
  if (preset) await _applyGetLeadsPreset(preset);
  _refreshImportCount();
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

// ── Tabs ─────────────────────────────────────────────────────────────────────

onTab('getleads', name => {
  if (name === 'history') loadScrapeLists();
  if (name === 'import') getLeadsDestChanged();
});

// From anywhere: open Get leads on the importer, aimed at a channel (and a
// campaign and country, for WhatsApp). What each channel's "+ Add leads"
// sends you to for anything new.
function openGetLeads(preset = {}) {
  window._getLeadsPreset = { dest: 'contacts', ...preset };
  if (typeof closeModal === 'function') {
    ['modal-import-wa', 'modal-add-call-leads'].forEach(id => {
      if (document.getElementById(id)?.classList.contains('open')) closeModal(id);
    });
  }
  showSection('scraper');
}

async function _applyGetLeadsPreset(p) {
  const dest = document.getElementById('gl-dest');
  dest.value = p.dest || 'contacts';
  await getLeadsDestChanged(p.campaign || '', p.country || '');
}

async function _refreshImportCount() {
  const [lists, held] = await Promise.all([api('/api/contacts/sources'), api('/api/scraper/held')]);
  const n = (Array.isArray(lists) ? lists.filter(l => l.kind !== 'manual').length : 0)
          + (Array.isArray(held) ? held.length : 0);
  setTabCount('getleads', 'history', n);
}

// ── Import a file or paste ───────────────────────────────────────────────────

const _GL_DEST = {
  contacts: { hint: 'Every business lands in Contacts. Rows with an email address are also listed under Email '
                  + '— nothing is sent until you put them in a campaign.' },
  calling:  { hint: 'They go on your Calling list, ready to dial.',
              campaigns: '/api/call-campaigns', optional: true,
              campaignHint: 'Optional. They always land on Calling; this also puts them in a campaign.' },
  whatsapp: { hint: 'They land in Ready to send, each with its message written — from the file if it brings one, '
                  + 'else from the campaign.',
              campaigns: '/api/wa/campaigns', optional: false,
              campaignHint: "Messages are written from this campaign's templates unless a row brings its own." },
};

async function getLeadsDestChanged(selectCampaign = '', country = '') {
  const dest = document.getElementById('gl-dest').value;
  const spec = _GL_DEST[dest] || _GL_DEST.contacts;
  document.getElementById('gl-dest-hint').textContent = spec.hint;
  const group = document.getElementById('gl-campaign-group');
  document.getElementById('gl-country-group').style.display = dest === 'whatsapp' ? 'block' : 'none';
  if (dest === 'whatsapp') {
    await loadCountries();
    const wrap = document.getElementById('gl-country-wrap');
    if (!document.getElementById('gl-country')) {
      wrap.innerHTML = countryPickerHtml('gl-country', country || _countries.used[0] || 'AE');
    } else if (country) {
      setCountryPicker('gl-country', country);
    }
  }
  if (!spec.campaigns) { group.style.display = 'none'; return; }
  group.style.display = 'block';
  document.getElementById('gl-campaign-label').textContent =
    `${dest === 'whatsapp' ? 'WhatsApp' : 'Calling'} campaign`;
  document.getElementById('gl-campaign-hint').textContent = spec.campaignHint;
  const got = await api(spec.campaigns);
  const list = (Array.isArray(got) ? got : []).filter(c => c.status !== 'archived');
  const keep = selectCampaign || document.getElementById('gl-campaign')?.value || '';
  document.getElementById('gl-campaign-wrap').innerHTML = campaignSelectHtml('gl-campaign', list, {
    allowNone: spec.optional, noneLabel: 'No campaign — just add them to Calling',
    selected: keep || (!spec.optional && list[0] ? list[0].id : ''),
  });
  const sel = document.getElementById('gl-campaign');
  if (!spec.optional && !list.length) { sel.value = '__new'; sel.dispatchEvent(new Event('change')); }
  if (dest === 'whatsapp') {
    sel.addEventListener('change', () => {
      const c = list.find(x => String(x.id) === sel.value);
      if (c && c.country) setCountryPicker('gl-country', c.country);
    });
  }
}

// One CSV line, quotes and all ("Smith, Jones & Co" stays one cell).
function _csvCells(line) {
  const out = [];
  let cur = '', q = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (q) {
      if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++; }
      else if (ch === '"') q = false;
      else cur += ch;
    } else if (ch === '"') q = true;
    else if (ch === ',') { out.push(cur); cur = ''; }
    else cur += ch;
  }
  out.push(cur);
  return out.map(s => s.trim());
}

// Split CSV text into lines, keeping a quoted cell's line breaks inside it.
function _csvLines(text) {
  const lines = [];
  let cur = '', q = false;
  for (const ch of text.replace(/\r\n?/g, '\n')) {
    if (ch === '"') q = !q;
    if (ch === '\n' && !q) { lines.push(cur); cur = ''; } else cur += ch;
  }
  lines.push(cur);
  return lines.filter(l => l.trim());
}

const _GL_HEADERS = ['company', 'name', 'phone', 'email', 'website', 'address', 'city', 'country',
                     'category', 'rating', 'review_count', 'message', 'followup_1', 'first_name'];

function _jsonRows(text) {
  let parsed;
  try { parsed = JSON.parse(text); } catch (e) { toast(`That isn't valid JSON — ${e.message}`, 'err'); return null; }
  const rows = Array.isArray(parsed) ? parsed : parsed && parsed.rows;
  if (!Array.isArray(rows) || !rows.length) { toast('Expected an array of rows, or {"rows": [...]}', 'err'); return null; }
  if (rows.some(r => !r || typeof r !== 'object' || Array.isArray(r))) {
    toast('Every row must be an object with named keys like "company"', 'err');
    return null;
  }
  return rows;
}

// What was pasted, as rows: JSON; CSV with a header row; or one business per
// line ("name, phone, website, email"), where a line that's just an address
// is an email.
function _pastedRows(text) {
  const t = text.trim();
  if (t.startsWith('[') || t.startsWith('{')) {
    const rows = _jsonRows(t);
    return rows ? { rows, source: 'JSON' } : null;
  }
  const lines = _csvLines(t);
  const head = _csvCells(lines[0]).map(h => h.toLowerCase());
  if (head.some(h => _GL_HEADERS.includes(h))) {
    const rows = lines.slice(1).map(l => {
      const cells = _csvCells(l);
      return Object.fromEntries(head.map((h, i) => [h, cells[i] ?? '']).filter(([h]) => h));
    });
    return { rows, source: 'CSV' };
  }
  const rows = lines.map(l => {
    const cells = _csvCells(l);
    if (cells.length === 1 && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(cells[0])) return { email: cells[0] };
    const [company, phone, website, email] = cells;
    return { company: company || '', phone: phone || '', website: website || '', email: email || '' };
  }).filter(r => r.company || r.email || r.phone);
  return { rows, source: 'Paste' };
}

async function _postImport(url, rows, file, fields) {
  if (file) {
    const fd = new FormData();
    fd.append('file', file);
    Object.entries(fields).forEach(([k, v]) => { if (v !== '' && v != null) fd.append(k, v); });
    const r = await fetch(url, { method: 'POST', credentials: 'same-origin',
                                 headers: { 'X-CSRF-Token': await _getCsrfToken() }, body: fd });
    if (r.status === 401) { window.location.href = '/login'; return null; }
    const body = await r.json().catch(() => ({}));
    return r.ok ? body : { error: body.error || `Import failed (${r.status})` };
  }
  return api(url, 'POST', { rows, ...fields });
}

async function importLeads() {
  const dest = document.getElementById('gl-dest').value;
  const file = document.getElementById('gl-file').files[0];
  const paste = document.getElementById('gl-paste').value.trim();
  if (!file && !paste) { toast('Choose a file or paste something', 'err'); return; }

  let country = '', campaignId = '';
  if (dest === 'whatsapp') {
    country = countryValue('gl-country');
    if (!country) { toast('Pick the country from the list', 'err'); return; }
    campaignId = await resolveCampaignSelect('gl-campaign', '/api/wa/campaigns', { country });
    if (!campaignId) { if (campaignId === '') toast('Pick the campaign these leads go into', 'err'); return; }
  } else if (dest === 'calling') {
    campaignId = await resolveCampaignSelect('gl-campaign', '/api/call-campaigns');
    if (campaignId === null) return;
  }

  // A CSV file goes up as it is; a JSON file or a paste is read here first,
  // so a malformed one is a message about the paste, not a 400.
  let rows = null, upload = null, source;
  if (file) {
    source = file.name;
    if (/\.json$/i.test(file.name)) { rows = _jsonRows(await file.text()); if (!rows) return; }
    else upload = file;
  } else {
    const parsed = _pastedRows(paste);
    if (!parsed) return;
    if (!parsed.rows.length) { toast('Nothing to import — check the paste', 'err'); return; }
    ({ rows, source } = parsed);
  }

  const btn = document.getElementById('gl-submit');
  const status = document.getElementById('gl-status');
  btn.disabled = true;
  status.textContent = 'Importing…';
  try {
    const res = dest === 'whatsapp'
      ? await _importToWhatsApp(rows, upload, source, country, campaignId)
      : await _importToContacts(rows, upload, source, dest, campaignId);
    if (!res) return;
    _showImportResult(res);
    document.getElementById('gl-file').value = '';
    document.getElementById('gl-paste').value = '';
    _refreshImportCount();
    if (typeof loadCountries === 'function' && dest === 'whatsapp') loadCountries(true);
  } finally {
    btn.disabled = false;
    status.textContent = '';
  }
}

async function _importToWhatsApp(rows, upload, source, country, campaignId) {
  const first = await _postImport('/api/wa/import', rows, upload,
                                  { country, wa_campaign_id: campaignId, source });
  if (!first || first.error) { toast((first && first.error) || 'Import failed', 'err'); return null; }
  const final = await confirmChannelConflicts(first, () =>
    api('/api/wa/import', 'POST', {
      rows: first.conflicts.map(c => c.row), country, wa_campaign_id: campaignId, confirm_conflicts: true,
      batch_id: first.batch_id || null, source,
    }));
  notifyCrossOwnerOverlap(first);
  const inserted = (first.inserted || 0) + (final !== first ? (final.inserted || 0) : 0);
  return {
    text: `${inserted} lead${inserted === 1 ? '' : 's'} on WhatsApp, ready to send`
          + describeWaDrafts(_mergeWaDrafts(first, final)),
    batch: first.batch_id || (final && final.batch_id),
  };
}

async function _importToContacts(rows, upload, source, dest, campaignId) {
  const channel = dest === 'calling' ? 'calling' : 'email';
  const first = await _postImport('/api/contacts/import', rows, upload, { source, channel });
  if (!first || first.error) { toast((first && first.error) || 'Import failed', 'err'); return null; }
  const final = await confirmChannelConflicts(first, () =>
    api('/api/contacts/import', 'POST', {
      rows: first.conflicts.map(c => c.row), confirm_conflicts: true,
      batch_id: first.batch_id || null, source, channel,
    }));
  notifyCrossOwnerOverlap(first);
  const ids = [...(first.business_ids || []), ...(final !== first ? (final.business_ids || []) : [])];
  const batch = first.batch_id || (final && final.batch_id);
  const inv = (first.invalid_mx || 0) + (final !== first ? (final.invalid_mx || 0) : 0);
  const invText = inv ? ` · ${inv} email address${inv === 1 ? '' : 'es'} on a domain that can't receive mail` : '';
  if (dest === 'calling') {
    const added = await _addToCalling(ids, campaignId);
    if (!added || added.error) { toast((added && added.error) || 'Imported, but not added to Calling', 'err'); return null; }
    return { text: describeAdd(added, 'Added to Calling:') + invText, batch };
  }
  return { text: `${ids.length} business${ids.length === 1 ? '' : 'es'} imported${invText}`, batch };
}

function _showImportResult(res) {
  const box = document.getElementById('gl-result');
  box.style.display = 'block';
  box.innerHTML = `<b>Done.</b> ${esc(res.text)}
    ${res.batch ? ` · <a style="color:var(--blue);cursor:pointer" onclick="openScrapeInContacts('${res.batch}')">See them in Contacts →</a>
      · <a style="color:var(--blue);cursor:pointer" onclick="setTab('getleads','history')">Your imports</a>` : ''}`;
  toast('Imported ✓');
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
