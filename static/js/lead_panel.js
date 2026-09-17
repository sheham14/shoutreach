// ── The lead side panel ──────────────────────────────────────────────────────
//
// One panel for one business, used on Contacts and on every channel's Leads
// tab, so clicking a lead never sends you to another page. It shows that
// channel's own controls first, then what's the same everywhere: where else
// the business is, notes, the audit, and its history.

// ── Countries ────────────────────────────────────────────────────────────────

let _countries = null;          // {list: [{code, dial, name}], used: [codes]}
let _countryNames = null;

function countryName(code) {
  if (!code) return '';
  try {
    _countryNames = _countryNames || new Intl.DisplayNames(['en'], { type: 'region' });
    return _countryNames.of(code) || code;
  } catch (_) { return code; }
}

async function loadCountries(force = false) {
  if (_countries && !force) return _countries;
  const data = await api('/api/countries');
  const list = ((data && data.countries) || []).map(c => ({ ...c, name: countryName(c.code) }))
    .sort((a, b) => a.name.localeCompare(b.name));
  _countries = { list, used: (data && data.used) || [] };
  _ensureCountryDatalist();
  return _countries;
}

function _countryLabel(c) { return `${c.name} (+${c.dial})`; }

// One shared <datalist>, the countries you've used first. Typing filters it:
// "sau" finds Saudi Arabia, "+966" finds it too.
function _ensureCountryDatalist() {
  let dl = document.getElementById('country-datalist');
  if (!dl) {
    dl = document.createElement('datalist');
    dl.id = 'country-datalist';
    document.body.appendChild(dl);
  }
  const used = _countries.used.map(code => _countries.list.find(c => c.code === code)).filter(Boolean);
  const rest = _countries.list.filter(c => !_countries.used.includes(c.code));
  dl.innerHTML = [...used, ...rest].map(c => `<option value="${esc(_countryLabel(c))}"></option>`).join('');
}

function countryPickerHtml(id, selected = '') {
  const c = _countries && _countries.list.find(x => x.code === (selected || '').toUpperCase());
  return `<input id="${id}" class="soft-input" list="country-datalist" autocomplete="off"
            placeholder="Type a country…" value="${esc(c ? _countryLabel(c) : '')}"
            onfocus="this.select()" />`;
}

// The country code for whatever is typed in a picker, or '' if it matches none.
function countryValue(id) {
  const raw = (document.getElementById(id)?.value || '').trim().toLowerCase();
  if (!raw || !_countries) return '';
  const hit = _countries.list.find(c => _countryLabel(c).toLowerCase() === raw)
    || _countries.list.find(c => c.code.toLowerCase() === raw || c.name.toLowerCase() === raw)
    || _countries.list.find(c => c.name.toLowerCase().startsWith(raw));
  return hit ? hit.code : '';
}

function setCountryPicker(id, code) {
  const el = document.getElementById(id);
  const c = _countries && _countries.list.find(x => x.code === (code || '').toUpperCase());
  if (el && c) el.value = _countryLabel(c);
}

// ── Opening WhatsApp ─────────────────────────────────────────────────────────
//
// Always the operator's own tap on Send, inside WhatsApp. On a phone this
// opens the WhatsApp app; on a computer it opens WhatsApp Web in one tab that
// is reused for every lead, instead of a new tab and a "continue to chat"
// page each time. Someone using the desktop app can switch to that.

function isPhone() { return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent); }

function waOpensIn() {
  if (isPhone()) return 'app';
  try { return localStorage.getItem('wa-open') || 'web'; } catch (_) { return 'web'; }
}

function setWaOpensIn(where) {
  try { localStorage.setItem('wa-open', where); } catch (_) { /* private mode */ }
  toast(where === 'app' ? 'Will open the WhatsApp app' : 'Will open WhatsApp Web, in one tab');
}

function openWhatsAppChat(number, message) {
  const text = encodeURIComponent(message || '');
  if (waOpensIn() === 'app') {
    window.location.href = `whatsapp://send?phone=${number}&text=${text}`;
    return;
  }
  const tab = window.open(`https://web.whatsapp.com/send?phone=${number}&text=${text}`, 'shoutreach-whatsapp');
  // A reused tab isn't always brought to the front on its own, which made it
  // look as if nothing happened.
  if (tab) { try { tab.focus(); } catch (_) { /* cross-origin in some browsers */ } }
}

// Now, in the database's own format (UTC), for a lead just opened.
function utcNow() { return new Date().toISOString().replace('T', ' ').substring(0, 19); }

