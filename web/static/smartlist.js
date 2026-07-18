/* Attune auto-playlists — the rules editor + saved-list navigation.
   Backend: smartlists.py (/api/smartlist/*). Globals from studio.js: S, $, esc, jget,
   jpost, toast, renderRows, hms, fmt, markTree, currentExportIds; Player from player.js. */
'use strict';

const Smart = (() => {
  // field metadata mirrors smartlists.py FIELDS; ops are (value, label) per kind.
  const OPS = {
    num:  [['gte','is at least'],['lte','is at most'],['eq','is'],['ne','is not'],
           ['gt','is over'],['lt','is under'],['between','is between']],
    bool: [['is','is']],
    date: [['in_last_days','in the last'],['not_in_last_days','not in the last'],
           ['before_days','is older than']],
    text: [['contains','contains'],['not_contains','does not contain'],['is','is'],
           ['is_not','is not'],['starts','starts with'],['ends','ends with']],
    tags: [['contains','contains'],['is','is'],['not_contains','does not contain']],
  };
  const FIELDS = [
    ['rating','Rating','num'], ['plays','Play count','num'], ['loved','Loved','bool'],
    ['genre','Genre','tags'], ['artist','Artist','text'], ['album','Album','text'],
    ['title','Title','text'], ['year','Year','num'], ['length','Length (sec)','num'],
    ['added','Date added','date'], ['last_played','Last played','date'],
    ['analyzed','Analyzed','bool'],
  ];
  const FKIND = Object.fromEntries(FIELDS.map(f => [f[0], f[2]]));
  let editId = null;

  /* ---------------------------------------------------------------- rule rows */
  function ruleRow(cond = {}) {
    const field = cond.field || 'rating';
    const kind = FKIND[field] || 'num';
    const li = document.createElement('div');
    li.className = 'slrule';
    li.innerHTML =
      `<select class="slf">${FIELDS.map(f =>
        `<option value="${f[0]}" ${f[0] === field ? 'selected' : ''}>${f[1]}</option>`).join('')}</select>
       <select class="slo"></select>
       <span class="slv"></span>
       <button class="slx" title="Remove rule">✕</button>`;
    const foSel = li.querySelector('.slf');
    foSel.addEventListener('change', () => { paintOps(li, foSel.value); paintVal(li); });
    li.querySelector('.slo').addEventListener('change', () => paintVal(li));
    li.querySelector('.slx').addEventListener('click', () => { li.remove(); preview(); });
    paintOps(li, field, cond.op);
    paintVal(li, cond.value);
    return li;
  }
  function paintOps(li, field, op) {
    const kind = FKIND[field] || 'num';
    li.querySelector('.slo').innerHTML = OPS[kind].map(o =>
      `<option value="${o[0]}" ${o[0] === op ? 'selected' : ''}>${o[1]}</option>`).join('');
  }
  function paintVal(li, value) {
    const field = li.querySelector('.slf').value;
    const op = li.querySelector('.slo').value;
    const kind = FKIND[field] || 'num';
    const v = li.querySelector('.slv');
    if (kind === 'bool') {
      v.innerHTML = `<select class="slval"><option value="true">true</option>
        <option value="false">false</option></select>`;
      if (value === false) v.querySelector('select').value = 'false';
    } else if (op === 'between') {
      const a = Array.isArray(value) ? value[0] : '', b = Array.isArray(value) ? value[1] : '';
      v.innerHTML = `<input class="slval slval-a" type="number" value="${a}" placeholder="min">
        <span class="sldash">–</span><input class="slval slval-b" type="number" value="${b}" placeholder="max">`;
    } else if (kind === 'date') {
      v.innerHTML = `<input class="slval" type="number" min="0" value="${value ?? 30}" style="width:70px"> <span class="hint">days</span>`;
    } else if (kind === 'num') {
      v.innerHTML = `<input class="slval" type="number" value="${value ?? ''}" placeholder="value">`;
    } else {
      v.innerHTML = `<input class="slval" type="text" value="${esc(value ?? '')}" placeholder="text">`;
    }
    v.querySelectorAll('.slval').forEach(el => el.addEventListener('input', () => debouncePreview()));
  }

  function collectRules() {
    const conditions = [...$('slRules').children].map(li => {
      const field = li.querySelector('.slf').value;
      const op = li.querySelector('.slo').value;
      const kind = FKIND[field] || 'num';
      let value;
      if (op === 'between') {
        value = [ +li.querySelector('.slval-a').value || 0, +li.querySelector('.slval-b').value || 0 ];
      } else if (kind === 'bool') {
        value = li.querySelector('.slval').value === 'true';
      } else if (kind === 'num' || kind === 'date') {
        value = +li.querySelector('.slval').value || 0;
      } else {
        value = li.querySelector('.slval').value;
      }
      return { field, op, value };
    });
    const rules = { match: $('slMatch').value, conditions };
    if ($('slSort').value) { rules.sort = $('slSort').value; if ($('slDesc').value) rules.desc = true; }
    const lim = +$('slLimit').value || 0; if (lim > 0) rules.limit = lim;
    return rules;
  }

  /* ---------------------------------------------------------------- editor modal */
  function openEditor(existing) {
    editId = existing ? existing.id : null;
    $('slEditId').textContent = existing ? `#${existing.id}` : 'new';
    $('slName').value = existing ? existing.name : '';
    $('slMsg').textContent = ''; $('slPreview').textContent = '';
    $('slDelete').hidden = !existing;
    const r = existing ? existing.rules : { match: 'all', conditions: [{ field: 'rating', op: 'gte', value: 4 }] };
    $('slMatch').value = r.match || 'all';
    $('slSort').value = r.sort || '';
    $('slDesc').value = r.desc ? '1' : '';
    $('slLimit').value = r.limit || 0;
    $('slRules').innerHTML = '';
    (r.conditions && r.conditions.length ? r.conditions : [{}]).forEach(c =>
      $('slRules').appendChild(ruleRow(c)));
    $('slWrap').hidden = false;
    preview();
  }

  let pvTimer = 0;
  function debouncePreview() { clearTimeout(pvTimer); pvTimer = setTimeout(preview, 350); }
  async function preview() {
    try {
      const j = await jpost('/api/smartlist/preview', { rules: collectRules() });
      $('slPreview').textContent = `Matches ${fmt(j.total)} track${j.total === 1 ? '' : 's'}`
        + (j.rows[0] ? ` — e.g. ${j.rows[0].artist} — ${j.rows[0].title}` : '');
    } catch (e) { $('slPreview').textContent = '⚠ ' + e.message; }
  }
  async function save() {
    const name = $('slName').value.trim();
    if (!name) { $('slMsg').className = 'msg err'; $('slMsg').textContent = 'Name required'; return; }
    try {
      const j = await jpost('/api/smartlist/save',
        { id: editId || undefined, name, rules: collectRules() });
      editId = j.id;
      $('slMsg').className = 'msg ok'; $('slMsg').textContent = 'Saved.';
      await loadList();
      setTimeout(() => { $('slWrap').hidden = true; }, 500);
    } catch (e) { $('slMsg').className = 'msg err'; $('slMsg').textContent = e.message; }
  }
  async function del() {
    if (!editId) return;
    try {
      await jpost('/api/smartlist/delete', { id: editId });
      $('slWrap').hidden = true;
      await loadList();
      if (S.view === 'smartlist' && S._slId === editId) loadLibrary(true);
    } catch (e) { $('slMsg').className = 'msg err'; $('slMsg').textContent = e.message; }
  }

  /* ---------------------------------------------------------------- tree + open */
  async function loadList() {
    try {
      const j = await jget('/api/smartlist/list');
      S._smartlists = j.smartlists;
      $('slList').innerHTML = j.smartlists.map(s =>
        `<li data-slid="${s.id}" title="${esc(s.name)}"><span class="ti">◈</span>${esc(s.name)}
           <span class="sledit" data-slid="${s.id}" title="Edit rules">✎</span></li>`).join('')
        || '<li class="empty-tree hint">No auto-playlists yet</li>';
    } catch (e) { /* leave as-is */ }
  }

  async function open(id) {
    S.view = 'smartlist'; S._slId = id; S.smart = ''; S.folder = null;
    document.querySelectorAll('#tree li, #plList li, #folderTree li').forEach(l => l.classList.remove('on'));
    document.querySelectorAll('#slList li').forEach(l =>
      l.classList.toggle('on', +l.dataset.slid === id));
    $('btnBackLib').hidden = false; $('pager').innerHTML = ''; $('queueTools').hidden = true;
    try {
      const j = await jget('/api/smartlist/open?id=' + id);
      S._slIds = j.ids || [];
      renderRows(j.rows);
      $('viewLabel').textContent = j.name;
      $('viewSub').textContent = `${fmt(j.total)} tracks (auto)` +
        (j.total > j.rows.length ? ` · showing first ${j.rows.length}` : '');
    } catch (e) { toast(e.message, true); }
  }

  function playAll() {
    if (S.view === 'smartlist' && S._slIds && S._slIds.length) {
      Player.playList(S._slIds, 0); return true;
    }
    return false;
  }

  /* ---------------------------------------------------------------- wiring */
  function init() {
    $('slNew').addEventListener('click', e => { e.stopPropagation(); openEditor(null); });
    $('slAddRule').onclick = () => { $('slRules').appendChild(ruleRow({})); preview(); };
    $('slMatch').onchange = preview;
    $('slSort').onchange = preview; $('slDesc').onchange = preview;
    $('slLimit').oninput = debouncePreview;
    $('slPreviewBtn').onclick = preview;
    $('slSave').onclick = save;
    $('slDelete').onclick = del;
    $('slName').addEventListener('keydown', e => { if (e.key === 'Enter') save(); });
    $('slList').addEventListener('click', e => {
      const edit = e.target.closest('.sledit');
      if (edit) {
        e.stopPropagation();
        const s = (S._smartlists || []).find(x => x.id === +edit.dataset.slid);
        if (s) openEditor(s);
        return;
      }
      const li = e.target.closest('li[data-slid]');
      if (li) open(+li.dataset.slid);
    });
    loadList();
  }

  return { init, open, playAll, loadList, openEditor };
})();
