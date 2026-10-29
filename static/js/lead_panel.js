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

// ── Pipeline stage ───────────────────────────────────────────────────────────
//
// How far along a business is, shared by every channel: booked over WhatsApp
// reads as booked from Email too. Lives here rather than in whatsapp.js
// because the side panel shows it on all three, and this file loads first.

let _pipelineStages = null;

async function loadPipelineStages(force = false) {
  if (_pipelineStages && !force) return _pipelineStages;
  _pipelineStages = await api('/api/pipeline-stages') || [];
  return _pipelineStages;
}

function pipelineStage(key) {
  return (_pipelineStages || []).find(s => s.key === key) || null;
}

const _PIPE_TONE = { good: 'green', bad: 'red', info: 'blue', amber: 'amber' };

// The pill a business carries wherever it's listed. Falls back to the raw key
// so a stage the other operator invented, or one archived since, still reads
// as something rather than vanishing.
function pipelinePill(row) {
  const key = row.pipeline_stage || '';
  if (!key) return '';
  const s = pipelineStage(key);
  const label = s ? s.label : (row.pipeline_label || key);
  const via = row.pipeline_channel ? ` · via ${row.pipeline_channel}` : '';
  const when = row.pipeline_at ? ` · ${shortDate(row.pipeline_at)}` : '';
  const due = row.next_action_at ? ` · due ${shortDate(row.next_action_at)}` : '';
  return pill(label, _PIPE_TONE[s && s.tone] || '', `${label}${via}${when}${due}`);
}

// Overdue is worth shouting about — it's the whole reason next_action_at
// exists. Compared by date only: a thing due today isn't late yet.
function pipelineOverdue(row) {
  if (!row.next_action_at) return false;
  return String(row.next_action_at).substring(0, 10) < new Date().toISOString().substring(0, 10);
}

// A stored value ('YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS') as whichever kind of
// picker the stage wants. No timezone conversion, on purpose: a meeting time
// is the operator's own wall clock, exactly like Calling's next_call_at
// already works, and since a lead is only ever seen by the one operator who
// owns it, "what I typed" and "what I read back" are always the same person.
function _pipeInputValue(stored, wantsTime) {
  const text = String(stored || '');
  return wantsTime ? text.substring(0, 16).replace(' ', 'T') : text.substring(0, 10);
}

/**
 * Set (or clear) a business's stage. `channel` is where it was set from, so
 * the other channels can say "booked — via whatsapp" instead of leaving you
 * to guess. `after` runs on success.
 */
async function setPipelineStage(businessId, channel, after) {
  const stages = await loadPipelineStages();
  const live = stages.filter(s => !s.archived);
  const [current, zoho] = await Promise.all([api(`/api/businesses/${businessId}`), loadZohoStatus()]);
  const calendars = zoho && zoho.connected ? await loadZohoCalendars() : [];
  const now = (current && current.pipeline_stage) || '';
  const nowStage = pipelineStage(now);
  const asked = chooseDialog({
    title: 'Where is this one up to?',
    width: 560,
    body: `<label class="field-label">Stage</label>
      <select id="pl-stage" class="filter-select" style="width:100%;max-width:100%"
              onchange="_pipeDateToggle()">
        <option value="">Nothing yet</option>
        ${live.map(s => `<option value="${esc(s.key)}" data-date="${s.wants_date ? 1 : 0}"
           data-time="${s.wants_time ? 1 : 0}"
           ${s.key === now ? 'selected' : ''}>${esc(s.label)}</option>`).join('')}
      </select>
      <div id="pl-date-wrap" style="display:${nowStage && nowStage.wants_date ? 'block' : 'none'}">
        <label class="field-label" id="pl-date-label">${nowStage && nowStage.wants_time ? 'When is it?' : 'When is it due?'}</label>
        <input id="pl-date" type="${nowStage && nowStage.wants_time ? 'datetime-local' : 'date'}" class="soft-input"
               value="${esc(_pipeInputValue(current && current.next_action_at, nowStage && nowStage.wants_time))}" />
      </div>
      <div id="pl-meeting-wrap" style="display:${now === MEETING_STAGE ? 'block' : 'none'}">
        ${meetingFieldsHtml(current || {}, zoho, calendars)}
      </div>
      <div class="form-hint" style="margin-top:8px">This is the business, not just this
        channel — it shows the same wherever you open them. Setting a stage stops their
        WhatsApp follow-ups.</div>`,
    confirm: 'Save',
    collect: () => {
      const stage = document.getElementById('pl-stage').value;
      const dateEl = document.getElementById('pl-date');
      const out = { stage, next_action_at: stage && dateEl ? dateEl.value : null };
      if (stage === MEETING_STAGE) {
        const meeting = collectMeetingFields();
        if (!meeting) return null;
        out.meeting = meeting;
      }
      return out;
    },
  });
  const emailInput = document.getElementById('pl-email');
  if (emailInput && emailInput.value) _meetingEmailChanged(emailInput);
  const values = await asked;
  if (!values) return false;
  const res = await api(`/api/businesses/${businessId}/pipeline`, 'PUT',
                        { ...values, channel });
  if (!res || res.error) { toast((res && res.error) || 'Could not save that', 'err'); return false; }
  if (res.meeting) toastMeeting(res);
  else toast(values.stage ? 'Stage saved' : 'Stage cleared');
  if (after) after(res);
  return true;
}