// A database time (UTC) as the operator reads it: "at 15:42" today, else "on 16 Sep at 15:42".
function whenLocal(ts) {
  if (!ts) return '';
  const d = new Date(String(ts).replace(' ', 'T') + 'Z');
  if (isNaN(d)) return '';
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === new Date().toDateString()) return `at ${time}`;
  return `on ${d.toLocaleDateString([], { day: 'numeric', month: 'short' })} at ${time}`;
}

// After opening a chat: nothing is recorded until the operator says what
// happened. The same box on the To do tab and in a lead's side panel.
function waConfirmHtml(id, kind, openedAt, number, panelId = null) {
  const pid = panelId ? `'${panelId}'` : 'null';
  return `<div class="confirm-send">
    <div class="confirm-title">Did it send?</div>
    <div class="text-muted text-small">You opened this chat in WhatsApp ${esc(whenLocal(openedAt))}.
      Nothing is recorded until you say.</div>
    <div class="flex gap-2" style="flex-wrap:wrap;margin-top:10px">
      <button class="btn btn-primary" onclick="confirmWaSent(${id}, '${kind}', ${pid})">Sent</button>
      <button class="btn btn-ghost" onclick="markNotOnWhatsApp([${id}], ${pid})">Not on WhatsApp</button>
      <button class="btn btn-ghost" onclick="waDidntSend(${id}, ${pid})">Didn't send</button>
    </div>
    <a class="text-small" style="color:var(--blue);cursor:pointer;display:inline-block;margin-top:8px"
       onclick="reopenWaChat(${id}, '${esc(number || '')}', ${pid})">Open the chat again</a>
  </div>`;
}

function waOpensInToggleHtml() {
  if (isPhone()) return '';
  const inApp = waOpensIn() === 'app';
  return `<a class="text-muted text-small" style="cursor:pointer;text-decoration:underline"
             onclick="setWaOpensIn('${inApp ? 'web' : 'app'}');this.textContent='${inApp ? 'Use the desktop app instead' : 'Use WhatsApp Web instead'}'">
            ${inApp ? 'Use WhatsApp Web instead' : 'Use the desktop app instead'}</a>`;
}

// ── Audit ────────────────────────────────────────────────────────────────────

let _auditLinks = null;         // this operator's own links

async function loadAuditLinks(force = false) {
  if (_auditLinks && !force) return _auditLinks;
  _auditLinks = (await api('/api/audit-links')) || [];
  if (!Array.isArray(_auditLinks)) _auditLinks = [];
  return _auditLinks;
}

function _domainOf(d) {
  if (d.domain) return d.domain;
  try { return new URL(d.website.startsWith('http') ? d.website : `https://${d.website}`).hostname.replace(/^www\./, ''); }
  catch (_) { return ''; }
}

