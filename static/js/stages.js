// ── Settings → Stages & outcomes ─────────────────────────────────────────────
//
// The two lists an operator picks from all day, in one place: deal stages
// (on the business, read the same from every channel) and call outcomes
// (Calling's dialler). One editor, two configs, the same rules for both:
//
//   - Hide takes something out of YOUR pickers. Anything already on it keeps
//     it, and it still reads by name. A built-in is hidden for you alone --
//     the other operator still sees it (hidden_choices). Your own is
//     archived, which nobody else could see anyway.
//   - Delete is only for your own, and a stage or outcome in use is archived
//     instead, so no lead loses where it's up to and no call loses its label.
//   - Built-ins are shared, so only an admin renames, recolours or re-orders
//     one, and nobody changes what one does (the code relies on 'booked',
//     'do_not_call' and the website stages behaving as they do).
//
// The call outcome editor used to live under Calling → Script; this replaced
// it. The stage picker's "Edit stages…" link opens this tab.

const CHOICE_KINDS = {
  stage: {
    url: '/api/pipeline-stages',
    title: 'Deal stages',
    noun: 'stage',
    placeholder: 'New stage, e.g. Demo done',
    hint: 'Where a lead is up to. One stage per business, shared by Email, Calling and WhatsApp, so a lead booked on a call reads as booked everywhere. Setting a stage stops that business’s WhatsApp follow-ups.',
    usedLabel: 'Leads on it',
    used: n => `${n} lead${n === 1 ? ' is' : 's are'} on this stage`,
    tones: [['neutral', 'Plain'], ['info', 'In progress'], ['amber', 'Waiting on you'], ['good', 'Good'], ['bad', 'Dead']],
    flagHeads: ['Asks for', 'Ends the deal'],
    flagCells: (r, editable) => {
      const asks = r.wants_time ? 'time' : r.wants_date ? 'date' : '';
      const asksText = { '': 'Nothing', date: 'A date', time: 'A date and time' }[asks];
      return [
        editable
          ? `<select class="filter-select" aria-label="What this stage asks for"
                onchange="choiceAsksFor('${esc(r.key)}', this.value)">
               ${[['', 'Nothing'], ['date', 'A date'], ['time', 'A date and time']].map(([v, l]) =>
                 `<option value="${v}" ${v === asks ? 'selected' : ''}>${l}</option>`).join('')}
             </select>`
          : `<span class="text-muted">${asksText}</span>`,
        _choiceFlag('stage', r, 'is_terminal', editable, 'Ends the deal: it drops off the board'),
      ];
    },
    newFlags: () => `
      <select id="ch-new-stage-asks" class="filter-select" aria-label="What it asks for">
        <option value="">Asks for nothing</option><option value="date">Asks for a date</option>
        <option value="time">Asks for a date and time</option>
      </select>
      <label class="ch-check"><input type="checkbox" id="ch-new-stage-terminal" /> Ends the deal</label>`,
    collectNew: () => {
      const asks = document.getElementById('ch-new-stage-asks').value;
      return { wants_date: !!asks, wants_time: asks === 'time',
               is_terminal: document.getElementById('ch-new-stage-terminal').checked };
    },
  },
  outcome: {
    url: '/api/call-outcomes',
    title: 'Call outcomes',
    noun: 'outcome',
    placeholder: 'New outcome, e.g. Follow up later',
    hint: 'What happened on a call. <strong>Needs a date</strong> makes the next-call date required — leave it off for something like “follow up later”, where nothing was agreed. <strong>Ends the lead</strong> takes it out of every call queue; <strong>Stops email</strong> also ends any email sequence they’re in.',
    usedLabel: 'Calls logged',
    used: n => `${n} call${n === 1 ? ' was' : 's were'} logged with this outcome`,
    tones: [['neutral', 'Plain'], ['info', 'In progress'], ['good', 'Good'], ['bad', 'Dead']],
    flagHeads: ['Needs a date', 'Ends the lead', 'Stops email'],
    flagCells: (r, editable) => [
      _choiceFlag('outcome', r, 'requires_date', editable, 'Needs a date'),
      _choiceFlag('outcome', r, 'is_terminal', editable, 'Ends the lead'),
      _choiceFlag('outcome', r, 'stops_email', editable, 'Stops email'),
    ],
    newFlags: () => `
      <label class="ch-check"><input type="checkbox" id="ch-new-outcome-date" /> Needs a date</label>
      <label class="ch-check"><input type="checkbox" id="ch-new-outcome-terminal" /> Ends the lead</label>
      <label class="ch-check"><input type="checkbox" id="ch-new-outcome-stops" /> Stops email</label>`,
    collectNew: () => ({
      requires_date: document.getElementById('ch-new-outcome-date').checked,
      is_terminal: document.getElementById('ch-new-outcome-terminal').checked,
      stops_email: document.getElementById('ch-new-outcome-stops').checked,
    }),
  },
};