// Only stages that want one ask for a date — "proposal due" does, "replied"
// doesn't, and showing an empty date box on every stage invites junk. A stage
// that wants a real appointment time ('Meeting booked') swaps to a
// date+time picker rather than a bare date. Switching type clears whatever
// was typed, since a date string and a datetime-local string aren't the same
// shape — there's nothing sensible to carry across.
function _pipeDateToggle() {
  const sel = document.getElementById('pl-stage');
  const wrap = document.getElementById('pl-date-wrap');
  const input = document.getElementById('pl-date');
  const label = document.getElementById('pl-date-label');
  if (!sel || !wrap || !input) return;
  const opt = sel.options[sel.selectedIndex];
  const wantsDate = opt && opt.dataset.date === '1';
  const wantsTime = opt && opt.dataset.time === '1';
  wrap.style.display = wantsDate ? 'block' : 'none';
  const type = wantsTime ? 'datetime-local' : 'date';
  if (input.type !== type) { input.type = type; input.value = ''; }
  if (label) label.textContent = wantsTime ? 'When is it?' : 'When is it due?';
  const meeting = document.getElementById('pl-meeting-wrap');
  if (meeting) meeting.style.display = sel.value === MEETING_STAGE ? 'block' : 'none';
}

// ── Meetings: booked onto Zoho Calendar ──────────────────────────────────────
//
// 'booked' is the one stage that's a real meeting, and the only one that goes
// on a calendar (the same exact key Calling's "Meeting booked" outcome uses).
// The client hears from us -- an invite from Zoho, from your own address --
// only when a box saying so is ticked. Changing or clearing the stage never
// cancels a meeting; only Cancel meeting does, so a mis-click can't email a
// client a cancellation.

const MEETING_STAGE = 'booked';
const MEETING_LENGTHS = [15, 30, 45, 60, 90, 120];

let _zohoStatus = null;
let _zohoCalendars = null;

async function loadZohoStatus(force = false) {
  if (_zohoStatus && !force) return _zohoStatus;
  const s = await api('/api/zoho/status');
  _zohoStatus = s && !s.error ? s : null;
  return _zohoStatus;
}

async function loadZohoCalendars(force = false) {
  if (_zohoCalendars && !force) return _zohoCalendars;
  const res = await api('/api/zoho/calendars');
  _zohoCalendars = (res && res.calendars) || [];
  return _zohoCalendars;
}

function browserTimezone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (_) { return 'UTC'; }
}

function timezoneOptionsHtml(selected) {
  let zones = [];
  try { zones = Intl.supportedValuesOf('timeZone'); } catch (_) { zones = []; }
  if (!zones.includes('UTC')) zones = ['UTC', ...zones];
  if (selected && !zones.includes(selected)) zones = [selected, ...zones];
  return zones.map(z => `<option value="${esc(z)}" ${z === selected ? 'selected' : ''}>${esc(z.replace(/_/g, ' '))}</option>`).join('');
}

