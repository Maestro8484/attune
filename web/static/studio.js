/* Attune Studio — client.
   Views: library | mix | nowplaying | playlist.  One selection model, one table renderer. */
'use strict';

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = n => Number(n).toLocaleString();
const hms = s => {                       // 3:07 · 51:40 · 2:03:02
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = s % 60;
  const p = n => String(n).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(sec)}` : `${m}:${p(sec)}`;
};

const S = {
  stats: null,
  view: 'library',
  rows: [],            // rows currently displayed
  total: 0,
  offset: 0,
  limit: 200,
  sort: 'artist',
  desc: false,
  sel: new Set(),      // selected pool indices
  anchor: null,
  seed: null,          // seed of the current mix
  mix: [],
  np: [],              // now playing queue (pool indices)
  npAt: -1,
  playlist: null,
  liked: [],
  disliked: [],
  facets: { genre: new Set(), artist: new Set(), album: new Set() },
  q: '',
};

/* ------------------------------------------------------------------ net */
async function jget(url) {
  const r = await fetch(url);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
async function jpost(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
function toast(msg, err) {
  const t = $('toast');
  t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => t.hidden = true, 3200);
}

/* ------------------------------------------------------------------ params */
function mixParams() {
  const p = new URLSearchParams();
  // /api/mix clamps size to 100 server-side; clamp here too so the number in the box is
  // never a promise the server won't keep. (Minutes mode overrides this in doMix.)
  p.set('size', Math.min(Math.max(+$('mixSize').value || 25, 1), 100));
  if ($('dedupOn').checked) p.set('dedup', $('dedupField').value);
  if (S.stats && S.stats.engine === 'musicip') {
    p.set('style', $('style').value);
    p.set('variety', $('variety').value);
  } else {
    for (const k of ['clap', 'lib', 'genre', 'bpm', 'era']) p.set(k, $(k).value);
    if ($('mmr').checked) p.set('variety', '1');
    if ($('flow').checked) p.set('flow', '1');
  }
  return p;
}
function facetQS() {
  const p = new URLSearchParams();
  for (const g of S.facets.genre) p.append('genre', g);
  for (const a of S.facets.artist) p.append('artist', a);
  for (const b of S.facets.album) p.append('album', b);
  if (S.q) p.set('q', S.q);
  return p;
}

/* ------------------------------------------------------------------ table */
function renderRows(rows, opts = {}) {
  S.rows = rows;
  const tb = $('tbody');
  const seed = opts.seed ?? null;
  tb.innerHTML = rows.map(r => {
    const cls = [];
    if (S.sel.has(r.i)) cls.push('sel');
    if (S.np[S.npAt] === r.i) cls.push('playing');
    if (seed !== null && r.i === seed) cls.push('seed');
    if (r.missing) cls.push('missing');
    const ok = r.status === 'Analyzed';
    return `<tr data-i="${r.i}" class="${cls.join(' ')}">
      <td class="c-track">${r.track || ''}</td>
      <td class="c-title">${esc(r.title)}</td>
      <td class="c-len">${r.length}</td>
      <td class="c-artist">${esc(r.artist)}</td>
      <td class="c-album">${esc(r.album)}</td>
      <td class="c-year">${r.year || ''}</td>
      <td class="c-status${ok ? '' : ' no'}">${esc(r.status)}</td>
    </tr>`;
  }).join('');
  $('empty').hidden = rows.length > 0;
  updateSelStatus();
}

function setHeaderSort() {
  document.querySelectorAll('thead th').forEach(th => {
    th.classList.toggle('sorted', th.dataset.sort === S.sort);
    th.classList.toggle('asc', th.dataset.sort === S.sort && !S.desc);
  });
}

/* ------------------------------------------------------------------ views */
async function loadLibrary(resetOffset) {
  if (resetOffset) S.offset = 0;
  S.view = 'library';
  $('viewLabel').textContent = 'Library';
  $('btnBackLib').hidden = true;
  markTree('library');
  const p = facetQS();
  p.set('sort', S.sort); if (S.desc) p.set('desc', '1');
  p.set('offset', S.offset); p.set('limit', S.limit);
  const j = await jget('/api/lib/tracks?' + p);
  S.total = j.total;
  renderRows(j.rows);
  setHeaderSort();
  const hrs = (j.seconds / 3600).toFixed(1);
  $('viewSub').textContent = `${fmt(j.total)} songs · ${hrs} h`;
  renderPager();
  loadFacets();
}

function renderPager() {
  const pg = $('pager');
  if (S.view !== 'library' || S.total <= S.limit) { pg.innerHTML = ''; return; }
  const page = Math.floor(S.offset / S.limit) + 1;
  const pages = Math.ceil(S.total / S.limit);
  pg.innerHTML = `
    <button id="pPrev" ${S.offset === 0 ? 'disabled' : ''}>‹ Prev</button>
    <span>Page ${page} of ${fmt(pages)}</span>
    <button id="pNext" ${S.offset + S.limit >= S.total ? 'disabled' : ''}>Next ›</button>
    <span class="spacer"></span>
    <span>showing ${fmt(S.offset + 1)}–${fmt(Math.min(S.offset + S.limit, S.total))}</span>`;
  $('pPrev').onclick = () => { S.offset -= S.limit; loadLibrary(); };
  $('pNext').onclick = () => { S.offset += S.limit; loadLibrary(); };
}

async function loadFacets() {
  const j = await jget('/api/lib/facets?' + facetQS());
  const paint = (elId, list, kind, countId) => {
    $(countId).textContent = fmt(list.length);
    $(elId).innerHTML = list.map(x =>
      `<li data-v="${esc(x.name)}" class="${S.facets[kind].has(x.name) ? 'on' : ''}">
         <span class="nm">${esc(x.name)}</span><span class="n">${fmt(x.n)}</span></li>`).join('');
  };
  paint('fGenre', j.genres, 'genre', 'gN');
  paint('fArtist', j.artists, 'artist', 'aN');
  paint('fAlbum', j.albums, 'album', 'bN');
}

async function doMix(seedI) {
  if (seedI == null) return toast('Select a track first', true);
  const byMinutes = $('sizeType').value === 'minutes';
  const want = Math.max(1, +$('mixSize').value || 25);
  const p = mixParams();
  p.set('i', seedI);
  // /api/mix only speaks track counts (and caps at 100). For a minutes-length mix, ask for
  // the cap and trim by cumulative duration below -- the control used to do nothing at all.
  if (byMinutes) p.set('size', 100);
  $('btnMix').disabled = true;
  try {
    const j = await jget('/api/mix?' + p);
    let ids = (j.tracks || []).map(x => x.i).filter(i => i !== seedI);
    S.seed = seedI;
    if (byMinutes) {
      const rows = await jget('/api/lib/rows?' + [seedI, ...ids].map(i => `i=${i}`).join('&'));
      const sec = new Map(rows.rows.map(r => [r.i, r.seconds]));
      const budget = want * 60;
      let total = sec.get(seedI) || 0;
      const kept = [];
      for (const i of ids) {
        const d = sec.get(i) || 0;
        if (total + d > budget && kept.length) break;
        kept.push(i); total += d;
      }
      ids = kept;
      if (!ids.length) toast('No tracks fit that time budget', true);
    }
    S.mix = [seedI, ...ids];
    $('mixN').textContent = S.mix.length;
    await showMix();
  } catch (e) { toast(e.message, true); }
  finally { $('btnMix').disabled = false; }
}

async function showMix() {
  S.view = 'mix'; markTree('mix');
  $('viewLabel').textContent = 'Mix';
  $('btnBackLib').hidden = false;
  $('pager').innerHTML = '';
  if (!S.mix.length) { renderRows([]); return; }
  const j = await jget('/api/lib/rows?' + S.mix.map(i => `i=${i}`).join('&'));
  const byI = new Map(j.rows.map(r => [r.i, r]));
  const rows = S.mix.map(i => byI.get(i)).filter(Boolean);
  renderRows(rows, { seed: S.seed });
  const secs = rows.reduce((a, r) => a + r.seconds, 0);
  const sr = byI.get(S.seed);
  $('viewSub').textContent = `${rows.length} tracks · ${hms(secs)} · seed: ${sr ? sr.artist + ' — ' + sr.title : '?'}`;
}

async function showNowPlaying() {
  S.view = 'nowplaying'; markTree('nowplaying');
  $('viewLabel').textContent = 'Now Playing';
  $('btnBackLib').hidden = false; $('pager').innerHTML = '';
  if (!S.np.length) { renderRows([]); $('viewSub').textContent = 'queue is empty'; return; }
  const j = await jget('/api/lib/rows?' + S.np.map(i => `i=${i}`).join('&'));
  const byI = new Map(j.rows.map(r => [r.i, r]));
  renderRows(S.np.map(i => byI.get(i)).filter(Boolean));
  $('viewSub').textContent = `${S.np.length} queued`;
}

async function showPlaylist(name) {
  S.view = 'playlist'; S.playlist = name;
  document.querySelectorAll('#tree li').forEach(l => l.classList.remove('on'));
  document.querySelectorAll('#plList li').forEach(l =>
    l.classList.toggle('on', l.dataset.name === name));
  $('viewLabel').textContent = name;
  $('btnBackLib').hidden = false; $('pager').innerHTML = '';
  try {
    const j = await jget('/api/playlist?name=' + encodeURIComponent(name));
    const rows = j.rows.slice();
    for (const m of j.missing) rows.push({ i: -1, track: 0, title: m, length: '—',
      artist: '(not in library)', album: '', year: '', status: '—', seconds: 0, missing: true });
    renderRows(rows);
    const secs = j.rows.reduce((a, r) => a + r.seconds, 0);
    $('viewSub').textContent = `${j.rows.length} tracks · ${hms(secs)}` +
      (j.missing.length ? ` · ${j.missing.length} not found` : '');
  } catch (e) { toast(e.message, true); }
}

async function loadPlaylists() {
  const j = await jget('/api/playlists');
  $('plN').textContent = j.playlists.length;
  $('plList').innerHTML = j.playlists.map(p =>
    `<li data-name="${esc(p.name)}" title="${esc(p.name)}"><span class="ti">≡</span>${esc(p.name)}</li>`).join('');
  $('exportDir').textContent = j.dir ? 'Folder: ' + j.dir : 'No playlist folder configured (--playlists)';
  $('btnSaveDir').disabled = !j.dir;
}

function markTree(v) {
  document.querySelectorAll('#tree li[data-view]').forEach(l =>
    l.classList.toggle('on', l.dataset.view === v));
  document.querySelectorAll('#plList li').forEach(l => l.classList.remove('on'));
}

/* ------------------------------------------------------------------ selection */
function updateSelStatus() {
  const sel = S.rows.filter(r => S.sel.has(r.i));
  if (!sel.length) { $('sSel').textContent = 'No selection'; }
  else {
    const secs = sel.reduce((a, r) => a + r.seconds, 0);
    $('sSel').textContent = `${sel.length} selected · ${hms(secs)}`;
  }
  $('exportCount').textContent = `${currentExportIds().length} tracks`;
}
function currentExportIds() {
  if (S.view === 'mix' && S.mix.length) return S.mix;
  if (S.sel.size) return S.rows.filter(r => S.sel.has(r.i)).map(r => r.i);
  return S.rows.filter(r => r.i >= 0).map(r => r.i);
}
function firstSelected() {
  for (const r of S.rows) if (S.sel.has(r.i) && r.i >= 0) return r.i;
  return null;
}

/* ------------------------------------------------------------------ playback */
function playIndex(qpos) {
  if (qpos < 0 || qpos >= S.np.length) return;
  S.npAt = qpos;
  const i = S.np[qpos];
  const a = $('audio');
  a.src = `/audio?i=${i}`;
  a.play().catch(() => {});
  $('tPlay').textContent = '⏸';
  const img = new Image();
  img.onload = () => { $('art').innerHTML = ''; $('art').appendChild(img); };
  img.onerror = () => { $('art').innerHTML = '<span class="noart">♪</span>'; };
  img.src = `/api/art?i=${i}`;
  jget('/api/lib/rows?i=' + i).then(j => {
    const r = j.rows[0]; if (!r) return;
    $('npTitle').textContent = r.title;
    $('npArtist').textContent = `${r.artist} — ${r.album}`;
  }).catch(() => {});
  renderRows(S.rows, { seed: S.seed });
}
function playTrack(i) {
  const at = S.np.indexOf(i);
  if (at >= 0) return playIndex(at);
  S.np.push(i); $('npN').textContent = S.np.length; playIndex(S.np.length - 1);
}

/* ------------------------------------------------------------------ export */
async function exportSaveDir() {
  const ids = currentExportIds();
  if (!ids.length) return toast('Nothing to export', true);
  const name = $('plName').value.trim() || 'Attune mix';
  try {
    const j = await jpost('/api/export/m3u_dir',
      { ids, name, flavor: $('flavor').value });
    $('exportMsg').className = 'msg ok';
    $('exportMsg').textContent = `Wrote ${j.count} tracks → ${j.name}`;
    loadPlaylists();
  } catch (e) { $('exportMsg').className = 'msg err'; $('exportMsg').textContent = e.message; }
}
function exportDownload() {
  const seed = S.seed ?? firstSelected();
  if (seed == null) return toast('Create a mix first', true);
  const p = mixParams(); p.set('i', seed); p.set('flavor', $('flavor').value);
  window.location = '/api/export/m3u?' + p;
}
async function exportPlex() {
  const seed = S.seed ?? firstSelected();
  if (seed == null) return toast('Create a mix first', true);
  const p = mixParams(); p.set('i', seed);
  $('exportMsg').className = 'msg'; $('exportMsg').textContent = 'Creating Plex playlist…';
  try {
    const j = await jpost('/api/export/plex?' + p, {});
    $('exportMsg').className = 'msg ok';
    $('exportMsg').textContent = `Plex: ${j.added ?? j.count ?? '?'} added` +
      (j.missed && j.missed.length ? `, ${j.missed.length} missed` : '');
  } catch (e) { $('exportMsg').className = 'msg err'; $('exportMsg').textContent = e.message; }
}

/* ------------------------------------------------------------------ why-this-pick */
async function showWhy(i, x, y) {
  if (S.seed == null) return toast('Only available inside a mix', true);
  if (S.stats.engine !== 'v2') return toast('Why-this-pick needs --engine v2', true);
  try {
    // app.py's /api/explain takes seed= and cand=, and returns {seed, cand, ...components}
    const j = await jget(`/api/explain?seed=${S.seed}&cand=${i}`);
    const c = Object.fromEntries(Object.entries(j)
      .filter(([k, v]) => typeof v === 'number'));
    const max = Math.max(...Object.values(c).map(v => Math.abs(v)), 0.001);
    const el = $('why');
    el.innerHTML = `<h4>Why this pick?</h4>` + Object.entries(c).map(([k, v]) => `
      <div class="bar"><span class="lbl">${esc(k)}</span>
        <span class="track"><span class="fill ${v < 0 ? 'neg' : ''}"
          style="width:${Math.abs(v) / max * 100}%"></span></span>
        <span class="v">${v.toFixed(3)}</span></div>`).join('');
    el.style.left = Math.min(x, innerWidth - 360) + 'px';
    el.style.top = Math.min(y, innerHeight - 220) + 'px';
    el.hidden = false;
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ wiring */
function bindSliders() {
  const link = (id, dp) => {
    const el = $(id), out = $(id + 'V');
    if (!el || !out) return;
    const upd = () => out.textContent = dp ? Number(el.value).toFixed(dp) : el.value;
    el.addEventListener('input', upd); upd();
  };
  ['style', 'variety'].forEach(i => link(i, 0));
  ['clap', 'lib', 'genre', 'bpm', 'era'].forEach(i => link(i, 2));
}

function bindEvents() {
  // table rows
  $('tbody').addEventListener('click', e => {
    const tr = e.target.closest('tr'); if (!tr) return;
    const i = +tr.dataset.i; if (i < 0) return;
    if (e.shiftKey && S.anchor != null) {
      const ids = S.rows.map(r => r.i);
      const a = ids.indexOf(S.anchor), b = ids.indexOf(i);
      if (a >= 0 && b >= 0) {
        if (!e.ctrlKey) S.sel.clear();
        for (let k = Math.min(a, b); k <= Math.max(a, b); k++) S.sel.add(ids[k]);
      }
    } else if (e.ctrlKey || e.metaKey) {
      S.sel.has(i) ? S.sel.delete(i) : S.sel.add(i); S.anchor = i;
    } else { S.sel.clear(); S.sel.add(i); S.anchor = i; }
    renderRows(S.rows, { seed: S.seed });
  });
  $('tbody').addEventListener('dblclick', e => {
    const tr = e.target.closest('tr'); if (!tr) return;
    const i = +tr.dataset.i; if (i >= 0) playTrack(i);
  });

  // sorting
  document.querySelectorAll('thead th').forEach(th => th.addEventListener('click', () => {
    const s = th.dataset.sort;
    if (S.sort === s) S.desc = !S.desc; else { S.sort = s; S.desc = false; }
    if (S.view === 'library') loadLibrary(true);
    else {
      const keyf = { track: r => r.track, title: r => r.title.toLowerCase(),
        length: r => r.seconds, artist: r => r.artist.toLowerCase(),
        album: r => r.album.toLowerCase(), year: r => r.year || 0,
        status: r => r.status }[s];
      const rows = S.rows.slice().sort((a, b) =>
        keyf(a) < keyf(b) ? -1 : keyf(a) > keyf(b) ? 1 : 0);
      if (S.desc) rows.reverse();
      // keep S.mix in step with what's on screen -- currentExportIds() reads S.mix in the
      // mix view, so a stale S.mix would export a different order than the user is looking at
      if (S.view === 'mix') S.mix = rows.map(r => r.i);
      renderRows(rows, { seed: S.seed }); setHeaderSort();
    }
  }));

  // facets
  for (const [elId, kind] of [['fGenre', 'genre'], ['fArtist', 'artist'], ['fAlbum', 'album']]) {
    $(elId).addEventListener('click', e => {
      const li = e.target.closest('li'); if (!li) return;
      const v = li.dataset.v;
      if (!e.ctrlKey && !e.metaKey) {
        const only = S.facets[kind].size === 1 && S.facets[kind].has(v);
        S.facets[kind].clear(); if (!only) S.facets[kind].add(v);
      } else { S.facets[kind].has(v) ? S.facets[kind].delete(v) : S.facets[kind].add(v); }
      loadLibrary(true);
    });
  }

  // tree
  $('tree').addEventListener('click', e => {
    const li = e.target.closest('li[data-view]'); if (!li) return;
    ({ library: () => loadLibrary(true), mix: showMix, nowplaying: showNowPlaying })[li.dataset.view]();
  });
  $('plList').addEventListener('click', e => {
    const li = e.target.closest('li'); if (li) showPlaylist(li.dataset.name);
  });
  $('btnBackLib').onclick = () => loadLibrary(false);

  // search (debounced)
  let t; $('q').addEventListener('input', e => {
    clearTimeout(t); t = setTimeout(() => { S.q = e.target.value.trim(); loadLibrary(true); }, 220);
  });

  // toolbar
  $('btnMix').onclick = () => doMix(firstSelected());
  $('btnShuffle').onclick = () => {
    const rows = S.rows.slice();
    for (let i = rows.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1)); [rows[i], rows[j]] = [rows[j], rows[i]];
    }
    if (S.view === 'mix') S.mix = rows.map(r => r.i);
    renderRows(rows, { seed: S.seed });
  };
  const pop = (btn, panel) => $(btn).onclick = () => {
    const p = $(panel); const other = panel === 'optionsPanel' ? 'exportPanel' : 'optionsPanel';
    $(other).hidden = true; p.hidden = !p.hidden;
    if (!p.hidden) updateSelStatus();
  };
  pop('btnOptions', 'optionsPanel'); pop('btnExport', 'exportPanel');
  const showRoot = () => {
    const r = (S.stats && S.stats.roots) || {};
    const v = r[$('flavor').value];
    $('flavorRoot').textContent = v ? 'Paths will read: ' + v + '\…' : 'Not configured in .env';
  };
  $('flavor').addEventListener('change', showRoot);
  showRoot._run = showRoot;
  $('btnSaveDir').onclick = exportSaveDir;
  $('btnDownload').onclick = exportDownload;
  $('btnPlex').onclick = exportPlex;

  document.querySelectorAll('.chip[data-preset]').forEach(b => b.onclick = () => {
    const w = b.dataset.preset === 'nobpm'
      ? { clap: 1.0, lib: 0.4, genre: 0.3, bpm: 0.0, era: 0.1 }
      : { clap: 1.0, lib: 0.4, genre: 0.3, bpm: 0.3, era: 0.1 };
    for (const [k, v] of Object.entries(w)) { $(k).value = v; $(k).dispatchEvent(new Event('input')); }
  });

  // context menu
  const ctx = $('ctx');
  $('tbody').addEventListener('contextmenu', e => {
    const tr = e.target.closest('tr'); if (!tr) return;
    e.preventDefault();
    const i = +tr.dataset.i; if (i < 0) return;
    if (!S.sel.has(i)) { S.sel.clear(); S.sel.add(i); S.anchor = i; renderRows(S.rows, { seed: S.seed }); }
    ctx.dataset.i = i;
    ctx.style.left = Math.min(e.clientX, innerWidth - 230) + 'px';
    ctx.style.top = Math.min(e.clientY, innerHeight - 320) + 'px';
    ctx.hidden = false;
  });
  ctx.addEventListener('click', async e => {
    const li = e.target.closest('li[data-act]'); if (!li) return;
    const i = +ctx.dataset.i; ctx.hidden = true;
    const row = S.rows.find(r => r.i === i) || {};
    switch (li.dataset.act) {
      case 'mix': doMix(i); break;
      case 'play': playTrack(i); break;
      case 'queue': S.np.push(i); $('npN').textContent = S.np.length; toast('Queued'); break;
      case 'more': S.liked.push(i); refine(); break;
      case 'less': S.disliked.push(i); refine(); break;
      case 'artist': S.facets.artist = new Set([row.artist]); loadLibrary(true); break;
      case 'album': S.facets.album = new Set([row.album]); loadLibrary(true); break;
      case 'genre': S.facets.genre = new Set([(row.genre || '').split(/[;,]/)[0].trim()]);
        loadLibrary(true); break;
      case 'why': showWhy(i, e.clientX, e.clientY); break;
      case 'copy': navigator.clipboard.writeText(`${row.artist} — ${row.title}`)
        .then(() => toast('Copied')); break;
      case 'reveal': toast('Open File Location needs the desktop build'); break;
    }
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('.ctxmenu')) $('ctx').hidden = true;
    if (!e.target.closest('.why')) $('why').hidden = true;
    if (!e.target.closest('.popover') && !e.target.closest('#toolbar button'))
      { $('optionsPanel').hidden = true; $('exportPanel').hidden = true; }
  });

  // keyboard
  document.addEventListener('keydown', e => {
    if (e.target.matches('input,select,textarea')) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'm') { e.preventDefault(); doMix(firstSelected()); }
    else if (e.key === ' ') { e.preventDefault(); $('tPlay').click(); }
    else if (e.key === 'Escape') { $('ctx').hidden = true; $('why').hidden = true; }
  });

  // transport
  const a = $('audio');
  $('tPlay').onclick = () => {
    if (!a.src) { const i = firstSelected(); if (i != null) return playTrack(i); return; }
    if (a.paused) { a.play(); $('tPlay').textContent = '⏸'; }
    else { a.pause(); $('tPlay').textContent = '▶'; }
  };
  $('tPrev').onclick = () => playIndex(S.npAt - 1);
  $('tNext').onclick = () => playIndex(S.npAt + 1);
  a.addEventListener('ended', () => playIndex(S.npAt + 1));
  a.addEventListener('timeupdate', () => {
    if (!a.duration) return;
    $('tSeek').value = (a.currentTime / a.duration) * 1000;
    $('tCur').textContent = hms(a.currentTime);
    $('tDur').textContent = hms(a.duration);
  });
  $('tSeek').addEventListener('input', () => {
    if (a.duration) a.currentTime = ($('tSeek').value / 1000) * a.duration;
  });
  $('tVol').addEventListener('input', () => a.volume = +$('tVol').value);
  a.volume = 0.9;
}

async function refine() {
  if (S.seed == null) return toast('Create a mix first', true);
  if (S.stats.engine !== 'v2') return toast('More/Less Like This needs --engine v2', true);
  try {
    // /api/refine is a POST with everything in the JSON body (no query params)
    const j = await jpost('/api/refine', {
      i: S.seed, size: Math.min(+$('mixSize').value || 25, 100),
      liked: S.liked, disliked: S.disliked,
    });
    const ids = (j.tracks || []).map(x => x.i);
    S.mix = [S.seed, ...ids.filter(i => i !== S.seed)];
    $('mixN').textContent = S.mix.length;
    showMix(); toast(`Refined (+${S.liked.length} / −${S.disliked.length})`);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ boot */
(async function init() {
  bindSliders(); bindEvents();
  try {
    S.stats = await jget('/api/lib/stats');
  } catch (e) { return toast('Cannot reach server: ' + e.message, true); }
  const s = S.stats;
  $('engineName').textContent = s.engine === 'musicip' ? 'MusicIP Mixer (live)' : 'Attune V2';
  $('mipControls').hidden = s.engine !== 'musicip';
  $('v2Controls').hidden = s.engine === 'musicip';
  $('btnPlex').disabled = !s.plex;
  $('sLib').textContent = `Songs: ${fmt(s.songs)} (${fmt(s.analyzed)} analyzed)`;
  $('sTot').textContent = `${fmt(s.songs)} songs · ${s.gb} GB · ${s.hours} h · ` +
    `${fmt(s.genres)} genres · ${fmt(s.artists)} artists · ${fmt(s.albums)} albums`;
  const fr = $('flavor'); fr.dispatchEvent(new Event('change'));
  await loadPlaylists();
  await loadLibrary(true);
})();
