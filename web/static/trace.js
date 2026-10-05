/* Attune trace: what the engine did to make the list on screen, drawn from the server's
   own account. Every mix route returns a `trace` (web/app.py _trace(), 2026-10-05) built
   from the walk's report (src/hybrid.py _walk): each song's place in the ranking and its
   fit, every song the walk reached with what happened to it (the field), how each song
   leads into the next, the loudness of each song on the list, and which ingredient
   carried the list. Nothing here is written by a model or guessed by this script; what
   the server did not record is not shown.

   On screen, in the Mix view (and the Now Playing view while Radio is on):
     the funnel   the numbers above the list, in the order the code ran: in the library,
                  to choose from, looked at, taken, fit the line, on the list. The one that
                  decided where the list ended is lit. Click a number and the list becomes
                  that population, with a column saying what happened to each song
                  (TODO.md row 58, Joe's "show me the actual field Attune was choosing from")
     Fit column   each song's fit, a meter from the line to a perfect twin and the number
     List / Arc   the list as a table, or as an arc: loudness along the sequence, Heat under
                  it, the closeness of each hand-off as the ground, the weakest in the accent
     the drawer   under the list, closed by default (the Trace button, or T): how it was
                  built, what changed since the last step, the runs this session compared
                  two at a time, and "why isn't this song here?"
   Why this pick (right-click, or click a Fit meter) is the one-song view of the same data.
   The MusicIP engine sends no trace: Attune cannot see inside it and says nothing. */
'use strict';