// The one-click links, built from what's on file. Grouped the way an agency
// works through a prospect. Some open a finished answer; the rest open a
// search you glance through.
function auditLinkGroups(d) {
  const enc = encodeURIComponent;
  const domain = _domainOf(d);
  const url = d.website ? (d.website.startsWith('http') ? d.website : `https://${d.website}`) : '';
  const name = d.company || d.name || '';
  const city = d.city || '';
  const category = d.category || 'business';
  const country = ((d.whatsapp && d.whatsapp.country) || d.country || '').toUpperCase();
  const socials = ((d.audit || {}).site || {}).socials || {};
  const q = text => `https://www.google.com/search?q=${enc(text)}`;
  const groups = [
    ['Website & tech', domain ? [
      ['PageSpeed report', `https://pagespeed.web.dev/report?url=${enc(url)}`],
      ['BuiltWith', `https://builtwith.com/${domain}`],
      ['Wayback Machine', `https://web.archive.org/web/*/${domain}`],
      ['Domain lookup', `https://who.is/whois/${domain}`],
      ['SSL Labs', `https://www.ssllabs.com/ssltest/analyze.html?d=${domain}&hideResults=on`],
      ['Email setup (MXToolbox)', `https://mxtoolbox.com/SuperTool.aspx?action=mx%3a${domain}&run=toolpage`],
    ] : []],
    ['SEO & AI search', [
      domain && ['Pages Google has', q(`site:${domain}`)],
      city && ['Google: ' + `${category} in ${city}`, q(`${category} in ${city}`)],
      url && ['Rich Results Test', `https://search.google.com/test/rich-results?url=${enc(url)}`],
      city && ['Ask ChatGPT', `https://chatgpt.com/?q=${enc(`What are the best ${category} options in ${city}?`)}`],
      city && ['Ask Perplexity', `https://www.perplexity.ai/search?q=${enc(`best ${category} in ${city}`)}`],
      domain && ['Ahrefs authority', `https://ahrefs.com/website-authority-checker?input=${domain}`],
      domain && ['Similarweb', `https://www.similarweb.com/website/${domain}/`],
    ].filter(Boolean)],
    ['Ads', [
      ['Meta Ad Library', `https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=${country || 'ALL'}&q=${enc(name)}&search_type=keyword_unordered`],
      domain && ['Google Ads Transparency', `https://adstransparency.google.com/?region=${country || 'anywhere'}&domain=${domain}`],
    ].filter(Boolean)],
    ['Reputation', [
      ['Google Maps listing', `https://www.google.com/maps/search/?api=1&query=${enc(`${name} ${d.address || city}`)}`],
      city && ['Competitors on Maps', `https://www.google.com/maps/search/${enc(`${category} in ${city}`)}`],
      ['Booking platforms', q(`"${name}" okadoc OR vezeeta OR practo`)],
    ].filter(Boolean)],
    ['Social & people', [
      ['Instagram', socials.Instagram || q(`${name} ${city} site:instagram.com`)],
      ['Facebook', socials.Facebook || q(`${name} ${city} site:facebook.com`)],
      ['TikTok', socials.TikTok || q(`${name} site:tiktok.com`)],
      ['LinkedIn page', socials.LinkedIn || q(`${name} site:linkedin.com/company`)],
      ['Owner or manager', q(`site:linkedin.com/in "${name}" owner OR founder OR manager OR director`)],
      ['Hiring', q(`"${name}" jobs ${city}`)],
      ['News', `https://www.google.com/search?tbm=nws&q=${enc(`"${name}"`)}`],
    ]],
  ];
  const fill = template => template.replace(/\{(domain|website|name|city|country|category|phone)\}/g, (_m, key) => enc({
    domain, website: url, name, city, country, category, phone: d.phone || '',
  }[key] || ''));
  const own = (_auditLinks || []).map(l => [l.label, fill(l.url)]);
  if (own.length) groups.push(['Your links', own]);
  return groups.filter(([, links]) => links.length);
}

function _score(label, value) {
  if (value === null || value === undefined) return '';
  const tone = value >= 90 ? 'green' : value >= 50 ? 'amber' : 'red';
  return `<div class="score ${tone}"><b>${value}</b><span>${esc(label)}</span></div>`;
}

function _yes(ok, yes, no) {
  return ok ? `<span style="color:var(--green)">✓</span> ${esc(yes)}` : `<span style="color:var(--amber)">✗</span> ${esc(no)}`;
}