function meetingLengthOptionsHtml(selected) {
  const lengths = MEETING_LENGTHS.includes(+selected) ? MEETING_LENGTHS : [...MEETING_LENGTHS, +selected].sort((a, b) => a - b);
  return lengths.map(n => `<option value="${n}" ${n === +selected ? 'selected' : ''}>${
    n < 60 ? `${n} minutes` : n === 60 ? '1 hour' : `${n / 60} hours`}</option>`).join('');
}

// A calendar shared with you read-only can't take a meeting; say so rather
// than let Zoho refuse it after you've booked.
function calendarOptionsHtml(calendars, selected) {
  return calendars.map(c => `<option value="${esc(c.uid)}" data-name="${esc(c.name)}" ${c.uid === selected ? 'selected' : ''}>${
    esc(c.name)}${/view|free/i.test(c.privilege || '') ? ' (view only)' : ''}</option>`).join('');
}

// The meeting part of the stage dialog. Prefilled from the meeting already
// booked, then your own defaults (My settings), then this browser's timezone.
function meetingFieldsHtml(d, zoho, calendars) {
  const m = d.meeting && !d.meeting.cancelled_at ? d.meeting : null;
  const p = (zoho && zoho.prefs) || {};
  const tz = (m && m.timezone) || p.timezone || browserTimezone();
  const minutes = (m && m.minutes) || p.minutes || 60;
  const kind = (m && m.link_kind) || p.link_kind || 'zmeeting';
  const ownLink = m ? m.own_link : (p.own_link || '');
  const leadEmails = (d.emails || []).filter(e => e.status !== 'deleted').map(e => e.email);
  const email = (m && m.invite_email) || leadEmails[0] || '';
  const connected = zoho && zoho.connected;

  let calendar;
  if (!connected) {
    calendar = `<div class="form-hint" style="margin-top:10px">${zoho && zoho.configured
      ? `Zoho Calendar isn't connected, so this is saved here only.
         <a style="color:var(--blue);cursor:pointer" onclick="chooseCancel(); openSettingsTab('mine')">Connect it in My settings</a>`
      : 'Zoho Calendar isn\'t set up yet (an admin adds it in Settings → Team setup), so this is saved here only.'}</div>`;
  } else if (m && m.event_uid) {
    calendar = `<div class="text-muted text-small" style="margin-top:10px">On <b>${esc(m.calendar_name || 'your calendar')}</b></div>`;
  } else {
    const chosen = (m && m.calendar_uid) || zoho.calendar_uid;
    calendar = `<label class="field-label">Calendar</label>
      <select id="pl-cal" class="filter-select" style="width:100%;max-width:100%">${calendarOptionsHtml(calendars, chosen)}</select>`;
  }

  let invite = '';
  if (connected && m && m.invited_at) {
    invite = `<label class="check-line"><input type="checkbox" id="pl-notify" checked />
      If the time changes, email <b>${esc(m.invite_email)}</b> the new time</label>`;
  } else if (connected) {
    // Follows the email box until you tick or untick it yourself: typing the
    // client's address is almost always so they get the invite.
    invite = `<label class="check-line"><input type="checkbox" id="pl-invite" ${email ? 'checked' : ''}
        onchange="this.dataset.touched = '1'" />
      Email them an invite, from your Zoho address</label>`;
  }

  return `
    <div class="flex gap-2" style="flex-wrap:wrap">
      <div style="flex:2;min-width:180px"><label class="field-label">Timezone</label>
        <select id="pl-tz" class="filter-select" style="width:100%;max-width:100%">${timezoneOptionsHtml(tz)}</select></div>
      <div style="flex:1;min-width:120px"><label class="field-label">Length</label>
        <select id="pl-minutes" class="filter-select" style="width:100%;max-width:100%">${meetingLengthOptionsHtml(minutes)}</select></div>
    </div>
    ${calendar}
    <label class="field-label">Meeting link</label>
    <select id="pl-link" class="filter-select" style="width:100%;max-width:100%"
            onchange="document.getElementById('pl-own-link').style.display = this.value === 'own' ? 'block' : 'none'">
      <option value="zmeeting" ${kind === 'zmeeting' ? 'selected' : ''}>Zoho Meeting — a new room for this meeting</option>
      <option value="own" ${kind === 'own' ? 'selected' : ''}>My own link (e.g. Google Meet)</option>
      <option value="none" ${kind === 'none' ? 'selected' : ''}>No link</option>
    </select>
    <input id="pl-own-link" class="soft-input" placeholder="https://meet.google.com/…" value="${esc(ownLink)}"
           style="display:${kind === 'own' ? 'block' : 'none'};margin-top:6px" />
    <label class="field-label">Client's email</label>
    <input id="pl-email" class="soft-input" type="email" placeholder="the address they sent you" value="${esc(email)}"
           data-business="${d.id}" data-known="${esc(JSON.stringify(leadEmails))}"
           oninput="_meetingEmailChanged(this)" />
    <div id="pl-email-warn" class="form-hint" style="color:var(--amber)"></div>
    <label class="check-line" id="pl-save-email-line" style="display:${email && !leadEmails.includes(email) ? 'flex' : 'none'}">
      <input type="checkbox" id="pl-save-email" ${leadEmails.length ? '' : 'checked'} />
      Also save it to this lead (it isn't put in any email campaign)</label>
    ${invite}`;
}

