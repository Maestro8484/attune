/* Attune trace: what the engine did to make the list on screen, drawn from the server's
   own account. Every mix route returns a `trace` (web/app.py _trace(), 2026-10-05) built
   from the walk's report (src/hybrid.py _walk): each song's place in the ranking and its
   fit, every song the walk reached with what happened to it (the field), how each song
   leads into the next, the loudness of each song on the list, which ingredient carried
   the list, and the verdict: the same facts in a listener's words (TODO.md row 59).
   Nothing here is written by a model or guessed by this script; what the server did not
   record is not shown.

   On screen:
     the card       over the list in the Mix view (and over the queue while Radio is on):
                    the verdict in words, the one deciding number in the accent, the
                    controls that change it as buttons, and under it the sieve: the
                    numbers of the run as bars sized by count, the deciding one lit.
                    Click a bar and the list becomes that population, with a column
                    saying what happened to each song.
     Fit column     each song's fit, a meter from the cut-off to a perfect twin and the
                    number; hover for it in words (its place among the songs you own).
     List / Arc     the list as a table, or as an arc: loudness along the sequence, Heat
                    under it, the closeness of each hand-off as the ground.
     Show the magic the view under Mix in the side list (Joe's name for it): the card and
                    the sieve, the hill of every song looked at, what the score is made of,
                    the field, the arc, the runs of this session compared, and "why isn't
                    this song here?". T opens it from a mix.
     the reveal     the moment a fresh list arrives: the recorded numbers replayed for
                    three seconds over the list, then the card flies into the side list and
                    Show the magic lights up. Never a progress bar; any click skips it.
   Why this pick (right-click, or click a Fit meter) is the one-song view of the same data.
   The MusicIP engine sends no trace: Attune cannot see inside it and says nothing. */
'use strict';