function auditResultsHtml(a, ctx) {
  if (!a) return '';
  if (a.error) return `<div class="text-small" style="color:var(--amber)">${esc(a.error)}</div>`;
  const rows = [];
  const ps = a.pagespeed || {};
  if (ps.ok) {
    rows.push(`<div class="audit-row"><span class="k">Mobile scores</span><div>
      <div class="scores">${_score('Speed', ps.performance)}${_score('SEO', ps.seo)}${_score('Access.', ps.accessibility)}${_score('Best practice', ps.best_practices)}</div>
      ${ps.largest_paint ? `<div class="text-muted text-small">Main content shows after ${esc(ps.largest_paint)} on a phone</div>` : ''}
      ${ps.screenshot ? `<img src="${esc(ps.screenshot)}" alt="How the site looks on a phone" class="audit-shot" />` : ''}
    </div></div>`);
  } else if (ps.error) {
    rows.push(`<div class="audit-row"><span class="k">Mobile scores</span><div class="text-muted">${esc(ps.error)}</div></div>`);
  }
  const site = a.site || {};
  if (site.ok) {
    const ssl = a.ssl || {};
    rows.push(`<div class="audit-row"><span class="k">Website</span><div>
      ${site.built_with.length ? `Built with ${esc(site.built_with.join(', '))}<br>` : ''}
      ${_yes(site.https && ssl.valid !== false, `Secure (https)${ssl.expires ? `, certificate until ${esc(ssl.expires)}` : ''}`, ssl.problem || 'Not secure — no working https')}<br>
      ${_yes(site.mobile_viewport, 'Set up for phones', 'Not set up for phone screens')}<br>
      ${_yes(site.title && site.description, 'Title and description set', site.title ? 'No description for Google' : 'No page title')}<br>
      ${_yes(site.schema_types.length, `Structured data: ${site.schema_types.join(', ')}`, 'No structured data (helps Google and AI answers)')}
      ${site.copyright_year && site.copyright_year < new Date().getFullYear() - 1 ? `<br><span style="color:var(--amber)">Footer still says © ${site.copyright_year}</span>` : ''}
    </div></div>`);
    rows.push(`<div class="audit-row"><span class="k">Tracking</span><div>${site.tracking.length
      ? esc(site.tracking.join(', ')) : '<span style="color:var(--amber)">No analytics or ad pixels found</span>'}</div></div>`);
    if (site.engagement.length) rows.push(`<div class="audit-row"><span class="k">On the site</span><div>${esc(site.engagement.join(', '))}</div></div>`);
    const socials = Object.entries(site.socials || {});
    rows.push(`<div class="audit-row"><span class="k">Socials linked</span><div>${socials.length
      ? socials.map(([k, v]) => `<a href="${esc(v)}" target="_blank" rel="noopener" style="color:var(--blue)">${esc(k)} ↗</a>`).join(' · ')
      : '<span class="text-muted">None linked from the homepage</span>'}</div></div>`);
  } else if (site.error) {
    rows.push(`<div class="audit-row"><span class="k">Website</span><div style="color:var(--amber)">${esc(site.error)}</div></div>`);
  }
  const email = a.email || {};
  if (email.ok) rows.push(`<div class="audit-row"><span class="k">Email</span><div>${esc(email.provider)}</div></div>`);
  const arch = a.archive || {};
  const dom = a.domain || {};
  const age = [arch.first_seen && `site first seen ${arch.first_seen}`, dom.registered && `domain registered ${dom.registered}`,
               dom.expires && `expires ${dom.expires}`].filter(Boolean);
  if (age.length) rows.push(`<div class="audit-row"><span class="k">Age</span><div>${esc(age.join(' · '))}</div></div>`);
  if (ctx) rows.push(ratingContextRow(ctx));
  return `<div class="audit-results">${rows.join('')}</div>
    <div class="text-muted text-small" style="margin-top:6px">Checked ${esc((a.checked_at || '').substring(0, 16))} UTC</div>`;
}

function ratingContextRow(ctx) {
  return `<div class="audit-row"><span class="k">Google rating</span><div>${esc(ctx.rating)}★ from ${esc(ctx.reviews)} reviews
    <span class="text-muted">— the ${ctx.peers} others ${esc(ctx.scope)} average ${ctx.avg_rating}★ from ${ctx.avg_reviews};
    #${ctx.rank_by_reviews} of ${ctx.peers + 1} by reviews</span></div></div>`;
}

function auditSectionHtml(d) {
  const groups = auditLinkGroups(d);
  return `<div class="audit-section" id="audit-${d.id}">
    <div class="flex items-center gap-2" style="flex-wrap:wrap">
      ${d.website ? `<button class="btn btn-ghost btn-sm" onclick="runAudit(${d.id})" id="audit-run-${d.id}">⚡ Run checks</button>
        <span class="text-muted text-small" id="audit-status-${d.id}">${d.audit ? '' : 'Speed & SEO scores, tech, pixels, email, site age — about a minute'}</span>`
      : '<span class="text-muted text-small">No website on file, so there\'s nothing to check automatically.</span>'}
    </div>
    <div id="audit-results-${d.id}">${d.audit ? auditResultsHtml(d.audit, d.rating_context)
      : (d.rating_context ? `<div class="audit-results">${ratingContextRow(d.rating_context)}</div>` : '')}</div>
    <div class="audit-links">${groups.map(([title, links]) => `
      <div class="audit-link-group"><span class="k">${esc(title)}</span>
        <div class="chips">${links.map(([label, href]) =>
          `<a class="chip" href="${esc(href)}" target="_blank" rel="noopener">${esc(label)} ↗</a>`).join('')}</div>
      </div>`).join('')}
      <div class="text-muted text-small">Add your own links under Settings → Audit links.</div>
    </div>
  </div>`;
}

const _auditPolls = {};