// Reads the meeting part of the dialog; null (with a toast) if it won't do.
function collectMeetingFields() {
  const val = id => (document.getElementById(id) || {}).value || '';
  const checked = id => !!(document.getElementById(id) || {}).checked;
  const kind = val('pl-link');
  const ownLink = val('pl-own-link').trim();
  const email = val('pl-email').trim();
  if (kind === 'own' && !/^https?:\/\/\S+$/.test(ownLink)) {
    toast('Paste the meeting link (starting https://), or pick Zoho Meeting', 'err'); return null;
  }
  if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { toast("That email doesn't look right", 'err'); return null; }
  if (checked('pl-invite') && !email) { toast("Type the client's email to send them an invite", 'err'); return null; }
  const cal = document.getElementById('pl-cal');
  const calOpt = cal && cal.options[cal.selectedIndex];
  return {
    timezone: val('pl-tz'), minutes: +val('pl-minutes') || 60,
    link_kind: kind, own_link: kind === 'own' ? ownLink : '',
    invite_email: email,
    save_email: checked('pl-save-email') && document.getElementById('pl-save-email-line').style.display !== 'none',
    send_invite: checked('pl-invite'), notify_change: checked('pl-notify'),
    calendar_uid: calOpt ? calOpt.value : '', calendar_name: calOpt ? calOpt.dataset.name : '',
  };
}

let _emailCheckTimer = null;

// A warning -- never a block -- when an invite is about to go to an address
// that unsubscribed or bounced, or a business marked do not contact.
function _meetingEmailChanged(input, warnId = 'pl-email-warn', saveLineId = 'pl-save-email-line') {
  const email = input.value.trim().toLowerCase();
  let known = [];
  try { known = JSON.parse(input.dataset.known || '[]'); } catch (_) { known = []; }
  const line = document.getElementById(saveLineId);
  if (line) line.style.display = email && !known.includes(email) ? 'flex' : 'none';
  const inviteBox = document.getElementById('pl-invite');
  if (inviteBox && !inviteBox.dataset.touched && input.id === 'pl-email') inviteBox.checked = !!email;
  clearTimeout(_emailCheckTimer);
  _emailCheckTimer = setTimeout(async () => {
    const warn = document.getElementById(warnId);
    if (!warn) return;
    if (!email.includes('@')) { warn.textContent = ''; return; }
    const res = await api(`/api/meetings/address-check?email=${encodeURIComponent(email)}&business_id=${input.dataset.business || ''}`);
    warn.textContent = res && res.warning ? `⚠ ${res.warning} You can still send it.` : '';
  }, 400);
}

// One toast that says what happened to the meeting, not just the stage.
function toastMeeting(res) {
  const m = res.meeting;
  const saved = res.email_error ? ` (the address wasn't saved: ${res.email_error})` : '';
  if (m.status === 'failed') { toast(`Booked, but it isn't on your calendar — Zoho said: ${m.sync_error}${saved}`, 'err'); return; }
  if (m.status === 'on_calendar') {
    toast((m.invited_at && m.invite_email ? `Booked — on your calendar, and ${m.invite_email} has an invite` : 'Booked — on your calendar') + saved);
    return;
  }
  toast(`Booked${saved}`);
}

