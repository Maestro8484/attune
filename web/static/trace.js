/* Attune trace: what the engine did to make the list on screen, drawn from the server's
   own account. Every mix route returns a `trace` (web/app.py _trace(), 2026-10-05) built
   from the walk's report (src/hybrid.py _walk): each song's place in the ranking and its
   fit, every song the walk reached and did not take with the reason, how each song leads
   into the next, and the stages in the order the code ran them. Nothing here is written
   by a model or guessed by this script; what the server did not record is not shown.

   Three things on screen, all in the Mix view only:
     the strip   the stages on the mix header, the one that decided the list lit in the
                 accent colour; click a stage to open the drawer on it
     Fit column  each song's fit, a meter from the line to a perfect twin and the number
     the drawer  under the list, closed by default (the Trace button, or T): how the list
                 was built, what was left out and why, and what changed since the last step
   Why this pick (right-click, or click a Fit meter) is the one-song view of the same data.
   The MusicIP engine sends no trace: Attune cannot see inside it and says nothing. */
'use strict';

const Trace = (() => {
  const WHY = {
    artist: 'same artist within 3 songs',
    recording: 'the same recording is already on the list',
    variety: "Radio's variety coin passed it over",
    energy: "Radio's energy corridor passed it over",
    line: 'below the fit line',
    count: 'next in rank, past the count asked for',
    removed: 'removed by you',
    blocked: 'its artist is blocked',
    duplicate: 'a duplicate of a song on the list',
    'near-twin': 'set aside for variety: sounds almost the same as a song kept',
  };
  const TERM = {
    clap: 'Sound fingerprint', lib: 'Timbre', genre: 'Genre tags', bpm: 'Tempo', era: 'Year',
    key: 'Key', artist: 'Artist links', earfeel: 'Earfeel',
  };
  const labels = new Map();            // pool index -> "Artist — Title", from rows painted
  let tab = store.get('traceTab', 'path');
  let open = store.get('traceOpen', false);
  let prevLabels = new Map();          // labels of the list before the last change

  // the trace on screen: the mix's in the Mix view; in Now Playing, the last Radio
  // batch's while Radio is on (player.js keeps it), so the queue says how it grew
  const tr = () => {
    if (typeof S === 'undefined') return null;
    if (S.view === 'mix') return S.trace;
    if (S.view === 'nowplaying' && typeof Player !== 'undefined' && Player.radio) return Player.radioTrace;
    return null;
  };
  const song = i => { const t = tr(); return t && t.songs ? t.songs[i] : null; };
  const fmt2 = v => (v == null ? '' : Number(v).toFixed(2));
  const fmt3 = v => (v == null ? '' : Number(v).toFixed(3));

  function noteRows(rows) {
    for (const r of rows || []) {
      if (r && r.i >= 0) labels.set(r.i, r.artist ? `${r.artist} — ${r.title}` : (r.title || `song ${r.i}`));
    }
  }
  function label(i) {
    if (labels.has(i)) return labels.get(i);
    const t = S.trace;
    const s = t && (t.seeds || []).find(x => x.i === i);
    if (s) return s.label;
    const l = t && (t.left_out || []).find(x => x.i === i);
    return l ? l.label : `song ${i}`;
  }

  /* ---- the fit meter: a bar from the line (or the weakest kept) to a perfect twin.
     The bar is relative so that a list whose fits sit between 0.6 and 0.9 reads as a
     range and not as ten full bars; the number beside it is the exact fit. */
  function fitFloor() {
    const t = tr(); if (!t) return 0;
    let lo = t.line != null ? t.line : 1;
    for (const s of Object.values(t.songs || {})) if (s.fit != null && s.fit < lo) lo = s.fit;
    if (lo >= 1) lo = 0;
    return Math.max(0, Math.min(0.95, lo - 0.04));
  }
  function fitCell(i) {
    const s = song(i); if (!s || s.fit == null) return '';
    const lo = fitFloor();
    const w = Math.max(0, Math.min(1, (s.fit - lo) / (1 - lo)));
    const t = tr();
    const tip = `Fit ${fmt3(s.fit)} of a perfect twin (1.000)` +
      (s.rank ? `, ranked ${s.rank.toLocaleString()} of ${t.pool.toLocaleString()}` : '') +
      (t.line != null ? `; the line is ${fmt2(t.line)}` : '') +
      (s.past ? '. Taken past the line so the list is not empty' : '') + '. Click for why.';
    return `<span class="fitm${s.past ? ' past' : ''}" data-i="${i}" title="${esc(tip)}">` +
      `<i style="width:${Math.round(w * 100)}%"></i></span><b>${fmt2(s.fit)}</b>`;
  }

  /* ---- the strip on the mix header */
  function strip() {
    const el = $('mixStop'); if (!el) return;
    const t = tr();
    el.classList.toggle('tstrip', !!t);
    if (!t) return;                               // the server's sentence stays as it was
    el.classList.remove('short');
    el.innerHTML = (t.stages || []).map(s =>
      `<button type="button" class="tstage${s.on ? ' on' : ''}" data-stage="${s.id}" title="${esc(s.note || '')}">${esc(s.label)}</button>`
    ).join('<span class="tsep">›</span>');
    el.hidden = false;
  }

  /* ---- the drawer */
  function paint() {
    const t = tr();
    noteRows(S.rows);
    strip();
    const lt = $('lTrace');
    if (lt) { lt.hidden = !t; lt.classList.toggle('on', !!(t && open)); lt.textContent = (t && open) ? 'Hide trace' : 'Trace'; }
    const d = $('traceDrawer'); if (!d) return;
    d.hidden = !(t && open);
    if (!t || !open) return;
    // the queue has no "last step" to compare against: that tab is the Mix view's
    const queue = S.view === 'nowplaying';
    const ch = d.querySelector('[data-ttab="changes"]'); if (ch) ch.hidden = queue;
    if (queue && tab === 'changes') tab = 'path';
    for (const b of d.querySelectorAll('[data-ttab]')) b.classList.toggle('on', b.dataset.ttab === tab);
    $('tracePath').hidden = tab !== 'path';
    $('traceLeft').hidden = tab !== 'left';
    $('traceChanges').hidden = tab !== 'changes';
    $('traceLeftN').textContent = (t.left_out || []).length || '';
    if (tab === 'path') $('tracePath').innerHTML = pathHtml(t);
    if (tab === 'left') $('traceLeft').innerHTML = leftHtml(t);
    if (tab === 'changes') $('traceChanges').innerHTML = changesHtml();
  }

  function recipeHtml(t) {
    const r = t.recipe || {};
    if (r.sound_only) {
      return `<span class="tchip">Sound fingerprint alone</span>` +
        `<span class="hint">a Blend, an Adventure and steering compare sound only; the dials do not act on them</span>`;
    }
    const chips = (r.terms || []).map(x =>
      `<span class="tchip" title="${esc(x.label)}: weight ${x.weight}">${esc(TERM[x.id] || x.label)} <b>×${x.weight}</b></span>`).join('');
    const scale = r.fusion === 'z' ? 'each ingredient as distance from its own library average, in units of its own spread'
      : r.fusion === 'rank' ? 'each ingredient as its percentile in the library'
      : 'the raw numbers added together';
    const prof = r.profile === 'sound' ? 'the sound profile' : 'the V2 recipe';
    return chips + `<span class="hint">${esc(prof)}; ${esc(scale)}</span>`;
  }

  function pathHtml(t) {
    const stages = (t.stages || []).map(s =>
      `<li class="${s.on ? 'on' : ''}"><b>${esc(s.label)}</b><span>${esc(s.note || '')}</span></li>`).join('');
    let seq = '';
    const tr_ = t.transitions || [];
    if (tr_.length) {
      const order = t.order || [];
      let worst = 0;
      for (let k = 1; k < tr_.length; k++) if (tr_[k] < tr_[worst]) worst = k;
      const lo = Math.min(...tr_), hi = Math.max(...tr_);
      const bars = tr_.map((v, k) => {
        const h = hi > lo ? 15 + 85 * (v - lo) / (hi - lo) : 60;
        const a = label(order[k]), b = label(order[k + 1]);
        return `<i class="${k === worst ? 'worst' : ''}" style="height:${h.toFixed(0)}%" title="${esc(`#${k + 1} → #${k + 2}: ${fmt3(v)}\n${a}\n→ ${b}`)}"></i>`;
      }).join('');
      const kind = t.kind === 'mix' ? 'in the space the recipe scores in' : 'by sound fingerprint';
      seq = `<div class="tsection"><div class="tk">How each song leads into the next</div>` +
        `<div class="tseq">${bars}</div>` +
        `<div class="hint">closeness between neighbours, ${esc(kind)}; weakest hand-off at ` +
        `#${worst + 1} → #${worst + 2} (${fmt3(tr_[worst])}): ${esc(label(order[worst]))} → ${esc(label(order[worst + 1]))}` +
        (t.stages.some(s => s.id === 'flow') ? '' : '. Arrange for flow (Mix options) re-orders by this.') + `</div></div>`;
    }
    let extra = '';
    if (t.kind === 'blend' && t.cohesion != null) {
      extra = `<div class="hint">The seeds agree with each other at ${fmt2(t.cohesion)} (1 = the same sound, 0 = unrelated); ` +
        `the list is ranked against their shared centre.</div>`;
    }
    return `<div class="tsection"><ol class="tchain">${stages}</ol>${extra}</div>` +
      `<div class="tsection"><div class="tk">What the score is made of</div><div class="trecipe">${recipeHtml(t)}</div></div>` + seq;
  }

  function leftHtml(t) {
    const rows = t.left_out || [];
    if (!rows.length) return `<div class="hint tpad">The walk took every song it reached. Nothing was passed over.</div>`;
    const lo = fitFloor();
    const body = rows.map(r => {
      const w = r.fit == null ? 0 : Math.max(0, Math.min(1, (r.fit - lo) / (1 - lo)));
      return `<tr data-i="${r.i}"><td class="tfit"><span class="fitm"><i style="width:${Math.round(w * 100)}%"></i></span><b>${fmt2(r.fit)}</b></td>` +
        `<td class="tname">${esc(r.label)}</td><td class="trank">${r.rank ? '#' + r.rank.toLocaleString() : ''}</td>` +
        `<td class="twhy">${esc(WHY[r.why] || r.why)}</td></tr>`;
    }).join('');
    return `<div class="hint tpad">Songs the walk reached and did not keep, best first. Ranked out of ${t.pool.toLocaleString()}.</div>` +
      `<table class="tleft"><tbody>${body}</tbody></table>`;
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
      return `<div class="hint tpad">Nothing to compare yet. Vote on a song, remove one, block an artist or change the dials, and this shows what the re-mix kept, dropped and added.</div>`;
    }
    const d = diff(prev, S.mix);
    const li = (ids, cls) => ids.map(i => `<li class="${cls}">${esc(prevLabels.get(i) || label(i))}</li>`).join('');
    const step = S.prevStep ? esc(S.prevStep) : 'the last step';
    return `<div class="tpad"><b>After ${step}:</b> kept ${d.kept.length}, new ${d.added.length}, gone ${d.gone.length}` +
      (d.moved ? `, ${d.moved} of the kept changed place` : '') + `</div>` +
      `<div class="tcols"><div><div class="tk">Gone (${d.gone.length})</div><ul class="tlist">${li(d.gone, 'gone') || '<li class="hint">none</li>'}</ul></div>` +
      `<div><div class="tk">New (${d.added.length})</div><ul class="tlist">${li(d.added, 'new') || '<li class="hint">none</li>'}</ul></div></div>`;
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
  function onView() {
    if (S.view === 'mix' || S.view === 'nowplaying') return;
    const d = $('traceDrawer'); if (d) d.hidden = true;
    const lt = $('lTrace'); if (lt) lt.hidden = true;
  }

  /* ---- Why this pick: the one-song view */
  async function showWhy(i, x, y) {
    if (S.seed == null) return toast('Only available inside a mix', true);
    if (S.stats.engine !== 'v2') return toast('Why-this-pick needs the V2 engine', true);
    const t = S.trace || {};
    const p = new URLSearchParams({ seed: S.seed, cand: i });
    if (S.kind === 'blend' && S.seeds.length >= 2) p.set('seeds', S.seeds.join(','));
    if (S.liked.length) p.set('liked', S.liked.join(','));
    if (S.disliked.length) p.set('disliked', S.disliked.join(','));
    try {
      const j = await jget('/api/explain?' + p);
      const s = (t.songs || {})[i] || {};
      const el = $('why');
      let head = `<h4>${esc(label(i))}</h4>`;
      const facts = [];
      if (s.rank) facts.push(`ranked ${s.rank.toLocaleString()} of ${(t.pool || 0).toLocaleString()}`);
      if (s.fit != null) facts.push(`fit ${fmt3(s.fit)}` + (t.line != null ? ` (line ${fmt2(t.line)})` : ''));
      if (s.past) facts.push('taken past the line so the list is not empty');
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
      const b = e.target.closest('.tstage'); if (!b) return;
      show('path');
      const li = document.querySelector(`#tracePath .tchain li.on`);
      if (li) li.scrollIntoView({ block: 'nearest' });
    });
    const d = $('traceDrawer');
    if (d) {
      d.addEventListener('click', e => {
        const tb = e.target.closest('[data-ttab]');
        if (tb) { tab = tb.dataset.ttab; store.set('traceTab', tab); paint(); return; }
        if (e.target.closest('#traceClose')) return toggle(false);
        const tr_ = e.target.closest('tr[data-i]');
        if (tr_) showWhy(+tr_.dataset.i, e.clientX, e.clientY);
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
  }

  return { paint, strip, fitCell, toggle, show, onView, showWhy, noteStep, noteRows, diff, bind,
           get open() { return open; }, WHY, TERM };
})();