async function runAudit(businessId) {
  const btn = document.getElementById(`audit-run-${businessId}`);
  const status = document.getElementById(`audit-status-${businessId}`);
  const res = await api(`/api/businesses/${businessId}/audit`, 'POST');
  if (!res || res.error) { toast((res && res.error) || 'Could not start the checks', 'err'); return; }
  if (btn) btn.disabled = true;
  if (status) status.textContent = 'Running checks… this takes about a minute. You can keep working.';
  clearInterval(_auditPolls[businessId]);
  const started = Date.now();
  _auditPolls[businessId] = setInterval(async () => {
    const data = await api(`/api/businesses/${businessId}/audit`);
    if (data && !data.running) {
      clearInterval(_auditPolls[businessId]);
      const el = document.getElementById(`audit-results-${businessId}`);
      if (el) el.innerHTML = auditResultsHtml(data.audit, data.rating_context);
      const b = document.getElementById(`audit-run-${businessId}`);
      const s = document.getElementById(`audit-status-${businessId}`);
      if (b) { b.disabled = false; b.textContent = '⚡ Run checks again'; }
      if (s) s.textContent = '';
    } else if (Date.now() - started > 150000) {
      clearInterval(_auditPolls[businessId]);
      if (status) status.textContent = 'Still running — check back in a minute.';
      if (btn) btn.disabled = false;
    }
  }, 4000);
}

// ── Notes and history ────────────────────────────────────────────────────────

async function saveBusinessNotes(id, notes) {
  const res = await api(`/api/businesses/${id}`, 'PUT', { notes });
  if (!res || res.error) { toast((res && res.error) || 'Could not save the note', 'err'); return; }
  toast('Note saved');
}

function notesHtml(d) {
  return `<textarea class="soft-input" style="min-height:80px"
            placeholder="Anything worth remembering — what you found in the audit, who you spoke to…"
            onchange="saveBusinessNotes(${d.id}, this.value)">${esc(d.notes || '')}</textarea>`;
}

function historyHtml(d) {
  const t = d.timeline || [];
  if (!t.length) return '<div class="text-muted text-small">Nothing sent, called or messaged yet.</div>';
  return `<div class="timeline">${t.slice(0, 40).map(item => `
    <div class="item">
      <span class="when">${esc((item.at || '').substring(0, 16))}</span>
      <span class="channel-${item.channel}">●</span> ${esc(item.text)}
      ${item.detail ? `<div class="detail">${esc(item.detail.length > 240 ? item.detail.substring(0, 240) + '…' : item.detail)}</div>` : ''}
    </div>`).join('')}</div>`;
}

function whereItIsHtml(d) {
  const where = [];
  (d.enrollments || []).slice(0, 3).forEach(e =>
    where.push(`<div>${pill('Email', 'green')} ${esc(e.campaign)} — ${esc(e.status === 'queued' ? `step ${e.current_step}` : e.status)}</div>`));
  if ((d.emails || []).length && !(d.enrollments || []).length) where.push(`<div>${pill('Email', 'green')} not in a campaign yet</div>`);
  if (d.call && !d.call.removed_at) {
    const camps = (d.call.campaigns || []).map(c => esc(c.name)).join(', ');
    where.push(`<div>${pill('Calling', 'blue')} ${esc(callOutcomeLabel(d.call.call_status))}${
      d.call.next_call_at ? ` · next ${esc(d.call.next_call_at.substring(0, 16))}` : ''}${camps ? ` · ${camps}` : ''}</div>`);
  }
  if (d.whatsapp) {
    const w = d.whatsapp;
    const off = w.moved_to || w.removed_at;
    where.push(`<div>${pill('WhatsApp', off ? 'dashed' : 'purple')} ${off ? (w.moved_to ? 'moved off WhatsApp' : 'taken off')
      : `${esc((WA_STAGE_META[w.stage] || [w.stage])[0].toLowerCase())} · ${esc(w.campaign_name || 'no campaign')}`}</div>`);
  }
  if (d.do_not_contact) where.push(`<div>${pill('Do not contact', 'red')} asked to be left alone</div>`);
  if (!where.length) where.push(`<div>${pill('Unassigned', 'dashed')} not on any channel</div>`);
  return `<div style="display:flex;flex-direction:column;gap:6px;font-size:12.5px">${where.join('')}</div>`;
}

function callOutcomeLabel(key) {
  if (!key) return 'never called';
  const meta = (typeof CALL_STATUS_META !== 'undefined') && CALL_STATUS_META[key];
  return meta ? meta.label.toLowerCase() : String(key).replace(/_/g, ' ');
}