const _CHOICE_PILL = { good: 'green', bad: 'red', info: 'blue', amber: 'amber' };
const _choiceRows = { stage: [], outcome: [] };
const _choiceShowHidden = { stage: false, outcome: false };

onTab('settings', name => { if (name === 'stages') renderChoiceEditors(); });

function _choiceAdmin() { return !!(typeof _settingsMe !== 'undefined' && _settingsMe && _settingsMe.is_admin); }

function renderChoiceEditors() {
  return Promise.all(['stage', 'outcome'].map(k => loadChoiceEditor(k)));
}

async function loadChoiceEditor(kind) {
  const rows = await api(`${CHOICE_KINDS[kind].url}?usage=1`);
  _choiceRows[kind] = Array.isArray(rows) ? rows : [];
  _drawChoiceEditor(kind);
}

function _choiceFlag(kind, r, field, editable, label) {
  if (!editable) return r[field] ? icon('check', { size: 14, label: 'Yes' }) : '<span class="text-muted">—</span>';
  return `<input type="checkbox" aria-label="${esc(label)}" ${r[field] ? 'checked' : ''}
            onchange="choicePatch('${kind}', '${esc(r.key)}', { ${field}: this.checked })" />`;
}

function _drawChoiceEditor(kind) {
  const cfg = CHOICE_KINDS[kind];
  const el = document.getElementById(`choice-editor-${kind}`);
  if (!el) return;
  const admin = _choiceAdmin();
  const all = _choiceRows[kind];
  const hiddenCount = all.filter(r => r.archived).length;
  const shown = _choiceShowHidden[kind] ? all : all.filter(r => !r.archived);
  const visible = all.filter(r => !r.archived);

  const row = r => {
    const own = !r.is_builtin;
    const canEdit = own || admin;          // name, colour, order
    const vi = visible.indexOf(r);
    const canUp = canEdit && !r.archived && vi > 0;
    const canDown = canEdit && !r.archived && vi > -1 && vi < visible.length - 1;
    const tags = [
      r.is_builtin ? pill('built-in', 'dashed', 'Shared by everyone on this install') : '',
      r.system ? pill('set by the website', 'dashed', 'Not offered when you set a stage by hand') : '',
      r.archived ? pill('hidden', '', 'Not in your pickers') : '',
    ].filter(Boolean).join(' ');
    const tone = (cfg.tones.find(([v]) => v === r.tone) || cfg.tones[0]);
    return `<tr class="${r.archived ? 'ch-hidden' : ''}">
      <td class="nowrap">
        <button class="btn btn-ghost btn-sm ch-move" ${canUp ? '' : 'disabled'} aria-label="Move up" title="Move up"
                onclick="moveChoice('${kind}', '${esc(r.key)}', -1)">${icon('chevron-up', { size: 14 })}</button>
        <button class="btn btn-ghost btn-sm ch-move" ${canDown ? '' : 'disabled'} aria-label="Move down" title="Move down"
                onclick="moveChoice('${kind}', '${esc(r.key)}', 1)">${icon('chevron-down', { size: 14 })}</button>
      </td>
      <td>${canEdit
          ? `<input class="soft-input ch-name" value="${esc(r.label)}" aria-label="Name" maxlength="60"
                    onchange="renameChoice('${kind}', '${esc(r.key)}', this)" />`
          : `<span class="biz-name">${esc(r.label)}</span>`}
        ${tags ? `<div class="ch-tags">${tags}</div>` : ''}</td>
      <td>${canEdit
          ? `<select class="filter-select" aria-label="Colour" onchange="choicePatch('${kind}', '${esc(r.key)}', { tone: this.value })">
               ${cfg.tones.map(([v, l]) => `<option value="${v}" ${v === r.tone ? 'selected' : ''}>${l}</option>`).join('')}
             </select>`
          : pill(tone[1], _CHOICE_PILL[tone[0]] || '')}</td>
      ${cfg.flagCells(r, own).map(c => `<td>${c}</td>`).join('')}
      <td class="num tnum">${r.in_use || 0}</td>
      <td class="nowrap">
        <button class="btn btn-ghost btn-sm" onclick="setChoiceHidden('${kind}', '${esc(r.key)}', ${!r.archived})"
                title="${r.archived ? 'Offer it in your pickers again' : 'Take it out of your pickers. Anything on it keeps it'}">${r.archived ? 'Show' : 'Hide'}</button>
        ${own ? `<button class="btn btn-ghost btn-sm" onclick="deleteChoice('${kind}', '${esc(r.key)}')">Delete</button>` : ''}
      </td>
    </tr>`;
  };

  el.innerHTML = `
    <div class="card-title" style="margin-bottom:4px">${cfg.title}</div>
    <div class="form-hint" style="margin-bottom:6px">${cfg.hint}</div>
    <div class="form-hint" style="margin-bottom:14px">Hiding one takes it out of your own pickers only: anything already on it keeps it, and the other operator still sees it.${
      admin ? ' As an admin you can rename, recolour and re-order the built-in ones, which changes them for everyone.'
            : ' Built-in ones are shared, so only an admin can rename them.'}</div>
    <div class="table-wrap">
      <table class="lead-table ch-table">
        <thead><tr><th style="width:84px">Order</th><th>Name</th><th>Colour</th>
          ${cfg.flagHeads.map(h => `<th>${h}</th>`).join('')}<th class="num">${cfg.usedLabel}</th><th></th></tr></thead>
        <tbody>${shown.length ? shown.map(row).join('')
          : `<tr><td colspan="${5 + cfg.flagHeads.length}"><div class="empty-state"><p>Everything is hidden.</p></div></td></tr>`}</tbody>
      </table>
    </div>
    ${hiddenCount ? `<div class="text-small text-muted" style="margin-top:8px">${hiddenCount} hidden ·
        <a style="color:var(--accent);cursor:pointer" onclick="toggleHiddenChoices('${kind}')">${_choiceShowHidden[kind] ? 'Leave them out' : 'Show them'}</a></div>` : ''}
    <div class="ch-new">
      <input id="ch-new-${kind}-label" class="soft-input" placeholder="${esc(cfg.placeholder)}" maxlength="60"
             aria-label="New ${cfg.noun}" onkeydown="if (event.key === 'Enter') addChoice('${kind}')" />
      <select id="ch-new-${kind}-tone" class="filter-select" aria-label="Colour">
        ${cfg.tones.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}
      </select>
      ${cfg.newFlags()}
      <button class="btn btn-primary btn-sm" onclick="addChoice('${kind}')">Add ${cfg.noun}</button>
    </div>`;
}