const Trace = (() => {
  const WHY = {
    kept: 'on the list', 'kept-past': 'on the list, taken past the line',
    carried: 'taken past the line, not needed', count: 'taken, then cut for the count',
    artist: 'passed over: same artist within 3 songs',
    recording: 'passed over: the same recording is already on the list',
    variety: "passed over by Radio's variety coin", energy: "passed over by Radio's energy corridor",
    line: 'below the fit line', next: 'next in rank, beyond where the walk stopped',
    removed: 'removed by you', blocked: 'its artist is blocked',
    duplicate: 'a duplicate of a song on the list',
    'near-twin': 'set aside for variety: sounds almost the same as a song kept',
  };
  const SC = {                          // the colour class of a status in the field
    kept: 'kept', 'kept-past': 'kept', carried: 'car', count: 'car', next: 'car', line: 'car',
    artist: 'drop', recording: 'drop', variety: 'drop', energy: 'drop',
    removed: 'drop', blocked: 'drop', duplicate: 'drop', 'near-twin': 'drop',
  };
  const TERM = {
    clap: 'Sound fingerprint', lib: 'Timbre', genre: 'Genre tags', bpm: 'Tempo', era: 'Year',
    key: 'Key', artist: 'Artist links', earfeel: 'Earfeel',
  };
  const TAKEN = new Set(['kept', 'kept-past', 'carried', 'count', 'duplicate', 'near-twin', 'removed', 'blocked']);
  const FIT = new Set(['kept', 'kept-past', 'carried']);
  const labels = new Map();            // pool index -> "Artist — Title", from rows painted
  let tab = store.get('traceTab', 'built');
  if (tab === 'left' || tab === 'path') tab = 'built';
  let open = store.get('traceOpen', false);
  let showing = 'list';                // which population the list area shows
  let view = 'list';                   // 'list' | 'arc'
  let prevLabels = new Map();          // labels of the list before the last change
  const runs = [];                     // this session's runs, oldest first, 20 deep
  let lastRun = null;                  // the trace object the last run was made from
  let ra = 0, rb = 0;                  // the two runs compared
  let whyq = '', whyAns = [], whyBusy = false;

  const tr = () => {
    if (typeof S === 'undefined') return null;
    if (S.view === 'mix') return S.trace;
    if (S.view === 'nowplaying' && typeof Player !== 'undefined' && Player.radio) return Player.radioTrace;
    return null;
  };
  const song = i => { const t = tr(); return t && t.songs ? t.songs[i] : null; };
  const fmt2 = v => (v == null ? '' : Number(v).toFixed(2));
  const fmt3 = v => (v == null ? '' : Number(v).toFixed(3));
  const inMix = () => typeof S !== 'undefined' && S.view === 'mix';

  function noteRows(rows) {
    for (const r of rows || []) {
      if (r && r.i >= 0) labels.set(r.i, r.artist ? `${r.artist} — ${r.title}` : (r.title || `song ${r.i}`));
    }
  }
  function label(i) {
    if (labels.has(i)) return labels.get(i);
    const t = tr() || S.trace;
    const s = t && (t.seeds || []).find(x => x.i === i);
    if (s) return s.label;
    const l = t && (t.field || t.left_out || []).find(x => x.i === i);
    return l ? l.label : `song ${i}`;
  }

  /* ---- the fit meter: a bar from the line (or the weakest kept) to a perfect twin.
     The bar is relative so a list whose fits sit between 0.6 and 0.9 reads as a range and
     not as ten full bars; the number beside it is the exact fit. */
  function fitFloor(t) {
    t = t || tr(); if (!t) return 0;
    let lo = t.line != null ? t.line : 1;
    for (const s of Object.values(t.songs || {})) if (s.fit != null && s.fit < lo) lo = s.fit;
    if (lo >= 1) lo = 0;
    return Math.max(0, Math.min(0.95, lo - 0.04));
  }
  function meter(fit, past, i, tip) {
    const lo = fitFloor();
    const w = Math.max(0, Math.min(1, (fit - lo) / (1 - lo)));
    return `<span class="fitm${past ? ' past' : ''}"${i != null ? ` data-i="${i}"` : ''}${tip ? ` title="${esc(tip)}"` : ''}>` +
      `<i style="width:${Math.round(w * 100)}%"></i></span><b>${fmt2(fit)}</b>`;
  }
  function fitCell(i) {
    const s = song(i); if (!s || s.fit == null) return '';
    const t = tr();
    const tip = `Fit ${fmt3(s.fit)} of a perfect twin (1.000)` +
      (s.rank ? `, ranked ${s.rank.toLocaleString()} of ${t.pool.toLocaleString()}` : '') +
      (t.line != null ? `; the line is ${fmt2(t.line)}` : '') +
      (s.past ? '. Taken past the line so the list is not empty' : '') + '. Click for why.';
    return meter(s.fit, s.past, i, tip);
  }

  /* ---- the funnel on the header */
  function strip() {
    const el = $('mixStop'); if (!el) return;
    const t = tr();
    el.classList.toggle('tstrip', !!t);
    if (!t) return;                               // the server's sentence stays as it was
    el.classList.remove('short');
    const tiles = t.funnel && t.funnel.length ? t.funnel : null;
    if (!tiles) {                                 // a trace saved before the funnel existed
      el.innerHTML = (t.stages || []).map(s =>
        `<button type="button" class="tstage${s.on ? ' on' : ''}" data-stage="${s.id}" title="${esc(s.note || '')}">${esc(s.label)}</button>`
      ).join('<span class="tsep">›</span>');
      el.hidden = false; return;
    }
    const parts = [];
    const canOpen = inMix();
    tiles.forEach((s, k) => {
      if (k) parts.push(`<span class="fcon${s.on ? ' cut' : ''}" title="${esc('What changed here: ' + (s.note || ''))}"><svg viewBox="0 0 22 30" preserveAspectRatio="none"><path d="M0 3 L22 9 L22 21 L0 27 Z"/></svg></span>`);
      parts.push(`<button type="button" class="ftile${s.on ? ' on' : ''}${showing === s.id && canOpen ? ' showing' : ''}" data-pop="${s.id}" ` +
        `title="${esc((s.note || '') + (canOpen ? (s.id === 'lib' ? '. Click to open the Library' : s.id === 'list' ? '. Click to show the list' : '. Click to see these songs') : ''))}">` +
        `<span class="n">${s.n == null ? '' : Number(s.n).toLocaleString()}</span><span class="l">${esc(s.label)}</span></button>`);
    });
    el.innerHTML = parts.join('');
    el.hidden = false;
  }

  /* ---- the field: the list area becomes one population of the run */
  function population(t, pop) {
    const field = t.field || [];
    const name = t.collection || 'Full library';
    const note = id => ((t.funnel || []).find(x => x.id === id) || {}).note || '';
    if (pop === 'pool') return { rows: field.filter(r => r.status !== 'next'), head: `<b>${Number(t.pool).toLocaleString()} songs</b> scored against the target${name !== 'Full library' ? ` in ${esc(name)}` : ''}; the walk looked at these, best first`, delta: `the rest were never considered: the walk stopped at #${Number(t.reach_depth || 0).toLocaleString()}` };
    if (pop === 'reach') return { rows: field, head: `<b>${field.filter(r => r.status !== 'next').length} songs the walk looked at</b>, best first, then the ten it would have reached next`, delta: `every song ranked past #${Number(t.reach_depth || 0).toLocaleString()} was never considered` };
    if (pop === 'taken') return { rows: field.filter(r => TAKEN.has(r.status)), head: `<b>the songs the walk took</b>, in rank order`, delta: note('taken') };
    if (pop === 'fit') return { rows: field.filter(r => FIT.has(r.status)), head: `<b>the songs that fit the line ${fmt2(t.line)}</b>, then the ones carried past it`, delta: note('fit') };
    return null;
  }
  function paintField() {
    const fv = $('fieldView'); if (!fv) return;
    const t = tr();
    const show = inMix() && t && view === 'list' && showing !== 'list' && !!population(t, showing);
    fv.hidden = !show;
    const tbl = document.querySelector('#tbl');
    if (!show) { if (tbl && view === 'list' && inMix()) tbl.hidden = false; return; }
    if (tbl) tbl.hidden = true;
    const P = population(t, showing);
    const rows = P.rows.map(r => {
      const cls = r.status === 'kept-past' ? 'past' : (SC[r.status] === 'drop' ? 'drop' : (SC[r.status] === 'car' ? 'dim' : ''));
      const onList = r.status === 'kept' || r.status === 'kept-past';
      return `<tr data-i="${r.i}" class="${cls}${onList ? ' onlist' : ''}" title="${esc(r.label + '. Double-click to play; click the meter for why.')}">` +
        `<td class="c-pos">${r.rank ? '#' + Number(r.rank).toLocaleString() : ''}</td>` +
        `<td class="c-fit">${r.fit == null ? '' : meter(r.fit, !onList, r.i)}</td>` +
        `<td class="c-title">${esc(r.label)}</td>` +
        `<td class="c-status ${SC[r.status] || ''}"><span class="sc"></span>${esc(WHY[r.status] || r.status)}</td></tr>`;
    }).join('');
    fv.innerHTML = `<div class="showing-line"><span>Showing: ${P.head}</span><span class="delta">${esc(P.delta)}</span>` +
      `<button type="button" class="chip" data-pop="list" title="Back to the list as it plays">Back to the list</button></div>` +
      `<table class="fieldtbl"><thead><tr><th>Rank</th><th>Fit</th><th>Song</th><th>What happened to it</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  /* ---- the arc: the list as a sequence */
  function heatIndex() {
    const ef = (S.stats && S.stats.earfeel) || [];
    const k = ef.findIndex(f => f.id === 'heat');
    return k < 0 ? null : k;
  }
  function paintArc() {
    const av = $('arcView'); if (!av) return;
    const t = tr();
    const show = inMix() && t && view === 'arc' && (t.transitions || []).length >= 1;
    av.hidden = !show;
    const tbl = document.querySelector('#tbl');
    if (!show) return;
    if (tbl) tbl.hidden = true;
    const order = t.order || [];
    const byI = new Map((S.rows || []).map(r => [r.i, r]));
    const seq = order.map(i => byI.get(i) || { i, title: label(i), artist: '' });
    const n = seq.length, W = 1000, H = 300, padL = 40, padR = 16, top = 22, bot = 60;
    const x = k => padL + (W - padL - padR) * k / Math.max(1, n - 1);
    const en = order.map(i => (t.energy && t.energy[i] != null) ? t.energy[i] : null);
    const have = en.filter(v => v != null);
    const emin = have.length ? Math.min(...have) : -1, emax = have.length ? Math.max(...have) : 1;
    const ye = v => top + (H - top - bot) * (1 - (v - emin) / ((emax - emin) || 1));
    const hk = heatIndex();
    const heat = seq.map(r => (hk != null && r.earfeel) ? r.earfeel[hk] : null);
    const yh = v => top + (H - top - bot) * (1 - v / 100);
    const trs = t.transitions, tmin = Math.min(...trs), tmax = Math.max(...trs);
    let worst = 0; trs.forEach((v, k) => { if (v < trs[worst]) worst = k; });
    let svg = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;
    if (have.length) {
      svg += `<line x1="${padL}" y1="${ye(0)}" x2="${W - padR}" y2="${ye(0)}" class="zero"/>` +
        `<text x="${padL - 4}" y="${top + 4}" class="lab" text-anchor="end">loud</text>` +
        `<text x="${padL - 4}" y="${H - bot}" class="lab" text-anchor="end">quiet</text>`;
    }
    trs.forEach((v, k) => {
      const h = 6 + 24 * (v - tmin) / ((tmax - tmin) || 1), xa = x(k), xb = x(k + 1);
      svg += `<rect x="${xa}" y="${H - bot + 30 - h}" width="${Math.max(1, xb - xa)}" height="${h}" class="ho${k === worst ? ' worst' : ''}"><title>${esc(`#${k + 1} → #${k + 2}: hand-off ${fmt3(v)}`)}</title></rect>`;
    });
    const hp = heat.map((v, k) => v == null ? null : `${x(k)},${yh(v)}`).filter(Boolean).join(' ');
    if (hp) svg += `<polyline points="${hp}" class="heat"/>`;
    const ep = en.map((v, k) => v == null ? null : `${x(k)},${ye(v)}`).filter(Boolean).join(' ');
    if (ep) svg += `<polyline points="${ep}" class="energy"/>`;
    seq.forEach((r, k) => {
      if (en[k] == null) return;
      const sg = song(r.i);
      const tip = `#${k + 1} ${r.title}${r.artist ? ' — ' + r.artist : ''}\nenergy ${fmt2(en[k])}` +
        (sg && sg.fit != null ? ` · fit ${fmt3(sg.fit)}` : '') + (r.year ? ` · ${r.year}` : '') +
        (k ? `\nhand-off from #${k}: ${fmt3(trs[k - 1])}` : '');
      svg += `<circle cx="${x(k)}" cy="${ye(en[k])}" r="${k === 0 ? 5 : 3.5}" class="node${k === 0 ? ' seed' : ''}"><title>${esc(tip)}</title></circle>`;
    });
    svg += `<text x="${(x(worst) + x(worst + 1)) / 2}" y="${H - bot + 42}" class="lab on" text-anchor="middle">weakest hand-off #${worst + 1} → #${worst + 2} (${fmt3(trs[worst])})</text>`;
    svg += `<text x="${padL}" y="${H - 6}" class="lab">hand-off closeness between neighbours as the ground (taller = closer)</text></svg>`;
    let shape = '';
    if (have.length >= 3) {
      const first = en[0], last = en[n - 1];
      const peak = en.indexOf(emax), trough = en.indexOf(emin);
      const trend = (last != null && first != null) ? (last - first > 0.4 ? 'ends louder than it starts' : (first - last > 0.4 ? 'ends quieter than it starts' : 'ends about where it starts')) : '';
      const years = seq.map(r => r.year).filter(Boolean);
      shape = `<p class="shape">The shape: loudest at <b>#${peak + 1}</b> (${esc(seq[peak].title)}), quietest at <b>#${trough + 1}</b> (${esc(seq[trough].title)})${trend ? ', ' + trend : ''}.` +
        (years.length ? ` Years run ${Math.min(...years)} to ${Math.max(...years)}.` : '') +
        ` The weakest hand-off is <b>#${worst + 1} → #${worst + 2}</b>, ${esc(seq[worst].title)} into ${esc(seq[worst + 1].title)} at ${fmt3(trs[worst])}` +
        ((t.stages || []).some(s => s.id === 'flow') ? ', with Arrange for flow on.' : '; Arrange for flow (Mix options) would re-order by these hand-offs.') +
        ` Whether this arc sounds right is your ear's call; this only reports it.</p>`;
    }
    av.innerHTML = svg +
      `<div class="legend2"><span><i class="e"></i>energy (loudness, the axis Radio's arc steers by)</span>${hk != null ? '<span><i class="h"></i>Earfeel Heat, cool to fiery</span>' : ''}<span><i class="g"></i>hand-off closeness</span></div>` + shape;
  }

  /* ---- the drawer */
  function paint() {
    const t = tr();
    noteRows(S.rows);
    if (inMix() && t && t !== lastRun) {        // a new run arrived: remember it, show the list
      lastRun = t;
      noteRun(t);
      showing = 'list'; view = 'list';
    }
    if (!inMix() || !t) { view = 'list'; showing = 'list'; }
    strip();
    paintField();
    paintArc();
    const lt = $('lTrace');
    if (lt) { lt.hidden = !t; lt.classList.toggle('on', !!(t && open)); lt.textContent = (t && open) ? 'Hide trace' : 'Trace'; }
    for (const [id, which] of [['mixViewList', 'list'], ['mixViewArc', 'arc']]) {
      const b = $(id); if (!b) continue;
      b.hidden = !(inMix() && t && (t.transitions || []).length);
      b.classList.toggle('on', view === which);
    }
    const d = $('traceDrawer'); if (!d) return;
    d.hidden = !(t && open);
    if (!t || !open) return;
    const queue = S.view === 'nowplaying';
    for (const id of ['changes', 'runs', 'why']) { const b = d.querySelector(`[data-ttab="${id}"]`); if (b) b.hidden = queue; }
    if (queue && tab !== 'built') tab = 'built';
    for (const b of d.querySelectorAll('[data-ttab]')) b.classList.toggle('on', b.dataset.ttab === tab);
    const body = $('traceBody');
    body.innerHTML = tab === 'built' ? builtHtml(t) : tab === 'changes' ? changesHtml() : tab === 'runs' ? runsHtml() : whyHtml();
    if (tab === 'why') {
      const inp = $('whyQ');
      if (inp) inp.addEventListener('keydown', e => { if (e.key === 'Enter') { whyq = inp.value.trim(); askWhy(); } });
      const go = $('whyGo'); if (go) go.addEventListener('click', () => { whyq = (($('whyQ') || {}).value || '').trim(); askWhy(); });
    }
    if (tab === 'runs') body.querySelectorAll('.runlist li').forEach(li => li.addEventListener('click', e => {
      const k = +li.dataset.k; if (e.shiftKey) rb = k; else ra = k; paint();
    }));
  }

  function recipeHtml(t) {
    const r = t.recipe || {};
    if (r.sound_only) {
      return `<span class="tchip">Sound fingerprint alone</span>` +
        `<span class="hint">a Blend, an Adventure and steering compare sound only; the dials do not act on them</span>`;
    }
    const say = t.say || {};
    const keys = (r.terms || []).map(x => x.id).sort((a, b) => (say[b] || 0) - (say[a] || 0));
    const chips = (r.terms || []).map(x =>
      `<span class="tchip" title="${esc(x.label)}: weight ${x.weight}${say[x.id] != null ? `, ${(say[x.id] * 100).toFixed(0)}% of the say over this list` : ''}">${esc(TERM[x.id] || x.label)} <b>×${x.weight}</b>${say[x.id] != null ? `<em>${(say[x.id] * 100).toFixed(0)}%</em>` : ''}</span>`).join('');
    const scale = r.fusion === 'z' ? 'each ingredient as distance from its own library average, in units of its own spread'
      : r.fusion === 'rank' ? 'each ingredient as its percentile in the library'
      : 'the raw numbers added together';
    const prof = r.profile === 'sound' ? 'the sound profile' : 'the V2 recipe';
    const bar = keys.length && Object.keys(say).length ? `<div class="say">${keys.map(k => `<i style="width:${((say[k] || 0) * 100).toFixed(1)}%" title="${esc((TERM[k] || k) + ' ' + ((say[k] || 0) * 100).toFixed(0) + '% of the say')}"></i>`).join('')}</div>` : '';
    return bar + chips + `<span class="hint">${esc(prof)}; ${esc(scale)}${Object.keys(say).length ? '; the percentages are each ingredient\'s share of the say, averaged over the songs on the list' : ''}</span>`;
  }

  function builtHtml(t) {
    const tiles = (t.funnel && t.funnel.length) ? t.funnel.map(s =>
      `<li class="${s.on ? 'on' : ''}" data-pop="${s.id}"><b>${s.n == null ? '' : Number(s.n).toLocaleString() + ' '}${esc(s.label)}</b><span>${esc(s.note || '')}</span></li>`).join('')
      : (t.stages || []).map(s => `<li class="${s.on ? 'on' : ''}"><b>${esc(s.label)}</b><span>${esc(s.note || '')}</span></li>`).join('');
    let ladder = '';
    const field = (t.field || []).filter(r => r.fit != null && r.status !== 'next').slice(0, 400);
    if (field.length > 3) {
      const fmin = Math.min(...field.map(r => r.fit)), fmax = Math.max(...field.map(r => r.fit));
      const hb = v => 4 + 60 * (v - fmin) / ((fmax - fmin) || 1);
      const lineY = t.line != null ? 66 - hb(t.line) : null;
      ladder = `<div class="tsection"><div class="tk">The boundary: every song the walk looked at, by rank, its fit as height${t.line != null ? '; the line dashed' : ''}</div>` +
        `<div class="ladder">${lineY != null ? `<div class="line" style="top:${Math.max(0, lineY).toFixed(0)}px"><span>line ${fmt2(t.line)}</span></div>` : ''}` +
        field.map(r => `<i class="${SC[r.status] === 'kept' ? 'kept' : (r.status === 'kept-past' ? 'past' : SC[r.status] === 'drop' ? 'drop' : 'car')}" style="height:${hb(r.fit).toFixed(0)}px" title="${esc('#' + r.rank + ' ' + r.label + ' · fit ' + fmt3(r.fit) + ' · ' + (WHY[r.status] || r.status))}"></i>`).join('') +
        `</div><div class="saylab"><span><b class="k1">amber</b> on the list</span><span><b class="k2">grey</b> carried past the line or cut for the count</span><span><b class="k3">red</b> passed over</span></div></div>`;
    }
    let extra = '';
    if (t.kind === 'blend' && t.cohesion != null) {
      const c = [0, 0]; let seen = 0;
      for (const s of Object.values(t.songs || {})) { if (s.seeds && s.seeds.length >= 2) { c[s.seeds[0] >= s.seeds[1] ? 0 : 1]++; seen++; } }
      const names = (t.seeds || []).map(s => s.label);
      extra = `<div class="tsection"><div class="tk">Seed influence</div>` +
        (seen && names.length >= 2 ? `<div class="say two"><i style="width:${(100 * c[0] / seen).toFixed(0)}%"></i><i style="width:${(100 * c[1] / seen).toFixed(0)}%"></i></div>` +
        `<div class="saylab"><span><b class="k1">${esc(names[0])}</b> pulls ${c[0]}</span><span><b class="k4">${esc(names[1])}</b> pulls ${c[1]}</span>${names.length > 2 ? `<span class="hint">the other ${names.length - 2} seeds share the rest; each song leans to whichever seed it sits closest to</span>` : ''}</div>` : '') +
        `<div class="hint">The seeds agree with each other at ${fmt2(t.cohesion)} (1 = the same sound, 0 = unrelated); the list is ranked against their shared centre.</div></div>`;
    }
    return `<div class="tsection"><ol class="tchain">${tiles}</ol></div>` +
      `<div class="tsection"><div class="tk">What the score is made of</div><div class="trecipe">${recipeHtml(t)}</div></div>` + ladder + extra +
      ((t.transitions || []).length ? `<div class="tsection hint">How each song leads into the next is the Arc view (the ∿ Arc button above the list).</div>` : '');
  }

  /* what changed between two lists of pool indices: the songs kept, gone and new, and
     how many of the kept ones changed place relative to each other */
  function diff(prev, cur) {
    const p = Array.isArray(prev) ? prev : [], c = Array.isArray(cur) ? cur : [];
    const inC = new Set(c), inP = new Set(p);
    const kept = p.filter(i => inC.has(i));
    const gone = p.filter(i => !inC.has(i));
    const added = c.filter(i => !inP.has(i));
    const curKept = c.filter(i => inP.has(i));
    let moved = 0;
    for (let k = 0; k < kept.length; k++) if (kept[k] !== curKept[k]) moved++;
    return { kept, gone, added, moved };
  }

  function changesHtml() {
    const prev = S.prevMix;
    if (!Array.isArray(prev) || !prev.length || !S.prevStep) {
      return `<div class="hint tpad">Nothing to compare yet. Vote on a song, remove one, block an artist or change the dials, and this shows what the re-mix kept, dropped and added. Runs compares any two runs of this session.</div>`;
    }
    const d = diff(prev, S.mix);
    const li = (ids, cls) => ids.map(i => `<li class="${cls}">${esc(prevLabels.get(i) || label(i))}</li>`).join('');
    const step = S.prevStep ? esc(S.prevStep) : 'the last step';
    return `<div class="tpad"><b>After ${step}:</b> kept ${d.kept.length}, new ${d.added.length}, gone ${d.gone.length}` +
      (d.moved ? `, ${d.moved} of the kept changed place` : '') + `</div>` +
      `<div class="tcols"><div><div class="tk">Gone (${d.gone.length})</div><ul class="tlist">${li(d.gone, 'gone') || '<li class="hint">none</li>'}</ul></div>` +
      `<div><div class="tk">New (${d.added.length})</div><ul class="tlist">${li(d.added, 'new') || '<li class="hint">none</li>'}</ul></div></div>`;
  }

  /* ---- the runs of this session, compared two at a time */
  function noteRun(t) {
    const lineKind = t.kind === 'mix' || t.kind === 'radio' ? 'fit' : 'sound-alike';
    const v = t.votes || null;
    const r = {
      n: runs.length + 1, at: Date.now(), trace: t, list: Array.isArray(S.mix) ? S.mix.slice() : [],
      kind: t.kind,
      name: S.prevStep || (({ mix: 'Create Mix', blend: 'Blend', adventure: 'Adventure', steer: 'Steering' })[t.kind] || t.kind) +
        ((t.seeds || []).length ? ' from ' + (t.seeds || []).map(s => s.label).join(' + ') : ''),
      inputs: {
        seeds: (t.seeds || []).map(s => s.label).join(' + ') || '',
        recipe: (t.recipe || {}).sound_only ? 'sound alone' : ((t.recipe || {}).profile === 'sound' ? 'the sound profile' : 'the V2 recipe') +
          ((t.recipe || {}).terms ? ' (' + t.recipe.terms.map(x => `${TERM[x.id] || x.id} ×${x.weight}`).join(', ') + ')' : ''),
        line: t.line != null ? `${fmt2(t.line)} (${lineKind})` : 'none, the count is a quota',
        collection: t.collection || 'Full library',
        votes: v ? `+${(v.liked || []).length} ${(v.liked || []).map(x => x.label).join(', ')} −${(v.disliked || []).length} ${(v.disliked || []).map(x => x.label).join(', ')}`.trim() : 'none',
        count: String(t.requested || ''),
      },
      reached: (t.field || []).filter(x => x.status !== 'next').length,
      nfit: ((t.funnel || []).find(x => x.id === 'fit') || {}).n,
    };
    runs.push(r);
    while (runs.length > 20) runs.shift();
    runs.forEach((x, k) => { x.n = k + 1; });
    rb = runs.length - 1; ra = Math.max(0, runs.length - 2);
    for (const i of r.list) if (!labels.has(i)) { const s = (t.seeds || []).find(x => x.i === i); if (s) labels.set(i, s.label); }
  }
  function runsHtml() {
    if (!runs.length) return `<div class="hint tpad">No runs yet this session.</div>`;
    if (runs.length === 1) {
      const r = runs[0];
      return `<div class="hint tpad">One run so far: <b>${esc(r.name)}</b>. Make another (a vote, a Blend, new dials, a new seed) and this compares the two: inputs side by side, what was kept, dropped and added.</div>`;
    }
    const A = runs[ra] || runs[0], B = runs[rb] || runs[runs.length - 1];
    const d = diff(A.list, B.list);
    const rankIn = (r, i) => { const k = r.list.indexOf(i); return k < 0 ? null : k + 1; };
    const row = (k, a, b) => `<div class="crow"><span class="k">${k}</span>${a === b ? `<span class="same">${esc(a)}</span>` : `<span class="a">${esc(a)}</span><span class="arr">→</span><span class="b">${esc(b)}</span>`}</div>`;
    const inputs = ['seeds', 'recipe', 'line', 'collection', 'votes', 'count'].map(k => row(k, A.inputs[k], B.inputs[k])).join('');
    const moved = d.kept.map(i => [i, rankIn(A, i), rankIn(B, i)]).filter(x => x[1] !== x[2]);
    const sameScale = (A.kind === 'mix' || A.kind === 'radio') === (B.kind === 'mix' || B.kind === 'radio');
    return `<div class="runs"><div><div class="tk">Runs this session, newest last. Click to set A, shift-click to set B.</div>` +
      `<ul class="runlist">${runs.map((r, k) => `<li class="${k === ra ? 'a' : ''}${k === rb ? ' b' : ''}" data-k="${k}" title="${esc(r.name)}"><span class="rn">${r.n}</span><span class="rl">${esc(r.name)}</span><span class="rt">looked at ${r.reached}${r.nfit != null ? ` · ${r.nfit} fit` : ''} · ${r.list.length} on the list</span></li>`).join('')}</ul>` +
      `<div class="tsection cmp"><div class="tk">Inputs, run ${A.n} → run ${B.n}</div>${inputs}</div></div>` +
      `<div><div class="tk">Run ${A.n} → run ${B.n}: kept ${d.kept.length}, gone ${d.gone.length}, new ${d.added.length}${moved.length ? `, ${moved.length} of the kept changed place` : ''}${sameScale ? '' : '. Fit is on a different scale in these two runs (the recipe against sound alone), so only places are compared'}</div>` +
      `<div class="tcols"><div><div class="tk">Gone (${d.gone.length})</div><ul class="tlist">${d.gone.slice(0, 14).map(i => `<li class="gone">${esc(label(i))}</li>`).join('') || '<li class="hint">none</li>'}${d.gone.length > 14 ? `<li class="hint">… ${d.gone.length - 14} more</li>` : ''}</ul></div>` +
      `<div><div class="tk">New (${d.added.length})</div><ul class="tlist">${d.added.slice(0, 14).map(i => `<li class="new">${esc(label(i))}</li>`).join('') || '<li class="hint">none</li>'}${d.added.length > 14 ? `<li class="hint">… ${d.added.length - 14} more</li>` : ''}</ul></div></div>` +
      (moved.length ? `<div class="tsection"><div class="tk">Kept, but moved</div><ul class="tlist">${moved.slice(0, 8).map(([i, a, b]) => `<li>${esc(label(i))}<span class="d">#${a} → #${b}</span></li>`).join('')}</ul></div>` : '') + `</div></div>`;
  }

  /* ---- why isn't this song here? */
  function whyHtml() {
    const cards = whyAns.map(a => `<div class="wcard"><div class="h"><b>${esc(a.label)}</b><span class="vc ${a.cls}">${esc(a.verdict)}</span></div><div class="r">${a.text}</div></div>`).join('');
    return `<div class="whyask"><div class="tk">Type a song and press Enter. The answer comes from the same ranking that built this list.</div>` +
      `<div class="whyrow"><input id="whyQ" type="search" placeholder="Why isn't … here?" value="${esc(whyq)}" autocomplete="off"><button type="button" id="whyGo" class="chip">Ask</button>${whyBusy ? '<span class="hint">asking…</span>' : ''}</div>` +
      `<div class="ans">${cards || (whyq && !whyBusy ? '<div class="hint">No song by that name in the library.</div>' : '')}</div></div>`;
  }
  async function askWhy() {
    const t = S.trace; if (!t || !whyq) { whyAns = []; paint(); return; }
    whyBusy = true; whyAns = []; paint();
    try {
      const hits = (await jget('/api/search?q=' + encodeURIComponent(whyq))).results.slice(0, 4);
      const byField = new Map((t.field || []).map(r => [r.i, r]));
      const seedIds = new Set((t.seeds || []).map(s => s.i));
      const p = new URLSearchParams();
      if (S.kind === 'blend' && S.seeds.length >= 2) p.set('seeds', S.seeds.join(','));
      else if (S.seed != null) p.set('seed', S.seed);
      if (S.liked.length) p.set('liked', S.liked.join(','));
      if (S.disliked.length) p.set('disliked', S.disliked.join(','));
      if (typeof curCollection !== 'undefined' && curCollection) p.set('collection', curCollection);
      if (t.reach_depth) p.set('reach', t.reach_depth);
      if (t.line != null) p.set('line', t.line);
      const pool = Number(t.pool || 0).toLocaleString();
      for (const h of hits) {
        const i = h.i;
        let a = { label: h.label };
        if (seedIds.has(i)) a = { ...a, verdict: 'the seed', cls: 'here', text: 'This list grew from it.' };
        else if (S.mix.includes(i)) {
          const s = song(i) || {};
          a = { ...a, verdict: 'here', cls: 'here', text: `On the list at <b>#${S.mix.indexOf(i) + 1}</b>${s.rank ? `, ranked ${Number(s.rank).toLocaleString()} of ${pool}` : ''}${s.fit != null ? `, fit <b>${fmt3(s.fit)}</b>` : ''}${s.past ? ', taken past the line so the list is not empty' : ''}.` };
        } else if (byField.has(i)) {
          const r = byField.get(i);
          const why = WHY[r.status] || r.status;
          const better = r.fit != null && t.songs && Object.values(t.songs).some(s => s.fit != null && s.fit < r.fit);
          a = { ...a, verdict: r.status === 'next' ? 'never considered' : 'considered, passed over', cls: r.status === 'next' ? 'never' : 'drop',
                text: `Ranked <b>${Number(r.rank || 0).toLocaleString()}</b> of ${pool}${r.fit != null ? `, fit <b>${fmt3(r.fit)}</b>` : ''}${better ? ', better than some songs on the list' : ''}. ${r.status === 'next' ? `The walk stopped at #${Number(t.reach_depth).toLocaleString()}, just before it.` : `The walk reached it: <b>${esc(why)}</b>.`}${r.status === 'artist' ? ' Vote More like this on it, or block the artist\'s other songs, and it comes back.' : ''}` };
        } else {
          const q = new URLSearchParams(p); q.set('cand', i);
          const j = await jget('/api/explain/absent?' + q);
          if (j.kind === 'not-mixable') a = { ...a, verdict: 'not mixable', cls: 'unmix', text: 'This song has no fingerprint yet, so no mix can reach it. See Not Mixable in the sidebar.' };
          else if (j.kind === 'outside') a = { ...a, verdict: 'outside the collection', cls: 'never', text: `The list draws from <b>${esc(j.collection)}</b>, and this song is not in it.` };
          else if (j.kind === 'never-reached') a = { ...a, verdict: 'never considered', cls: 'never', text: `Ranked <b>${Number(j.rank).toLocaleString()}</b> of ${Number(j.pool).toLocaleString()} for this list's target, fit <b>${fmt3(j.fit)}</b>. The walk stopped at <b>#${Number(j.reach).toLocaleString()}</b>, so Attune never looked at it.${j.excluded ? ' It is also one of the songs voted on, which a steered list leaves out.' : ''}` };
          else if (j.kind === 'below-line') a = { ...a, verdict: 'below the line', cls: 'never', text: `Ranked ${Number(j.rank).toLocaleString()} of ${Number(j.pool).toLocaleString()}, fit <b>${fmt3(j.fit)}</b>, under the line ${fmt2(j.line)}.` };
          else a = { ...a, verdict: 'reached, not kept', cls: 'drop', text: `Ranked ${Number(j.rank).toLocaleString()} of ${Number(j.pool).toLocaleString()}, fit ${fmt3(j.fit)}, inside where the walk looked; it was left out after the walk (hidden as a duplicate, removed, or past the count).` };
        }
        whyAns.push(a);
      }
    } catch (e) { toast(e.message, true); }
    whyBusy = false; paint();
  }

  /* the window calls this just before a re-mix replaces S.mix, with the step's name */
  function noteStep(stepLabel) {
    S.prevMix = Array.isArray(S.mix) ? S.mix.slice() : [];
    S.prevStep = stepLabel || '';
    prevLabels = new Map(labels);
  }

  function toggle(force) {
    open = typeof force === 'boolean' ? force : !open;
    store.set('traceOpen', open);
    paint();
  }
  function show(which) {
    if (which) { tab = which; store.set('traceTab', tab); }
    toggle(true);
  }
  function setView(v) { view = v; if (v === 'arc') showing = 'list'; paint(); }
  function showPop(pop) {
    if (!inMix()) return;
    if (pop === 'lib') { if (typeof loadLibrary === 'function') { S.view = 'library'; loadLibrary(true); } return; }
    showing = pop; view = 'list'; paint();
  }
  function onView() {
    if (S.view === 'mix' || S.view === 'nowplaying') return;
    const d = $('traceDrawer'); if (d) d.hidden = true;
    const lt = $('lTrace'); if (lt) lt.hidden = true;
    for (const id of ['mixViewList', 'mixViewArc']) { const b = $(id); if (b) b.hidden = true; }
    const fv = $('fieldView'); if (fv) fv.hidden = true;
    const av = $('arcView'); if (av) av.hidden = true;
  }

  /* ---- Why this pick: the one-song view */
  async function showWhy(i, x, y) {
    if (S.seed == null) return toast('Only available inside a mix', true);
    if (S.stats.engine !== 'v2') return toast('Why-this-pick needs the V2 engine', true);
    const t = (inMix() ? S.trace : tr()) || {};
    const p = new URLSearchParams({ seed: S.seed, cand: i });
    if (S.kind === 'blend' && S.seeds.length >= 2) p.set('seeds', S.seeds.join(','));
    if (S.liked.length) p.set('liked', S.liked.join(','));
    if (S.disliked.length) p.set('disliked', S.disliked.join(','));
    try {
      const j = await jget('/api/explain?' + p);
      const s = (t.songs || {})[i] || ((t.field || []).find(r => r.i === i)) || {};
      const el = $('why');
      let head = `<h4>${esc(label(i))}</h4>`;
      const facts = [];
      if (s.rank) facts.push(`ranked ${Number(s.rank).toLocaleString()} of ${Number(t.pool || 0).toLocaleString()}`);
      if (s.fit != null) facts.push(`fit ${fmt3(s.fit)}` + (t.line != null ? ` (line ${fmt2(t.line)})` : ''));
      if (s.past) facts.push('taken past the line so the list is not empty');
      if (s.status && !['kept', 'kept-past'].includes(s.status)) facts.push(WHY[s.status] || s.status);
      if (facts.length) head += `<div class="wfacts">${esc(facts.join(' · '))}</div>`;
      let body = '';
      const bars = (items, max) => items.map(([k, v]) => `
        <div class="bar"><span class="lbl" title="${esc(k)}">${esc(k)}</span>
          <span class="track"><span class="fill ${v < 0 ? 'neg' : ''}" style="width:${Math.abs(v) / max * 100}%"></span></span>
          <span class="v">${v.toFixed(3)}</span></div>`).join('');
      const seedsOnly = (S.kind === 'blend' && S.seeds.length >= 2) || S.kind === 'adventure' ||
                        S.liked.length || S.disliked.length;
      if (S.kind === 'blend' && (j.to_seeds || []).length >= 2) {
        const items = j.to_seeds.map(x => [x.label, x.cos]);
        body += `<div class="wk">How close it sounds to each seed</div>` + bars(items, 1) +
          (j.to_centre != null ? `<div class="wk">To the seeds' shared centre: <b>${fmt3(j.to_centre)}</b> (what the Blend ranks on)</div>` : '');
      } else if (S.kind === 'adventure') {
        body += `<div class="wk">An Adventure stop: fit is how close this song sits to its point on the line between the two ends${s.fit != null ? `, ${fmt3(s.fit)}` : ''}.</div>`;
      } else if (!seedsOnly) {
        const r = j.recipe || {};
        const terms = (r.terms || []).map(x => [TERM[x.id] || x.label, Number(j[x.id] || 0)]);
        const max = Math.max(...terms.map(x => Math.abs(x[1])), 0.001);
        const scale = r.fusion === 'z' ? 'each ingredient on its own scale, weight applied; a negative number is below the library average'
          : 'each ingredient weighted and added';
        body += `<div class="wk">What made its score</div>` + bars(terms, max) +
          `<div class="bar total"><span class="lbl">total</span><span class="track"></span><span class="v">${Number(j.total || 0).toFixed(3)}</span></div>` +
          `<div class="hint">${esc(scale)}</div>`;
      }
      if (S.liked.length || S.disliked.length) {
        const lk = (j.to_liked || []).map(x => [x.label, x.cos]);
        const dk = (j.to_disliked || []).map(x => [x.label, x.cos]);
        if (lk.length) body += `<div class="wk">Closeness to the songs you liked</div>` + bars(lk, 1);
        if (dk.length) body += `<div class="wk">Closeness to the songs you disliked</div>` + bars(dk, 1);
        body += `<div class="hint">a steered list is ranked by sound against the seed's fingerprint pulled toward the liked songs and away from the disliked</div>`;
      }
      el.innerHTML = head + body;
      placeFloating(el, x, y);
    } catch (e) { toast(e.message, true); }
  }

  function bind() {
    const strip_ = $('mixStop');
    if (strip_) strip_.addEventListener('click', e => {
      const ft = e.target.closest('.ftile');
      if (ft) { if (ft.dataset.pop === 'list') { showing = 'list'; view = 'list'; paint(); } else showPop(ft.dataset.pop); return; }
      const b = e.target.closest('.tstage'); if (!b) return;
      show('built');
    });
    const d = $('traceDrawer');
    if (d) {
      d.addEventListener('click', e => {
        const tb = e.target.closest('[data-ttab]');
        if (tb) { tab = tb.dataset.ttab; store.set('traceTab', tab); paint(); return; }
        if (e.target.closest('#traceClose')) return toggle(false);
        const li = e.target.closest('.tchain li[data-pop]');
        if (li) { if (li.dataset.pop === 'list') { showing = 'list'; view = 'list'; paint(); } else showPop(li.dataset.pop); }
      });
    }
    const fv = $('fieldView');
    if (fv) {
      fv.addEventListener('click', e => {
        const back = e.target.closest('[data-pop="list"]'); if (back) { showing = 'list'; paint(); return; }
        const m = e.target.closest('.fitm'); if (m && m.dataset.i) { e.stopPropagation(); showWhy(+m.dataset.i, e.clientX, e.clientY); }
      });
      fv.addEventListener('dblclick', e => {
        const row = e.target.closest('tr[data-i]'); if (!row) return;
        if (typeof Player !== 'undefined' && Player.playList) Player.playList([+row.dataset.i], 0);
      });
    }
    const tb = $('tbody');
    if (tb) tb.addEventListener('click', e => {
      const m = e.target.closest('.fitm'); if (!m || !m.dataset.i) return;
      e.stopPropagation();
      showWhy(+m.dataset.i, e.clientX, e.clientY);
    }, true);
    const lt = $('lTrace');
    if (lt) lt.addEventListener('click', () => toggle());
    const vl = $('mixViewList'), va = $('mixViewArc');
    if (vl) vl.addEventListener('click', () => setView('list'));
    if (va) va.addEventListener('click', () => setView('arc'));
  }

  return { paint, strip, fitCell, toggle, show, onView, showWhy, noteStep, noteRows, diff, bind,
           setView, showPop, get open() { return open; }, get runs() { return runs; }, WHY, TERM };
})();