// Notes, audit and history, each folded so the panel stays short. Used
// under the send controls on WhatsApp's To do tab, and in every lead panel.
function sharedSectionsHtml(d, { open = 'notes' } = {}) {
  return `
    <details class="panel-fold" ${open === 'notes' || open === 'all' ? 'open' : ''}>
      <summary>Notes</summary>${notesHtml(d)}</details>
    <details class="panel-fold" ${open === 'audit' || open === 'all' ? 'open' : ''}>
      <summary>Audit ${d.audit ? '<span class="text-muted text-small">· checked</span>' : ''}</summary>${auditSectionHtml(d)}</details>
    <details class="panel-fold">
      <summary>History <span class="text-muted text-small">· ${(d.timeline || []).length}</span></summary>${historyHtml(d)}</details>`;
}

// ── The panel itself ─────────────────────────────────────────────────────────
//
// opts.channel picks the controls at the top: 'whatsapp', 'calling', 'email',
// or nothing (Contacts). opts.panelId / opts.splitId are the elements to
// fill and to widen. opts.onChange runs after anything that changes the lead.

const LeadPanel = { current: {} };

async function openLeadPanel(businessId, opts) {
  const panel = document.getElementById(opts.panelId);
  if (!panel) return;
  const [d] = await Promise.all([api(`/api/businesses/${businessId}`), loadAuditLinks()]);
  if (!d || d.error) { toast((d && d.error) || 'Could not open that lead', 'err'); return; }
  const sameLead = LeadPanel.current[opts.panelId] && LeadPanel.current[opts.panelId].businessId === businessId;
  LeadPanel.current[opts.panelId] = { businessId, opts };
  const split = document.getElementById(opts.splitId);
  if (split) split.style.gridTemplateColumns = '';
  panel.style.display = 'block';
  // Full screen on a phone; the phone's back gesture closes it like the ✕ does.
  openSheet(opts.panelId, () => closeLeadPanel(opts.panelId));
  if (!sameLead) panel.scrollTop = 0;

  let channelHtml = '';
  if (opts.channel === 'whatsapp' && d.whatsapp) channelHtml = await _waPanelSection(d);
  if (opts.channel === 'calling') channelHtml = _callPanelSection(d);
  if (opts.channel === 'email') channelHtml = _emailPanelSection(d);

  const links = [];
  if (d.phone) links.push(`<a href="tel:${esc(d.phone)}" style="color:var(--blue)">${esc(d.phone)}</a>`);
  if (d.website) links.push(`<a href="${esc(d.website.startsWith('http') ? d.website : 'https://' + d.website)}" target="_blank" rel="noopener" style="color:var(--blue)">${esc(d.domain || d.website)} ↗</a>`);
  const facts = [d.category, d.city || d.address, d.rating != null ? `${d.rating}★ (${d.review_count ?? 0})` : '']
    .filter(Boolean).map(esc).join(' · ');

  panel.innerHTML = `
    ${sheetBarHtml(`closeLeadPanel('${opts.panelId}')`)}
    <div class="flex items-center gap-2" style="justify-content:space-between">
      <h3>${esc(d.company || 'Unnamed business')}</h3>
      <div class="flex gap-2">
        <button class="btn btn-ghost btn-sm" onclick="openBusinessForm(${d.id})" title="Edit details">✎</button>
        <button class="btn btn-ghost btn-sm" onclick="closeLeadPanel('${opts.panelId}')" title="Close">✕</button>
      </div>
    </div>
    <div class="text-small" style="display:flex;gap:10px;flex-wrap:wrap">${links.join('')}</div>
    ${facts ? `<div class="text-muted text-small" style="margin-top:2px">${facts}</div>` : ''}
    ${channelHtml}
    <span class="field-label">Where it is</span>
    ${whereItIsHtml(d)}
    ${_addToChannelButtons(d, opts.channel)}
    ${!opts.channel && (d.emails || []).length ? `<span class="field-label">Email addresses</span>
      <div style="display:flex;flex-direction:column;gap:4px">${d.emails.map(e =>
        `<div class="text-small"><span class="mono">${esc(e.email)}</span> ${
          e.status !== 'active' ? pill(e.status, 'red') : ''}${e.duplicate_of ? ' <span class="text-muted">(backup)</span>' : ''}</div>`).join('')}</div>` : ''}
    <div style="margin-top:12px">${sharedSectionsHtml(d, { open: opts.open || 'notes' })}</div>`;
}