function toggleHiddenChoices(kind) {
  _choiceShowHidden[kind] = !_choiceShowHidden[kind];
  _drawChoiceEditor(kind);
}

// Anything else on the page that cached the list reads it fresh next time.
function _choicesChanged(kind) {
  if (kind === 'stage') loadPipelineStages(true);
  else {
    const sel = document.getElementById('cl-outcome');
    if (sel) sel.innerHTML = '<option value="">Any outcome</option>';
  }
}

async function choicePatch(kind, key, body) {
  const res = await api(`${CHOICE_KINDS[kind].url}/${encodeURIComponent(key)}`, 'PATCH', body);
  if (!res || res.error) toast((res && res.error) || 'Could not save that', 'err');
  await loadChoiceEditor(kind);
  _choicesChanged(kind);
}

function renameChoice(kind, key, input) {
  const label = input.value.trim();
  const r = _choiceRows[kind].find(x => x.key === key);
  if (!label) { toast('Give it a name', 'err'); if (r) input.value = r.label; return; }
  if (r && label === r.label) return;
  choicePatch(kind, key, { label });
}

function choiceAsksFor(key, value) {
  choicePatch('stage', key, { wants_date: !!value, wants_time: value === 'time' });
}

// Moves one row past its visible neighbour by giving it a sort position
// between that neighbour and the one beyond. Only the moved row changes, so
// moving your own stage never re-orders the shared built-ins around it.
async function moveChoice(kind, key, dir) {
  const all = _choiceRows[kind];
  const visible = all.filter(r => !r.archived);
  const vi = visible.findIndex(r => r.key === key);
  const past = visible[vi + dir];
  if (vi < 0 || !past) return;
  const pi = all.indexOf(past);
  const beyond = all[pi + dir];
  const at = +past.sort_order;
  let to = beyond ? (at + +beyond.sort_order) / 2 : at + dir * 10;
  if (to === at) to = at + dir * 0.5;   // two rows on one position: step off it
  await choicePatch(kind, key, { sort_order: to });
}

