// ── Meetings (Plan 2 package 5) ──────────────────────────────────────────────
//
// Every meeting this operator has booked, from any channel or the website, in
// one table grouped Needs an outcome / Today / Tomorrow / Later. A meeting's
// time is the wall-clock time stored on the business (next_action_at), read
// exactly as stored -- Handover §5c. "Needs an outcome" is a meeting whose
// time has passed while the stage still says booked: the call happened (or
// didn't) and nobody has said how it went.

const _MT_GROUPS = ['Needs an outcome', 'Today', 'Tomorrow', 'Later', 'No time set'];

function _meetingGroup(m) {
  if (!m.starts_at) return 'No time set';
  const p = _parseWhen(m.starts_at, 'wall');
  if (!p) return 'No time set';
  const now = new Date();
  if (p.d < now && (p.hasTime || p.d.toDateString() !== now.toDateString())) return 'Needs an outcome';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(p.d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((day - today) / 864e5);
  return diff <= 0 ? 'Today' : diff === 1 ? 'Tomorrow' : 'Later';
}

const _FROM_LABELS = { whatsapp: 'from WhatsApp', call: 'from a call', email: 'from email' };

function meetingFrom(m) {
  if (m.pipeline_channel === 'website') return m.site ? `booked on ${m.site}` : 'booked on the website';
  return _FROM_LABELS[m.pipeline_channel] || 'set by hand';
}

function meetingState(m) {
  if (m.meeting_state === 'cancelled') return pill('cancelled', 'red', 'Cancelled on Cal.com. The stage is yours to change.');
  if (m.meeting_state === 'moved') return pill('moved', 'amber', 'Moved on Cal.com');
  return '';
}

async function refreshMeetingCount() {
  const r = await api('/api/meetings/count');
  const el = document.getElementById('nav-count-meetings');
  if (el && r && typeof r.count === 'number') el.textContent = r.count ? r.count : '';
}

async function loadMeetings() {
  const rows = await api('/api/meetings');
  const body = document.getElementById('mt-body');
  if (!Array.isArray(rows)) {
    body.innerHTML = `<tr><td colspan="5"><div class="empty-state"><p>${esc((rows && rows.error) || 'Could not load meetings')}</p></div></td></tr>`;
    return;
  }
  const el = document.getElementById('nav-count-meetings');
  const live = rows.filter(m => m.meeting_state !== 'cancelled').length;
  if (el) el.textContent = live ? live : '';
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="5"><div class="empty-state"><p>No meetings booked. Bookings made on
      the website's calendar land here by themselves; for anyone else, use <b>Copy booking link</b> on their
      lead, or set its stage to Meeting booked.</p></div></td></tr>`;
    return;
  }
  const groups = {};
  rows.forEach(m => (groups[_meetingGroup(m)] = groups[_meetingGroup(m)] || []).push(m));
  body.innerHTML = _MT_GROUPS.filter(g => groups[g]).map(g => `
    <tr class="mt-group"><td colspan="5">${esc(g)} <span class="text-muted">· ${groups[g].length}</span></td></tr>
    ${groups[g].map(m => {
      const when = m.starts_at ? fmtWhen(m.starts_at, 'wall') : 'No time set';
      const joinUrl = httpsOnly(m.join_url);
      const join = joinUrl && m.meeting_state !== 'cancelled'
        ? `<a class="btn btn-ghost btn-sm" href="${esc(joinUrl)}" target="_blank" rel="noopener"
             onclick="event.stopPropagation()">Join ↗</a>` : '';
      return `<tr class="clickable ${(LeadPanel.current['mt-panel'] || {}).businessId === m.business_id ? 'current' : ''}"
                  onclick="openMeetingLead(${m.business_id})">
        <td class="nowrap">${esc(when)}</td>
        <td><span class="biz-name">${esc(m.company || 'Unnamed business')}</span></td>
        <td class="text-muted nowrap">${esc(meetingFrom(m))}</td>
        <td>${meetingState(m)}</td>
        <td class="nowrap" style="text-align:right">${join}</td>
        <td class="m-card">${mCard(`<b>${esc(when)}</b> ${meetingState(m)}`,
          `<span class="biz-name">${esc(m.company || 'Unnamed business')}</span>`,
          esc(meetingFrom(m)))}</td>
        <td class="m-keep m-only">${join}</td>
      </tr>`;
    }).join('')}`).join('');
}

function openMeetingLead(businessId) {
  openLeadPanel(businessId, {
    panelId: 'mt-panel', splitId: 'mt-split', show: true,
    onClose: () => loadMeetings(),
  });
  loadMeetings();
}