// The channels this business could still go to, as one row of buttons.
function _addToChannelButtons(d, channel) {
  if (d.do_not_contact) return '';
  const onWa = d.whatsapp && !d.whatsapp.removed_at && !d.whatsapp.moved_to;
  const ruledOut = d.whatsapp && d.whatsapp.moved_to;
  const onCall = d.call && !d.call.removed_at;
  const buttons = [
    channel !== 'email' && (d.emails || []).length
      &&`<button class="btn btn-ghost btn-sm" onclick="contactsToEmail([${d.id}])">+ Email campaign</button>`,
    channel !== 'calling' && !onCall && d.phone
      && `<button class="btn btn-ghost btn-sm" onclick="contactsToCalling([${d.id}])">+ Calling</button>`,
    channel !== 'whatsapp' && !onWa && !ruledOut && d.phone
      && `<button class="btn btn-ghost btn-sm" onclick="contactsToWhatsApp([${d.id}])">+ WhatsApp</button>`,
  ].filter(Boolean);
  return buttons.length ? `<div class="flex gap-2" style="flex-wrap:wrap;margin-top:10px">${buttons.join('')}</div>` : '';
}

function closeLeadPanel(panelId) {
  const cur = LeadPanel.current[panelId];
  const panel = document.getElementById(panelId);
  if (panel) panel.style.display = 'none';
  if (cur) {
    const split = document.getElementById(cur.opts.splitId);
    if (split) split.style.gridTemplateColumns = '1fr';
    if (cur.opts.onClose) cur.opts.onClose();
  }
  delete LeadPanel.current[panelId];
  closeSheet(panelId);
}

function refreshLeadPanel(panelId) {
  const cur = LeadPanel.current[panelId];
  if (cur) openLeadPanel(cur.businessId, cur.opts);
}

function _leadChanged(panelId) {
  const cur = LeadPanel.current[panelId];
  if (cur && cur.opts.onChange) cur.opts.onChange();
  refreshLeadPanel(panelId);
}

async function _waPanelSection(d) {
  const w = await api(`/api/wa/leads/${d.whatsapp.id}`);
  const stage = d.whatsapp.stage;
  const panelId = 'wl-panel';
  const lastSent = w.sent_date ? `<div class="text-muted text-small" style="margin-top:8px">Last sent ${esc(whenLocal(w.sent_date))}
      · ${w.followup_count || 0} follow-up${w.followup_count === 1 ? '' : 's'} so far</div>` : '';
  let body = '';
  if (stage === 'ready' || stage === 'due') {
    const kind = stage === 'ready' ? 'opener' : 'followup';
    const actions = w.opened_at
      ? waConfirmHtml(w.id, kind, w.opened_at, w.wa_number, panelId)
      : `<div class="flex gap-2" style="flex-wrap:wrap;margin-top:10px;align-items:center">
          <button class="btn btn-primary btn-sm" onclick="openFromPanel(${w.id}, '${kind}', '${esc(w.wa_number)}', '${panelId}')">Open in WhatsApp</button>
          ${kind === 'opener'
            ? `<button class="btn btn-ghost btn-sm" onclick="rewordWaMessage(${w.id}, '${panelId}')">✨ Reword with AI</button>`
            : `<button class="btn btn-ghost btn-sm" onclick="waMarkReplied(${w.id}, true)">They replied</button>
               <button class="btn btn-ghost btn-sm" onclick="waSetPaused([${w.id}], true)">Pause follow-ups</button>`}
        </div>`;
    body = `${kind === 'followup' ? lastSent : ''}
      <span class="field-label">${kind === 'opener' ? `Message${w.template_variant ? ` · version ${esc(w.template_variant)}` : ''}` : 'Follow-up due'}</span>
      <textarea id="wl-msg" class="soft-input" style="min-height:110px"
                ${kind === 'opener' ? `onchange="saveWaMessage(${w.id}, this.value, '${panelId}')"` : ''}>${esc(kind === 'opener' ? (w.message || '') : (w.followup_message || ''))}</textarea>
      ${kind === 'opener' && w.message_edited ? `<div class="text-muted text-small">${w.paraphrased ? 'Reworded by AI' : 'Edited by hand'} ·
        <a style="color:var(--blue);cursor:pointer" onclick="resetWaMessage(${w.id}, '${panelId}')">Reset to template</a></div>` : ''}
      <div id="wl-actions">${actions}</div>`;
  } else if (stage === 'waiting' || stage === 'paused') {
    body = `${lastSent}
      <span class="field-label">Next follow-up</span>
      <div class="box">${esc(w.followup_message || '')}</div>
      <div class="flex gap-2" style="flex-wrap:wrap;margin-top:10px">
        <button class="btn btn-ghost btn-sm" onclick="waMarkReplied(${w.id}, true)">They replied</button>
        <button class="btn btn-ghost btn-sm" onclick="waSetPaused([${w.id}], ${stage !== 'paused'})">${stage === 'paused' ? 'Resume follow-ups' : 'Pause follow-ups'}</button>
      </div>`;
  } else if (stage === 'replied') {
    body = `<div class="text-small" style="margin-top:8px">${pill('Replied', 'green')} No more follow-ups.
      <a style="color:var(--blue);cursor:pointer" onclick="waMarkReplied(${w.id}, false)">Undo</a></div>`;
  } else if (stage === 'no_whatsapp') {
    body = `<div class="text-small" style="margin-top:8px;line-height:1.55">Marked not on WhatsApp ${esc(whenLocal(w.no_whatsapp_at))}.
        It's out of every queue until you move it off.</div>
      <div class="flex gap-2" style="flex-wrap:wrap;margin-top:10px">
        <button class="btn btn-primary btn-sm" onclick="moveWaLeadsOff([${w.id}])">Move off WhatsApp…</button>
        <button class="btn btn-ghost btn-sm" onclick="unmarkNotOnWhatsApp([${w.id}])">It's on WhatsApp after all</button>
      </div>`;
  }
  const on = !['moved', 'removed'].includes(stage);
  return `<span class="field-label">WhatsApp</span>
    <div class="text-small">${waStagePill(stage)} <span class="mono">${esc(prettyWaNumber(w.wa_number))}</span>
      · ${w.campaign_name ? esc(w.campaign_name) : pill('No campaign', 'amber')}</div>
    ${body}
    ${on ? `<div class="flex gap-2" style="flex-wrap:wrap;margin-top:8px">
      <button class="btn btn-ghost btn-sm" onclick="moveWaLeadsToCampaign([${w.id}])">Move campaign</button>
      ${stage !== 'no_whatsapp' ? `<button class="btn btn-ghost btn-sm" onclick="markNotOnWhatsApp([${w.id}])">Not on WhatsApp</button>` : ''}
      <button class="btn btn-ghost btn-sm" style="color:var(--red)" onclick="removeWaLeads([${w.id}])">Take off</button>
    </div>` : ''}`;
}