async function setChoiceHidden(kind, key, hidden) {
  const res = await api(`${CHOICE_KINDS[kind].url}/${encodeURIComponent(key)}/hidden`, 'POST', { hidden });
  if (!res || res.error) { toast((res && res.error) || 'Could not change that', 'err'); return; }
  toast(hidden ? 'Hidden from your pickers. Anything on it keeps it' : 'Back in your pickers');
  await loadChoiceEditor(kind);
  _choicesChanged(kind);
}

async function deleteChoice(kind, key) {
  const cfg = CHOICE_KINDS[kind];
  const r = _choiceRows[kind].find(x => x.key === key);
  if (!r) return;
  const n = r.in_use || 0;
  const ok = await confirmDialog(n ? {
    title: `Archive “${r.label}”?`,
    text: `${cfg.used(n)}, so it can’t be deleted without losing that. It will be archived instead: everything on it keeps it, and it won’t be offered any more.\n\nYou can show it again from the hidden list.`,
    confirm: 'Archive',
  } : {
    title: `Delete “${r.label}”?`,
    text: `Nothing is on this ${cfg.noun}, so it will be deleted for good.`,
    confirm: 'Delete', danger: true,
  });
  if (!ok) return;
  const res = await api(`${cfg.url}/${encodeURIComponent(key)}`, 'DELETE');
  if (!res || res.error) { toast((res && res.error) || 'Could not remove it', 'err'); return; }
  toast(res.result === 'archived' ? 'Archived. Everything on it keeps it' : 'Deleted');
  await loadChoiceEditor(kind);
  _choicesChanged(kind);
}

async function addChoice(kind) {
  const cfg = CHOICE_KINDS[kind];
  const input = document.getElementById(`ch-new-${kind}-label`);
  const label = input.value.trim();
  if (!label) { toast('Give it a name', 'err'); input.focus(); return; }
  const res = await api(cfg.url, 'POST', {
    label, tone: document.getElementById(`ch-new-${kind}-tone`).value, ...cfg.collectNew(),
  });
  if (!res || res.error) { toast((res && res.error) || 'Could not add it', 'err'); return; }
  toast(`Added “${label}”`);
  await loadChoiceEditor(kind);
  _choicesChanged(kind);
  document.getElementById(`ch-new-${kind}-label`)?.focus();
}

// From the "Set a stage" picker: straight to this tab.
function openStageEditor() {
  chooseCancel();
  openSettingsTab('stages');
}