// "Thu 5 Nov, 10:00 · 1 hour", with the timezone when it isn't this browser's.
function meetingWhenText(m) {
  const d = new Date(String(m.starts_at || '').replace(' ', 'T'));
  const when = isNaN(d) ? (m.starts_at || '') : d.toLocaleString([], {
    weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  const length = m.minutes < 60 ? `${m.minutes} min` : m.minutes === 60 ? '1 hour' : `${m.minutes / 60} hours`;
  const tz = m.timezone && m.timezone !== browserTimezone() ? ` · ${m.timezone.replace(/_/g, ' ')}` : '';
  return `${when} · ${length}${tz}`;
}

// The meeting on a lead's panel: when, whether it's on the calendar, the
// join link, the invite, and the only way to cancel it.
function meetingBoxHtml(d, opts) {
  const m = d.meeting;
  if (!m || m.cancelled_at) return '';
  const pid = opts.panelId;
  const zoho = _zohoStatus;
  const connected = zoho && zoho.connected;
  const link = (text, call) => `<a style="color:var(--blue);cursor:pointer" onclick="${call}">${text}</a>`;
  let cal;
  if (m.sync_error) {
    cal = `<span style="color:var(--red)">Not on the calendar — Zoho said: ${esc(m.sync_error)}</span>
      · ${link('Try again', `retryMeetingSync(${d.id}, '${pid}')`)}`;
  } else if (m.event_uid) {
    cal = `<span style="color:var(--green)">✓</span> On ${esc(m.calendar_name || 'your calendar')}`;
  } else if (connected) {
    cal = `Not on a calendar yet · ${link('Add it to your calendar', `retryMeetingSync(${d.id}, '${pid}')`)}`;
  } else {
    cal = `<span class="text-muted">Not on a calendar — ${link('connect Zoho Calendar', "openSettingsTab('mine')")}</span>`;
  }
  const invite = m.invited_at
    ? `Invite sent to ${esc(m.invite_email)} <span class="text-muted">${esc(whenLocal(m.invited_at))}</span>`
    : (connected && m.event_uid ? link('Send the client an invite…', `sendMeetingInvite(${d.id}, '${pid}')`) : '');
  const stale = d.pipeline_stage !== MEETING_STAGE
    ? `<div class="text-muted text-small">The stage isn't "Meeting booked" any more, but the meeting is still on. Cancel it below if it's off.</div>` : '';
  return `<div class="meeting-box">
      <div><b>📅 ${esc(meetingWhenText(m))}</b></div>
      <div>${cal}</div>
      ${m.join_url ? `<div><a href="${esc(m.join_url)}" target="_blank" rel="noopener" style="color:var(--blue)">Join link ↗</a></div>` : ''}
      ${invite ? `<div>${invite}</div>` : ''}
      ${stale}
      <div>${link('<span style="color:var(--red)">Cancel meeting…</span>', `cancelMeeting(${d.id}, '${pid}')`)}</div>
    </div>`;
}

async function retryMeetingSync(businessId, panelId) {
  const res = await api(`/api/businesses/${businessId}/meeting/sync`, 'POST');
  if (!res || !res.ok) toast((res && res.error) || "Couldn't reach Zoho", 'err');
  else toast('On your calendar');
  _leadChanged(panelId);
}

async function sendMeetingInvite(businessId, panelId) {
  const d = await api(`/api/businesses/${businessId}`);
  if (!d || d.error || !d.meeting) return;
  const leadEmails = (d.emails || []).filter(e => e.status !== 'deleted').map(e => e.email);
  const email = d.meeting.invite_email || leadEmails[0] || '';
  const asked = chooseDialog({
    title: `Invite ${d.company || 'them'} to the meeting`,
    body: `<div class="text-small" style="margin-bottom:6px">📅 ${esc(meetingWhenText(d.meeting))}</div>
      <label class="field-label">Client's email</label>
      <input id="mi-email" class="soft-input" type="email" value="${esc(email)}" data-business="${d.id}"
             data-known="${esc(JSON.stringify(leadEmails))}"
             oninput="_meetingEmailChanged(this, 'mi-email-warn', 'mi-save-line')" />
      <div id="mi-email-warn" class="form-hint" style="color:var(--amber)"></div>
      <label class="check-line" id="mi-save-line" style="display:${email && !leadEmails.includes(email) ? 'flex' : 'none'}">
        <input type="checkbox" id="mi-save" ${leadEmails.length ? '' : 'checked'} />
        Also save it to this lead (it isn't put in any email campaign)</label>
      <div class="form-hint">Zoho emails them the invite from your Zoho address.</div>`,
    confirm: 'Send invite',
    collect: () => {
      const v = document.getElementById('mi-email').value.trim();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v)) { toast("That email doesn't look right", 'err'); return null; }
      return { email: v, save_email: document.getElementById('mi-save').checked
               && document.getElementById('mi-save-line').style.display !== 'none' };
    },
  });
  if (email) _meetingEmailChanged(document.getElementById('mi-email'), 'mi-email-warn', 'mi-save-line');
  const values = await asked;
  if (!values) return;
  const res = await api(`/api/businesses/${businessId}/meeting/invite`, 'POST', values);
  if (!res || !res.ok) { toast((res && res.error) || "Couldn't send the invite", 'err'); return; }
  toast(`Invite sent to ${values.email}${res.email_error ? ` (the address wasn't saved: ${res.email_error})` : ''}`);
  _leadChanged(panelId);
}

async function cancelMeeting(businessId, panelId) {
  const d = await api(`/api/businesses/${businessId}`);
  if (!d || d.error || !d.meeting) return;
  const m = d.meeting;
  const values = await chooseDialog({
    title: 'Cancel this meeting?',
    danger: true,
    body: `<div class="text-small" style="margin-bottom:10px">${esc(d.company || 'This lead')} · 📅 ${esc(meetingWhenText(m))}</div>
      ${m.event_uid ? `<div class="text-small">It comes off ${esc(m.calendar_name || 'the calendar')}.</div>` : ''}
      ${m.invited_at ? `<label class="check-line"><input type="checkbox" id="mc-notify" checked />
        Email ${esc(m.invite_email)} that it's cancelled</label>` : ''}
      ${d.pipeline_stage === MEETING_STAGE ? `<label class="check-line"><input type="checkbox" id="mc-clear" checked />
        Also take them off "Meeting booked"</label>` : ''}`,
    confirm: 'Cancel meeting',
    collect: () => ({
      notify: !!(document.getElementById('mc-notify') || {}).checked,
      clear_stage: !!(document.getElementById('mc-clear') || {}).checked,
    }),
  });
  if (!values) return;
  const res = await api(`/api/businesses/${businessId}/meeting/cancel`, 'POST', values);
  if (!res || !res.ok) { toast((res && res.error) || "Couldn't cancel it", 'err'); return; }
  toast(values.notify ? `Cancelled — ${m.invite_email} has been told` : 'Meeting cancelled');
  _leadChanged(panelId);
}

// Mirrors db.WA_MAX_LEAD_FOLLOWUPS — not a limit anyone should meet, just the
// ceiling on how far into the sequence a lead's own wording can go. Here
// rather than in whatsapp.js because the side panel renders the same messages
// and this file loads first.
const WA_MAX_LEAD_FOLLOWUPS = 50;

// Where follow-up `i` (0-based; default the next one out) comes from — the
// same rule as db.wa_message_for: the lead's own wording for that position;
// inside the lead's own run, a gap takes the campaign's follow-up at that
// position; after it, the campaign's list from the top, round and round.
function waFollowupWhere(l, i = l.followup_count || 0) {
  let drafts = [];
  try { drafts = JSON.parse(l.draft_followups || '[]'); } catch (e) { drafts = []; }
  if (!Array.isArray(drafts)) drafts = [];
  if (drafts[i]) return { own: true, text: "this lead's own wording" };
  const own = l.own_followups || 0;
  const steps = Math.max(1, l.campaign_followup_steps || 1);
  const pos = i < own ? i : i - own;
  return { own: false, step: pos % steps + 1, steps,
           text: steps > 1 ? `the campaign's follow-up ${pos % steps + 1} of ${steps}` : "the campaign's follow-up" };
}

// wa_leads.message_source, said in the operator's words.
const WA_SOURCE_LABELS = {
  ai: 'Reworded by AI',
  import: 'From your import',
  manual: 'Edited by hand',
};

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
// Right under the business name on every channel's panel, because "where is
// this one up to" is the first thing you want to know before you write to
// them again — and the answer is the same whichever channel you came from.
// openLeadPanel's channel names don't all match db.PIPELINE_CHANNELS
// ('calling' vs 'call') -- a naming mismatch between the two features that
// predates this row. Setting a stage from the Calling panel with the
// untranslated name would silently store '' instead of 'call', which
// matters now that a channel-scoped Booked-style view can filter on it.
const _PANEL_TO_PIPE_CHANNEL = { calling: 'call', email: 'email', whatsapp: 'whatsapp' };

function pipelineRowHtml(d, opts) {
  const channel = _PANEL_TO_PIPE_CHANNEL[opts.channel] || '';
  const set = `setPipelineStage(${d.id}, '${channel}', () => refreshLeadPanel('${opts.panelId}'))`;
  if (!d.pipeline_stage) {
    return `<div style="margin-top:8px">
      <a class="text-small" style="color:var(--blue);cursor:pointer" onclick="${set}">+ Set a stage</a>
    </div>`;
  }
  const overdue = pipelineOverdue(d)
    ? ` <span style="color:var(--red)">overdue</span>` : '';
  return `<div class="flex items-center gap-2" style="margin-top:8px;flex-wrap:wrap">
      ${pipelinePill(d)}${overdue}
      <a class="text-small" style="color:var(--blue);cursor:pointer" onclick="${set}">Change</a>
    </div>`;
}

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
  const [d] = await Promise.all([api(`/api/businesses/${businessId}`), loadAuditLinks(),
                                 loadPipelineStages(), loadZohoStatus()]);
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
    ${pipelineRowHtml(d, opts)}
    ${meetingBoxHtml(d, opts)}
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
            ? (window._aiEnabled ? `<button class="btn btn-ghost btn-sm" onclick="rewordWaMessage(${w.id}, '${panelId}')">✨ Reword with AI</button>` : '')
            : `<button class="btn btn-ghost btn-sm" onclick="waMarkReplied(${w.id}, true)">They replied</button>
               <button class="btn btn-ghost btn-sm" onclick="waSetPaused([${w.id}], true)">Pause follow-ups</button>`}
        </div>`;
    const fuIndex = w.followup_count || 0;
    const ownFollowup = fuIndex < WA_MAX_LEAD_FOLLOWUPS;
    const where = waFollowupWhere(w, fuIndex);
    body = `${kind === 'followup' ? lastSent : ''}
      <span class="field-label">${kind === 'opener'
        ? `Message${w.template_variant ? ` · version ${esc(w.template_variant)}` : ''}`
        : `Follow-up ${fuIndex + 1} due · ${esc(where.text)}`}</span>
      <textarea id="wl-msg" class="soft-input" style="min-height:110px"
                ${kind === 'opener'
                  ? `onchange="saveWaMessage(${w.id}, this.value, '${panelId}')"`
                  : (ownFollowup ? `onchange="saveWaFollowup(${w.id}, ${fuIndex}, this.value)"` : '')}
                >${esc(kind === 'opener' ? (w.message || '') : (w.followup_message || ''))}</textarea>
      ${kind === 'opener' && w.message_edited ? `<div class="text-muted text-small">${
        WA_SOURCE_LABELS[w.message_source] || (w.paraphrased ? WA_SOURCE_LABELS.ai : WA_SOURCE_LABELS.manual)} ·
        <a style="color:var(--blue);cursor:pointer" onclick="resetWaMessage(${w.id}, '${panelId}')">Reset to template</a></div>` : ''}
      ${kind === 'followup' ? `<div class="text-muted text-small">${where.own
        ? 'Clear the box to go back to the campaign\'s follow-up.'
        : 'Edit it and this lead keeps your version for this follow-up.'}</div>` : ''}
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