function _callPanelSection(d) {
  const c = d.call;
  if (!c || c.removed_at) return '';
  const camps = (c.campaigns || []).map(x => pill(x.name)).join(' ');
  return `<span class="field-label">Calling</span>
    <div class="text-small">${callStatusBadge(c.call_status)} · ${c.call_attempts || 0} attempt${c.call_attempts === 1 ? '' : 's'}
      ${c.next_call_at ? ` · next call ${esc(c.next_call_at.substring(0, 16))}` : ''}</div>
    ${camps ? `<div style="margin-top:6px">${camps}</div>` : ''}
    <div class="flex gap-2" style="flex-wrap:wrap;margin-top:10px">
      <button class="btn btn-primary btn-sm" onclick="openInDialler(${d.id})">☎ Open in the dialler</button>
      <button class="btn btn-ghost btn-sm" onclick="callLeadsToCampaign([${d.id}])">Add to campaign</button>
      ${c.call_status ? `<button class="btn btn-ghost btn-sm" onclick="reopenCallLead(${d.id})">Reopen</button>` : ''}
      <button class="btn btn-ghost btn-sm" style="color:var(--red)" onclick="removeFromCalling([${d.id}])">Take off Calling</button>
    </div>`;
}

function _emailPanelSection(d) {
  const emails = d.emails || [];
  if (!emails.length) return '';
  return `<span class="field-label">Email</span>
    <div style="display:flex;flex-direction:column;gap:4px">${emails.map(e => {
      const enr = (d.enrollments || []).filter(x => x.email === e.email);
      return `<div class="text-small"><span class="mono">${esc(e.email)}</span> ${e.status !== 'active' ? pill(e.status, 'red') : ''}
        ${enr.map(x => `<span class="text-muted"> · ${esc(x.campaign)}: ${esc(x.status === 'queued' ? `step ${x.current_step}` : x.status)}</span>`).join('')}</div>`;
    }).join('')}</div>
    <div class="flex gap-2" style="flex-wrap:wrap;margin-top:10px">
      <button class="btn btn-ghost btn-sm" onclick="contactsToEmail([${d.id}])">Enroll in a campaign</button>
    </div>`;
}