const Trace = (() => {
  const WHY = {
    kept: 'on the list', 'kept-past': 'on the list, filled in past the cut-off',
    carried: 'taken past the cut-off, not needed', count: 'taken, then cut for the count',
    artist: 'held back: the same artist within three songs',
    recording: 'held back: the same recording was already on the list',
    variety: 'skipped by Radio for variety', energy: 'skipped by Radio for a steady loudness',
    line: 'under the cut-off', next: 'next in line, beyond where Attune stopped looking',
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
  let showing = 'list';                // which population the list area shows (Mix view)
  let view = 'list';                   // 'list' | 'arc' (Mix view)
  let mtab = store.get('magicTab', 'built');   // the tab open in Show the magic
  if (!['built', 'field', 'arc', 'changes', 'runs', 'why'].includes(mtab)) mtab = 'built';
  let mpop = 'reach';                  // the population open on the field tab there
  let prevLabels = new Map();          // labels of the list before the last change
  const runs = [];                     // this session's runs, oldest first, 20 deep
  let lastRun = null;                  // the trace object the last run was made from
  let ra = 0, rb = 0;                  // the two runs compared
  let whyq = '', whyAns = [], whyBusy = false;
  let revealedThisSession = false;

  const inMix = () => typeof S !== 'undefined' && S.view === 'mix';
  const inMagic = () => typeof S !== 'undefined' && S.view === 'magic';
  const tr = () => {
    if (typeof S === 'undefined') return null;
    if (S.view === 'mix' || S.view === 'magic') return S.trace;
    if (S.view === 'nowplaying' && typeof Player !== 'undefined' && Player.radio) return Player.radioTrace;
    return null;
  };
  const song = i => { const t = tr(); return t && t.songs ? t.songs[i] : null; };
  const fmt2 = v => (v == null ? '' : Number(v).toFixed(2));
  const fmt3 = v => (v == null ? '' : Number(v).toFixed(3));
  const pct = v => (v == null ? '' : `${Math.round(Number(v) * 100)}%`);
  const ordinal = n => { n = Number(n); const s = ['th', 'st', 'nd', 'rd'], v = n % 100; return n.toLocaleString() + (s[(v - 20) % 10] || s[v] || s[0]); };
  const reduceMotion = () => window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;

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
  /* what the list was made to sound like, for a sentence: the verdict's own words */
  const likeOf = t => (t && t.verdict && t.verdict.like) || ((t && t.seeds && t.seeds[0]) ? t.seeds[0].label : 'the seed');

  /* ---- the words for one song's numbers (the rule, TODO.md row 59): its place among the
     songs you own, then its fit as a share of what an identical song would score */
  function rankWords(rank, t) {
    if (!rank) return '';
    const pool = Number((t && t.pool) || 0);
    const direct = !t || t.kind === 'mix' || t.kind === 'radio';
    if (direct && rank === 1) return 'the seed itself';
    if (direct) return `the ${ordinal(rank - 1)} closest song you own to ${likeOf(t)}${pool ? ` (of ${pool.toLocaleString()})` : ''}`;
    return `ranked ${Number(rank).toLocaleString()}${pool ? ` of ${pool.toLocaleString()}` : ''} for this list's target`;
  }
  const shareWords = (fit, t) => Number(fit) > 0 ? `scores ${pct(fit)} of what an identical song would`
    : `scores below zero: further from ${likeOf(t)} than an average song`;
  function fitWords(s, t) {
    const parts = [];
    const r = rankWords(s.rank, t); if (r) parts.push(r);
    if (s.fit != null) parts.push(shareWords(s.fit, t));
    if (s.past) parts.push('filled in past the cut-off so the list is not empty');
    return parts.join('; ');
  }
  function gapWords(fit, line) {
    const d = Number(line) - Number(fit);
    return d <= 0.02 ? 'by a hair' : (d <= 0.10 ? 'by a little' : 'by a long way');
  }

  /* ---- the fit meter: a bar from the cut-off (or the weakest kept) to a perfect twin.
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
    const w = fitWords(s, t);
    const tip = (w ? w.charAt(0).toUpperCase() + w.slice(1) : `Fit ${fmt3(s.fit)}`) +
      (t.line != null ? `. The cut-off is ${pct(t.line)}` : '') + '. Click for why.';
    return meter(s.fit, s.past, i, tip);
  }

  /* ---- the verdict card: the words over the list */
  function verdictHtml(t, opts = {}) {
    const v = t && t.verdict; if (!v || !v.headline) return '';
    let hl = esc(v.headline);
    const m = hl.match(/\d[\d,]*/);
    if (m) hl = hl.replace(m[0], `<em>${m[0]}</em>`);
    else hl = hl.replace(/^(\S+)/, '<em>$1</em>');
    const levers = opts.levers === false ? '' : (v.levers || [])
      .filter(l => !(opts.noMagic && l.id === 'magic'))
      .map(l => `<button type="button" class="lever${l.id === 'line' ? ' hot' : ''}" data-lever="${l.id}">${esc(l.label)}</button>`).join('');
    return `<div class="vcard"><div class="hl">${hl}</div>${v.body ? `<p class="body">${esc(v.body)}</p>` : ''}${levers ? `<div class="levers">${levers}</div>` : ''}</div>`;
  }

  /* ---- the sieve: the numbers of the run as bars sized by count (log scale, so 21,000
     and 50 both fit on one card), narrowing in the order the code ran them. The deciding
     stage glows. The last bar is honest about the floor: a list filled in past the cut-off
     is wider than the songs that fit, so that bar shows the fit share solid and the rest
     hatched. Click a bar and the list becomes that population. */
  function sieveLabel(s, t) {
    const coll = t.collection && t.collection !== 'Full library' ? t.collection : null;
    switch (s.id) {
      case 'lib': return 'in your library';
      case 'pool': return coll ? `in ${coll}, every one scored` : 'can be mixed, every one scored against the seed';
      case 'reach': return 'looked at, closest first';
      case 'taken': return 'taken';
      case 'fit': return `sound enough like ${likeOf(t)}`;
      case 'list': return `on the list${t.requested ? ` of ${Number(t.requested).toLocaleString()} asked for` : ''}`;
      default: return s.label || '';
    }
  }
  function sieveHtml(t, opts = {}) {
    const tiles = (t && t.funnel) || []; if (!tiles.length) return '';
    const max = Math.max(1, ...tiles.map(s => Number(s.n) || 0));
    const lw = n => { n = Number(n) || 0; return n <= 0 ? 3 : Math.max(5, Math.round(8 + 92 * Math.log(n + 1) / Math.log(max + 1))); };
    const pastN = Object.values(t.songs || {}).filter(s => s.past).length;
    const canOpen = opts.open !== false;
    return `<div class="sieve${opts.cls ? ' ' + opts.cls : ''}">` + tiles.map(s => {
      const n = s.n == null ? '' : Number(s.n).toLocaleString();
      let inner = `<span class="lbl">${esc(sieveLabel(s, t))}</span>`;
      if (s.id === 'list' && pastN && Number(s.n) > 0) {
        const fitShare = Math.max(4, Math.round(100 * (Number(s.n) - pastN) / Number(s.n)));
        inner = `<i class="fill" style="width:${fitShare}%"></i><i class="hatch" style="width:${100 - fitShare}%"></i>` +
          `<span class="lbl two">${esc(sieveLabel(s, t))} · ${pastN} filled in past the cut-off</span>`;
      }
      const cut = s.id === 'fit' && t.line != null ? `<div class="cut"><span>the cut-off ${pct(t.line)}</span></div>` : '';
      const tip = (s.note || '').replace(/the line/g, 'the cut-off') + (canOpen ? (s.id === 'lib' ? '. Click to open the Library' : s.id === 'list' ? '. Click to show the list' : '. Click to see these songs') : '');
      return cut + `<button type="button" class="sv${s.on ? ' on' : ''}${showing === s.id && inMix() ? ' showing' : ''}" data-pop="${s.id}" data-n="${Number(s.n) || 0}" title="${esc(tip)}">` +
        `<span class="c">${n}</span><span class="bar" style="width:${lw(s.n)}%">${inner}</span></button>`;
    }).join('') + `</div>`;
  }

  /* ---- the card over the list (the Mix view, and the queue while Radio is on) */
  function strip() {
    const el = $('mixStop'); if (!el) return;
    const t = inMagic() ? null : tr();            // the page carries its own card
    el.classList.toggle('tstrip', !!t);
    if (!t) { if (inMagic()) el.hidden = true; return; }   // the server's sentence stays as it was
    el.classList.remove('short');
    if (!t.verdict || !(t.funnel || []).length) {  // a trace saved before the card existed
      el.innerHTML = (t.stages || []).map(s =>
        `<button type="button" class="tstage${s.on ? ' on' : ''}" data-stage="${s.id}" title="${esc(s.note || '')}">${esc(s.label)}</button>`
      ).join('<span class="tsep">›</span>');
      el.hidden = false; return;
    }
    el.innerHTML = verdictHtml(t) + sieveHtml(t);
    el.hidden = false;
  }

  /* ---- the field: the list area becomes one population of the run */
  function population(t, pop) {
    const field = t.field || [];
    const name = t.collection || 'Full library';
    const note = id => (((t.funnel || []).find(x => x.id === id) || {}).note || '').replace(/the line/g, 'the cut-off');
    const stopAt = Number(t.reach_depth || 0).toLocaleString();
    if (pop === 'pool') return { rows: field.filter(r => r.status !== 'next'), head: `<b>${Number(t.pool).toLocaleString()} songs</b> scored against the target${name !== 'Full library' ? ` in ${esc(name)}` : ''}; Attune looked at these, closest first`, delta: `the rest were never considered: it stopped looking at #${stopAt}` };
    if (pop === 'reach') return { rows: field, head: `<b>${field.filter(r => r.status !== 'next').length} songs Attune looked at</b>, closest first, then the ten it would have reached next`, delta: `every song ranked past #${stopAt} was never considered` };
    if (pop === 'taken') return { rows: field.filter(r => TAKEN.has(r.status)), head: `<b>the songs taken</b>, closest first`, delta: note('taken') };
    if (pop === 'fit') return { rows: field.filter(r => FIT.has(r.status)), head: `<b>the songs that sound enough like ${esc(likeOf(t))}</b> (over the cut-off ${pct(t.line)}), then the ones filled in past it`, delta: note('fit') };
    return null;
  }
  function fieldHtml(t, pop, opts = {}) {
    const P = population(t, pop); if (!P) return '';
    const rows = P.rows.map(r => {
      const cls = r.status === 'kept-past' ? 'past' : (SC[r.status] === 'drop' ? 'drop' : (SC[r.status] === 'car' ? 'dim' : ''));
      const onList = r.status === 'kept' || r.status === 'kept-past';
      return `<tr data-i="${r.i}" class="${cls}${onList ? ' onlist' : ''}" title="${esc(r.label + '. Double-click to play; click the meter for why.')}">` +
        `<td class="c-pos">${r.rank ? '#' + Number(r.rank).toLocaleString() : ''}</td>` +
        `<td class="c-fit">${r.fit == null ? '' : meter(r.fit, !onList, r.i, fitWords(r, t))}</td>` +
        `<td class="c-title">${esc(r.label)}</td>` +
        `<td class="c-status ${SC[r.status] || ''}"><span class="sc"></span>${esc(WHY[r.status] || r.status)}</td></tr>`;
    }).join('');
    return `<div class="showing-line"><span>Showing: ${P.head}</span><span class="delta">${esc(P.delta)}</span>` +
      (opts.back !== false ? `<button type="button" class="chip" data-pop="list" title="Back to the list as it plays">Back to the list</button>` : '') + `</div>` +
      `<table class="fieldtbl"><thead><tr><th>Rank</th><th>Fit</th><th>Song</th><th>What happened to it</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  function paintField() {
    const fv = $('fieldView'); if (!fv) return;
    const t = tr();
    const show = inMix() && t && view === 'list' && showing !== 'list' && !!population(t, showing);
    fv.hidden = !show;
    const tbl = document.querySelector('#tbl');
    if (!show) { if (tbl && view === 'list' && inMix()) tbl.hidden = false; return; }
    if (tbl) tbl.hidden = true;
    fv.innerHTML = fieldHtml(t, showing);
  }

  /* ---- the hill: every song Attune looked at, closest first, its fit as height, the
     cut-off dashed. Shows WHY a count is what it is: where the hill drops under the line */
  function hillHtml(t) {
    const field = (t.field || []).filter(r => r.fit != null && r.status !== 'next').slice(0, 600);
    if (field.length < 3) return '';
    const n = field.length, W = 1000, H = 190, L = 44, R = 8, top = 16, base = 158;
    const fmax = Math.max(1, ...field.map(r => r.fit)), fmin = Math.min(0, ...field.map(r => r.fit));
    const y = v => top + (base - top) * (1 - (v - fmin) / ((fmax - fmin) || 1));
    const bw = (W - L - R) / n;
    const col = st => SC[st] === 'kept' ? (st === 'kept-past' ? 'past' : 'kept') : (SC[st] === 'drop' ? 'drop' : 'car');
    let svg = `<svg class="hill" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">`;
    svg += `<line x1="${L}" y1="${base}" x2="${W - R}" y2="${base}" class="ax"/>`;
    svg += `<text x="${L - 4}" y="${y(1) + 4}" class="lab" text-anchor="end">100%</text><text x="${L - 4}" y="${base}" class="lab" text-anchor="end">0</text>`;
    field.forEach((r, k) => {
      const yy = y(r.fit);
      svg += `<rect x="${(L + k * bw).toFixed(1)}" y="${yy.toFixed(1)}" width="${Math.max(1, bw - 0.4).toFixed(1)}" height="${Math.max(0.5, base - yy).toFixed(1)}" class="${col(r.status)}"><title>${esc(`#${r.rank} ${r.label} · ${Number(r.fit) > 0 ? pct(r.fit) + ' of a twin' : 'below zero'} · ${WHY[r.status] || r.status}`)}</title></rect>`;
    });
    if (t.line != null) {
      const ly = y(t.line);
      svg += `<line x1="${L}" y1="${ly.toFixed(1)}" x2="${W - R}" y2="${ly.toFixed(1)}" class="ln"/>` +
        `<text x="${W - R}" y="${(ly - 5).toFixed(1)}" class="lab on" text-anchor="end">the cut-off ${pct(t.line)}</text>`;
      const firstPast = field.findIndex(r => r.status === 'kept-past');
      if (firstPast > 0) {
        const r = field[firstPast];
        svg += `<text x="${(L + firstPast * bw + 4).toFixed(1)}" y="${H - 6}" class="lab">#${r.rank} ${esc(r.label)}: first filled in past the cut-off, missed it ${gapWords(r.fit, t.line)}</text>`;
      }
    }
    svg += `<text x="${L}" y="${H - 6 - (t.line != null && field.some(r => r.status === 'kept-past') ? 12 : 0)}" class="lab">#${field[0].rank} closest</text>` +
      `<text x="${W - R}" y="${H - 6 - (t.line != null && field.some(r => r.status === 'kept-past') ? 12 : 0)}" class="lab" text-anchor="end">#${field[n - 1].rank}</text></svg>`;
    return `<div class="tsection"><div class="tk">The hill: every song Attune looked at, closest first, its fit as height${t.line != null ? '; the cut-off dashed' : ''}</div>${svg}` +
      `<div class="saylab"><span><b class="k1">amber</b> on the list</span><span><b class="k2">grey</b> filled in past the cut-off, or cut for the count</span><span><b class="k3">red</b> held back</span></div></div>`;
  }

  /* ---- the arc: the list as a sequence */
  function heatIndex() {
    const ef = (S.stats && S.stats.earfeel) || [];
    const k = ef.findIndex(f => f.id === 'heat');
    return k < 0 ? null : k;
  }
  function arcHtml(t) {
    if (!(t.transitions || []).length) return '';
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
    const loud = v => v > 1 ? 'among your loudest' : v < -1 ? 'among your quietest' : v > 0.3 ? 'louder than average' : v < -0.3 ? 'quieter than average' : 'about average loudness';
    let svg = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;
    if (have.length) {
      svg += `<line x1="${padL}" y1="${ye(0)}" x2="${W - padR}" y2="${ye(0)}" class="zero"/>` +
        `<text x="${padL - 4}" y="${top + 4}" class="lab" text-anchor="end">loud</text>` +
        `<text x="${padL - 4}" y="${H - bot}" class="lab" text-anchor="end">quiet</text>`;
    }
    trs.forEach((v, k) => {
      const h = 6 + 24 * (v - tmin) / ((tmax - tmin) || 1), xa = x(k), xb = x(k + 1);
      svg += `<rect x="${xa}" y="${H - bot + 30 - h}" width="${Math.max(1, xb - xa)}" height="${h}" class="ho${k === worst ? ' worst' : ''}"><title>${esc(`#${k + 1} → #${k + 2}: ${pct(v)} alike`)}</title></rect>`;
    });
    const hp = heat.map((v, k) => v == null ? null : `${x(k)},${yh(v)}`).filter(Boolean).join(' ');
    if (hp) svg += `<polyline points="${hp}" class="heat"/>`;
    const ep = en.map((v, k) => v == null ? null : `${x(k)},${ye(v)}`).filter(Boolean).join(' ');
    if (ep) svg += `<polyline points="${ep}" class="energy"/>`;
    seq.forEach((r, k) => {
      if (en[k] == null) return;
      const sg = song(r.i);
      const tip = `#${k + 1} ${r.title}${r.artist ? ' — ' + r.artist : ''}\n${loud(en[k])}` +
        (sg && sg.fit != null ? ` · ${pct(sg.fit)} of a twin` : '') + (r.year ? ` · ${r.year}` : '') +
        (k ? `\nhand-off from #${k}: ${pct(trs[k - 1])} alike` : '');
      svg += `<circle cx="${x(k)}" cy="${ye(en[k])}" r="${k === 0 ? 5 : 3.5}" class="node${k === 0 ? ' seed' : ''}"><title>${esc(tip)}</title></circle>`;
    });
    svg += `<text x="${(x(worst) + x(worst + 1)) / 2}" y="${H - bot + 42}" class="lab on" text-anchor="middle">weakest hand-off #${worst + 1} → #${worst + 2} (${pct(trs[worst])} alike)</text>`;
    svg += `<text x="${padL}" y="${H - 6}" class="lab">how alike each song is to the next, as the ground (taller = closer)</text></svg>`;
    let shape = '';
    if (have.length >= 3) {
      const first = en[0], last = en[n - 1];
      const peak = en.indexOf(emax), trough = en.indexOf(emin);
      const trend = (last != null && first != null) ? (last - first > 0.4 ? 'ends louder than it starts' : (first - last > 0.4 ? 'ends quieter than it starts' : 'ends about where it starts')) : '';
      const years = seq.map(r => r.year).filter(Boolean);
      shape = `<p class="shape">The shape: loudest at <b>#${peak + 1}</b> (${esc(seq[peak].title)}, ${loud(emax)}), quietest at <b>#${trough + 1}</b> (${esc(seq[trough].title)}, ${loud(emin)})${trend ? ', ' + trend : ''}.` +
        (years.length ? ` Years run ${Math.min(...years)} to ${Math.max(...years)}.` : '') +
        ` The weakest hand-off is <b>#${worst + 1} → #${worst + 2}</b>, ${esc(seq[worst].title)} into ${esc(seq[worst + 1].title)}, still ${pct(trs[worst])} alike` +
        ((t.stages || []).some(s => s.id === 'flow') ? ', with Arrange for flow on.' : '; Arrange for flow (Mix options) would re-order by these hand-offs.') +
        ` Whether this arc sounds right is your ear's call; this only reports it.</p>`;
    }
    return svg +
      `<div class="legend2"><span><i class="e"></i>loudness along the list (the axis Radio's arc steers by)</span>${hk != null ? '<span><i class="h"></i>Earfeel Heat, cool to fiery</span>' : ''}<span><i class="g"></i>how alike each hand-off is</span></div>` + shape;
  }
  function paintArc() {
    const av = $('arcView'); if (!av) return;
    const t = tr();
    const show = inMix() && t && view === 'arc' && (t.transitions || []).length >= 1;
    av.hidden = !show;
    const tbl = document.querySelector('#tbl');
    if (!show) return;
    if (tbl) tbl.hidden = true;
    av.innerHTML = arcHtml(t);
  }

  /* ---- the page: Show the magic */
  function litPop(t) {
    const on = (t.funnel || []).find(s => s.on);
    return on && on.id !== 'lib' && on.id !== 'list' ? on.id : 'reach';
  }
  function paintMagic() {
    const mv = $('magicView'); if (!mv) return;
    mv.hidden = !inMagic();
    if (!inMagic()) return;
    const tbl = document.querySelector('#tbl'); if (tbl) tbl.hidden = true;
    const t = S.trace;
    if (!t) {
      mv.innerHTML = `<div class="mempty">No mix on screen yet. Select a song and press Mix; this page then shows everything Attune did to build the list: the numbers in words, every song it looked at and what happened to it, the list as an arc, the runs of this session compared, and why a song is not here.</div>`;
      return;
    }
    const tabs = [['built', 'How it was built'], ['field', 'Every song looked at'], ['arc', 'The arc'], ['changes', 'What changed'], ['runs', 'Runs'], ['why', "Why isn't it here?"]];
    if (mtab === 'arc' && !(t.transitions || []).length) mtab = 'built';
    let body = '';
    if (mtab === 'built') body = builtHtml(t);
    else if (mtab === 'field') {
      const pops = [['reach', 'looked at'], ['pool', 'scored'], ['taken', 'taken'], ['fit', 'sound enough like it']].filter(p => population(t, p[0]));
      if (!population(t, mpop)) mpop = litPop(t);
      body = `<div class="mpops">${pops.map(p => `<button type="button" class="chip${p[0] === mpop ? ' on' : ''}" data-mpop="${p[0]}">${esc(p[1])}</button>`).join('')}</div>` +
        `<div class="fieldView inpage">${fieldHtml(t, mpop, { back: false })}</div>`;
    } else if (mtab === 'arc') body = `<div class="arcView inpage">${arcHtml(t)}</div>`;
    else if (mtab === 'changes') body = changesHtml();
    else if (mtab === 'runs') body = runsHtml();
    else body = whyHtml();
    mv.innerHTML = `<div class="mhead">${verdictHtml(t, { noMagic: true })}${sieveHtml(t, { cls: 'big' })}</div>` +
      `<div class="mtabs">${tabs.filter(x => x[0] !== 'arc' || (t.transitions || []).length).map(x => `<button type="button" data-mtab="${x[0]}" class="${x[0] === mtab ? 'on' : ''}">${esc(x[1])}</button>`).join('')}</div>` +
      `<div class="mbody">${body}</div>`;
    if (mtab === 'why') {
      const inp = $('whyQ');
      if (inp) inp.addEventListener('keydown', e => { if (e.key === 'Enter') { whyq = inp.value.trim(); askWhy(); } });
      const go = $('whyGo'); if (go) go.addEventListener('click', () => { whyq = (($('whyQ') || {}).value || '').trim(); askWhy(); });
    }
    if (mtab === 'runs') mv.querySelectorAll('.runlist li').forEach(li => li.addEventListener('click', e => {
      const k = +li.dataset.k; if (e.shiftKey) rb = k; else ra = k; paint();
    }));
  }

  function paint() {
    const t = tr();
    noteRows(S.rows);
    if ((inMix() || inMagic()) && t && t !== lastRun) {   // a new run arrived: remember it, show the list
      lastRun = t;
      noteRun(t);
      showing = 'list'; view = 'list'; mpop = litPop(t);
      if (S.revealNext) { S.revealNext = false; if (inMix()) reveal(t); }
    }
    if (!(inMix() || inMagic()) || !t) { view = 'list'; showing = 'list'; }
    strip();
    paintField();
    paintArc();
    paintMagic();
    const lt = $('lTrace');
    if (lt) lt.hidden = !(t && inMix());
    for (const [id, which] of [['mixViewList', 'list'], ['mixViewArc', 'arc']]) {
      const b = $(id); if (!b) continue;
      b.hidden = !(inMix() && t && (t.transitions || []).length);
      b.classList.toggle('on', view === which);
    }
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
    // the sentence: which ingredients carried the list, in words
    let said = '';
    if (keys.length >= 2 && Object.keys(say).length) {
      const name = k => ({ clap: 'how the songs sound', genre: 'their genre tags', era: 'their year', earfeel: 'their Earfeel', artist: 'artist links', lib: 'their timbre', bpm: 'their tempo', key: 'their key' })[k] || (TERM[k] || k).toLowerCase();
      const p = k => `${(say[k] * 100).toFixed(0)}%`;
      const [a, b] = keys;
      const nudged = keys.slice(2).filter(k => (say[k] || 0) >= 0.08);
      const barely = keys.slice(2).filter(k => (say[k] || 0) < 0.08);
      const list = ks => ks.map(name).join(', ').replace(/, ([^,]*)$/, ' and $1');
      said = `<p class="shape">This list was chosen mostly by <b>${name(a)}</b> (${p(a)})` + ((say[b] || 0) >= 0.2 ? ` and <b>${name(b)}</b> (${p(b)})` : `; ${name(b)} nudged it (${p(b)})`) +
        (nudged.length ? `; ${list(nudged)} nudged it (${nudged.map(p).join(', ')})` : '') + '.' +
        (barely.length ? ` ${list(barely).replace(/^./, c => c.toUpperCase())} barely spoke.` : '') + `</p>`;
    }
    return said + bar + chips + `<span class="hint">${esc(prof)}; ${esc(scale)}${Object.keys(say).length ? '; the percentages are each ingredient\'s share of the say, averaged over the songs on the list' : ''}</span>`;
  }

  function builtHtml(t) {
    let extra = '';
    if (t.kind === 'blend' && t.cohesion != null) {
      const c = [0, 0]; let seen = 0;
      for (const s of Object.values(t.songs || {})) { if (s.seeds && s.seeds.length >= 2) { c[s.seeds[0] >= s.seeds[1] ? 0 : 1]++; seen++; } }
      const names = (t.seeds || []).map(s => s.label);
      extra = `<div class="tsection"><div class="tk">Seed influence</div>` +
        (seen && names.length >= 2 ? `<div class="say two"><i style="width:${(100 * c[0] / seen).toFixed(0)}%"></i><i style="width:${(100 * c[1] / seen).toFixed(0)}%"></i></div>` +
        `<div class="saylab"><span><b class="k1">${esc(names[0])}</b> pulls ${c[0]}</span><span><b class="k4">${esc(names[1])}</b> pulls ${c[1]}</span>${names.length > 2 ? `<span class="hint">the other ${names.length - 2} seeds share the rest; each song leans to whichever seed it sits closest to</span>` : ''}</div>` : '') +
        `<div class="hint">The seeds are ${pct(t.cohesion)} alike (100% would be the same sound, 0% unrelated); the list is ranked against their shared centre.</div></div>`;
    }
    return hillHtml(t) +
      `<div class="tsection"><div class="tk">What the score is made of</div><div class="trecipe">${recipeHtml(t)}</div></div>` + extra;
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
  /* the same difference in words: mostly the same list, or mostly new */
  function diffWords(d, total) {
    const n = total || (d.kept.length + d.added.length);
    const same = n ? d.kept.length / n : 0;
    const lead = same >= 0.75 ? 'Mostly the same list' : same >= 0.4 ? 'About half the list changed' : 'Mostly a new list';
    return `${lead}: ${d.kept.length} of ${n} stayed, ${d.added.length} came in, ${d.gone.length} went out` + (d.moved ? `, and ${d.moved} of those that stayed changed place` : '') + '.';
  }

  function changesHtml() {
    const prev = S.prevMix;
    if (!Array.isArray(prev) || !prev.length || !S.prevStep) {
      return `<div class="hint tpad">Nothing to compare yet. Vote on a song, remove one, block an artist or change the dials, and this shows what the re-mix kept, dropped and added. Runs compares any two runs of this session.</div>`;
    }
    const d = diff(prev, S.mix);
    const li = (ids, cls) => ids.map(i => `<li class="${cls}">${esc(prevLabels.get(i) || label(i))}</li>`).join('');
    const step = S.prevStep ? esc(S.prevStep) : 'the last step';
    return `<div class="tpad"><b>After ${step}:</b> ${esc(diffWords(d, S.mix.length))}</div>` +
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
        line: t.line != null ? `${pct(t.line)} (${lineKind})` : 'none, the count is a quota',
        collection: t.collection || 'Full library',
        votes: v ? `+${(v.liked || []).length} ${(v.liked || []).map(x => x.label).join(', ')} −${(v.disliked || []).length} ${(v.disliked || []).map(x => x.label).join(', ')}`.trim() : 'none',
        count: String(t.requested || ''),
      },
      reached: (t.field || []).filter(x => x.status !== 'next').length,
      nfit: ((t.funnel || []).find(x => x.id === 'fit') || {}).n,
      headline: (t.verdict || {}).headline || '',
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
      `<ul class="runlist">${runs.map((r, k) => `<li class="${k === ra ? 'a' : ''}${k === rb ? ' b' : ''}" data-k="${k}" title="${esc(r.headline || r.name)}"><span class="rn">${r.n}</span><span class="rl">${esc(r.name)}</span><span class="rt">${esc(r.headline || `looked at ${r.reached}${r.nfit != null ? ` · ${r.nfit} fit` : ''} · ${r.list.length} on the list`)}</span></li>`).join('')}</ul>` +
      `<div class="tsection cmp"><div class="tk">Inputs, run ${A.n} → run ${B.n}</div>${inputs}</div></div>` +
      `<div><div class="tk">Run ${A.n} → run ${B.n}: ${esc(diffWords({ ...d, moved: moved.length }, B.list.length))}${sameScale ? '' : ' Fit is on a different scale in these two runs (the recipe against sound alone), so only places are compared.'}</div>` +
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
      const reach = Number(t.reach_depth || 0).toLocaleString();
      const like = likeOf(t);
      const direct = t.kind === 'mix' || t.kind === 'radio';
      const far = (rank, pool) => direct && rank > 2 ? `${(rank - 2).toLocaleString()} songs you own sound more like ${like} than it does` : `ranked ${Number(rank).toLocaleString()} of ${Number(pool).toLocaleString()} for this list's target`;
      for (const h of hits) {
        const i = h.i;
        let a = { label: h.label };
        if (seedIds.has(i)) a = { ...a, verdict: 'the seed', cls: 'here', text: 'This list grew from it.' };
        else if (S.mix.includes(i)) {
          const s = song(i) || {};
          const w = fitWords(s, t);
          a = { ...a, verdict: 'here', cls: 'here', text: `On the list at <b>#${S.mix.indexOf(i) + 1}</b>${w ? `: ${esc(w)}` : ''}.` };
        } else if (byField.has(i)) {
          const r = byField.get(i);
          const why = WHY[r.status] || r.status;
          const better = r.fit != null && t.songs && Object.values(t.songs).some(s => s.fit != null && s.fit < r.fit);
          const w = fitWords({ rank: r.rank, fit: r.fit }, t);
          a = { ...a, verdict: r.status === 'next' ? 'never considered' : 'looked at, held back', cls: r.status === 'next' ? 'never' : 'drop',
                text: `${esc(w.charAt(0).toUpperCase() + w.slice(1))}${better ? ', closer than some songs on the list' : ''}. ${r.status === 'next' ? `Attune stopped looking at #${reach}, just before it.` : `It was looked at and <b>${esc(why)}</b>.`}${r.status === 'artist' ? ' Vote More like this on it, or block the artist\'s other songs, and it comes back.' : ''}` };
        } else {
          const q = new URLSearchParams(p); q.set('cand', i);
          const j = await jget('/api/explain/absent?' + q);
          if (j.kind === 'not-mixable') a = { ...a, verdict: 'not mixable', cls: 'unmix', text: 'This song has no fingerprint yet, so no mix can reach it. See Not Mixable in the sidebar.' };
          else if (j.kind === 'outside') a = { ...a, verdict: 'outside the collection', cls: 'never', text: `The list draws from <b>${esc(j.collection)}</b>, and this song is not in it.` };
          else if (j.kind === 'never-reached') a = { ...a, verdict: 'never considered', cls: 'never', text: `<b>${esc(far(j.rank, j.pool).charAt(0).toUpperCase() + far(j.rank, j.pool).slice(1))}</b>${j.fit != null ? ` (it ${esc(shareWords(j.fit, t))})` : ''}. Attune stopped looking at <b>#${Number(j.reach).toLocaleString()}</b>, so it was never considered.${j.excluded ? ' It is also one of the songs voted on, which a steered list leaves out.' : ''}` };
          else if (j.kind === 'below-line') a = { ...a, verdict: 'under the cut-off', cls: 'never', text: `${esc(far(j.rank, j.pool).charAt(0).toUpperCase() + far(j.rank, j.pool).slice(1))}; it ${esc(shareWords(j.fit, t))} and the cut-off is ${pct(j.line)}.` };
          else a = { ...a, verdict: 'looked at, not kept', cls: 'drop', text: `${esc(far(j.rank, j.pool).charAt(0).toUpperCase() + far(j.rank, j.pool).slice(1))} (${Number(j.fit) > 0 ? pct(j.fit) + ' of a twin' : 'below zero'}), inside where Attune looked; it was left out after the walk (hidden as a duplicate, removed, or past the count).` };
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

  function setView(v) { view = v; if (v === 'arc') showing = 'list'; paint(); }
  function showPop(pop) {
    if (inMagic()) { mtab = 'field'; mpop = pop === 'list' ? 'fit' : pop; store.set('magicTab', mtab); paint(); return; }
    if (!inMix()) return;
    if (pop === 'lib') { if (typeof loadLibrary === 'function') { S.view = 'library'; loadLibrary(true); } return; }
    showing = pop; view = 'list'; paint();
  }
  function onView() {
    if (S.view === 'mix' || S.view === 'nowplaying' || S.view === 'magic') return;
    const lt = $('lTrace'); if (lt) lt.hidden = true;
    for (const id of ['mixViewList', 'mixViewArc']) { const b = $(id); if (b) b.hidden = true; }
    const fv = $('fieldView'); if (fv) fv.hidden = true;
    const av = $('arcView'); if (av) av.hidden = true;
    const mv = $('magicView'); if (mv) mv.hidden = true;
  }

  /* ---- the levers: a verdict's buttons go to the control they name */
  function flashControl(el) {
    if (!el) return;
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 1800);
    try { el.scrollIntoView({ block: 'nearest' }); } catch (e) { /* older engines */ }
    if (el.focus) el.focus();
    if (el.select) el.select();
  }
  function openOptionsFor(id) {
    // after the lever's own click has finished bubbling: the window closes an open
    // popover on any click outside it, and this click is outside it
    setTimeout(() => {
      const panel = $('optionsPanel'), btn = $('btnOptions');
      if (panel && panel.hidden && btn) btn.click();
      setTimeout(() => flashControl($(id)), 80);
    }, 0);
  }
  function pullLever(id) {
    if (id === 'magic') { if (typeof showMagic === 'function') showMagic(); return; }
    if (id === 'line') return openOptionsFor('fitLine');
    if (id === 'variety') return openOptionsFor('radioVariety');
    if (id === 'count') { const el = $('mixSize'); if (el) { flashControl(el); toast('Set the length here, then press Mix again'); } return; }
    if (id === 'undo') { const b = $('steerReset'); if (b) b.click(); else toast('No votes to clear on this list'); return; }
  }

  /* ---- the reveal: the recorded numbers replayed over the list, three seconds */
  let revealTimers = [], revealRaf = 0, revealing = false;
  function willReveal(t) {
    if (!t || !t.verdict || !(t.funnel || []).length || t.kind === 'radio') return false;
    if (!$('reveal')) return false;
    const pref = store.get('reveal', 'every');
    if (pref === 'never') return false;
    if (pref === 'first' && revealedThisSession) return false;
    return true;
  }
  function clearReveal() {
    revealTimers.forEach(clearTimeout); revealTimers = [];
    cancelAnimationFrame(revealRaf);
    revealing = false;
  }
  function tick(el, n, ms) {
    const start = performance.now();
    const step = now => {
      const p = Math.min(1, (now - start) / ms);
      el.textContent = Math.round(n * (p < 1 ? 1 - Math.pow(1 - p, 3) : 1)).toLocaleString();
      if (p < 1) revealRaf = requestAnimationFrame(step);
    };
    revealRaf = requestAnimationFrame(step);
  }
  function reveal(t) {
    const ov = $('reveal'), pop = $('rpop'); if (!ov || !willReveal(t)) return;
    revealedThisSession = true;
    clearReveal();
    const still = reduceMotion();
    $('rhl').innerHTML = '&nbsp;';
    $('rsieve').innerHTML = sieveHtml(t, { cls: 'r', open: false });
    ov.hidden = false; ov.classList.remove('fly'); ov.classList.add('show');
    pop.style.transform = '';
    const svs = Array.from($('rsieve').querySelectorAll('.sv'));
    const at = (ms, fn) => revealTimers.push(setTimeout(fn, still ? 0 : ms));
    const lit = Math.max(0, svs.findIndex(e => e.classList.contains('on')));
    svs.forEach((e, k) => {
      const bar = e.querySelector('.bar'), c = e.querySelector('.c');
      const w = bar.style.width, n = +e.dataset.n;
      if (!still) { bar.style.width = '0%'; c.textContent = '0'; }
      const when = k < lit ? 150 + k * 300 : (k === lit ? 1800 : 2200 + (k - lit - 1) * 250);
      at(when, () => { bar.style.width = w; if (!still) tick(c, n, 420); });
    });
    at(1800, () => { $('rhl').innerHTML = verdictHtml(t, { levers: false }).replace(/^<div class="vcard">/, '<div>'); });
    at(still ? 2000 : 3000, flyReveal);
    revealing = true;
  }
  function flyReveal() {
    const ov = $('reveal'), pop = $('rpop'); if (!ov || ov.hidden) return;
    clearReveal();
    const nav = document.querySelector('#tree li[data-view="magic"]');
    if (nav && !reduceMotion()) {
      const a = pop.getBoundingClientRect(), b = nav.getBoundingClientRect();
      pop.style.setProperty('--fx', `${(b.left + b.width / 2) - (a.left + a.width / 2)}px`);
      pop.style.setProperty('--fy', `${(b.top + b.height / 2) - (a.top + a.height / 2)}px`);
      ov.classList.add('fly');
    }
    revealTimers.push(setTimeout(() => {
      ov.classList.remove('show', 'fly'); ov.hidden = true;
      if (nav) { nav.classList.remove('lit'); void nav.offsetWidth; nav.classList.add('lit'); setTimeout(() => nav.classList.remove('lit'), 1600); }
      const nb = $('magicNew'); if (nb && !inMagic()) nb.hidden = false;
    }, reduceMotion() ? 0 : 520));
  }
  function skipReveal() { if (revealing) flyReveal(); }

  /* ---- Why this pick: the one-song view */
  async function showWhy(i, x, y) {
    if (S.seed == null) return toast('Only available inside a mix', true);
    if (S.stats.engine !== 'v2') return toast('Why-this-pick needs the V2 engine', true);
    const t = ((inMix() || inMagic()) ? S.trace : tr()) || {};
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
      const w = fitWords(s, t); if (w) facts.push(w.charAt(0).toUpperCase() + w.slice(1));
      if (s.fit != null && t.line != null) facts.push(`the cut-off is ${pct(t.line)}`);
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
          (j.to_centre != null ? `<div class="wk">To the seeds' shared centre: <b>${pct(j.to_centre)} alike</b> (what the Blend ranks on)</div>` : '');
      } else if (S.kind === 'adventure') {
        body += `<div class="wk">An Adventure stop: fit is how close this song sits to its point on the path between the two ends${s.fit != null ? `, ${pct(s.fit)}` : ''}.</div>`;
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
    const onCard = e => {
      const lv = e.target.closest('.lever[data-lever]');
      if (lv) { pullLever(lv.dataset.lever); return true; }
      const sv = e.target.closest('.sv[data-pop]');
      if (sv) { if (sv.dataset.pop === 'list' && inMix()) { showing = 'list'; view = 'list'; paint(); } else showPop(sv.dataset.pop); return true; }
      return false;
    };
    const strip_ = $('mixStop');
    if (strip_) strip_.addEventListener('click', e => {
      if (onCard(e)) return;
      const b = e.target.closest('.tstage'); if (!b) return;
      if (typeof showMagic === 'function') showMagic();
    });
    const mv = $('magicView');
    if (mv) {
      mv.addEventListener('click', e => {
        if (onCard(e)) return;
        const tb = e.target.closest('[data-mtab]');
        if (tb) { mtab = tb.dataset.mtab; store.set('magicTab', mtab); paint(); return; }
        const pb = e.target.closest('[data-mpop]');
        if (pb) { mpop = pb.dataset.mpop; paint(); return; }
        const m = e.target.closest('.fitm'); if (m && m.dataset.i) { e.stopPropagation(); showWhy(+m.dataset.i, e.clientX, e.clientY); }
      });
      mv.addEventListener('dblclick', e => {
        const row = e.target.closest('tr[data-i]'); if (!row) return;
        if (typeof Player !== 'undefined' && Player.playList) Player.playList([+row.dataset.i], 0);
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
    if (lt) lt.addEventListener('click', () => { if (typeof showMagic === 'function') showMagic(); });
    const vl = $('mixViewList'), va = $('mixViewArc');
    if (vl) vl.addEventListener('click', () => setView('list'));
    if (va) va.addEventListener('click', () => setView('arc'));
    const ov = $('reveal');
    if (ov) ov.addEventListener('click', skipReveal);
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && revealing) { skipReveal(); } });
  }

  return { paint, strip, fitCell, onView, showWhy, noteStep, noteRows, diff, diffWords, bind,
           setView, showPop, willReveal, reveal, skipReveal, pullLever,
           get runs() { return runs; }, WHY, TERM };
})();
