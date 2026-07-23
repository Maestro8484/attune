/* Attune Studio — core client: state, views, table, facets, selection, context menu,
   export. Playback lives in player.js (Player), preferences/tag-editor in prefs.js
   (Prefs), boot order in boot.js. Plain scripts sharing globals, loaded in that order. */
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
/* persisted UI state (per-browser; server settings live in settings.json) */
const store = {
  get(k, dflt) {
    try { const v = localStorage.getItem('attune.' + k);
      return v === null ? dflt : JSON.parse(v); } catch { return dflt; }
  },
  set(k, v) { try { localStorage.setItem('attune.' + k, JSON.stringify(v)); } catch {} },
};

const S = {
  stats: null,
  view: 'library',
  rows: [],            // rows currently displayed
  total: 0,
  offset: 0,
  limit: 200,
  loading: false,      // infinite-scroll fetch in flight
  sort: 'artist',
  desc: false,
  sel: new Set(),      // selected pool indices
  anchor: null,
  seed: null,          // seed of the current mix
  mix: [],
  playlist: null,
  liked: [],
  disliked: [],
  facets: { genre: new Set(), artist: new Set(), album: new Set() },
  q: '',
  smart: '',           // active Smart View id ('' = normal library)
  viewMode: store.get('viewMode', 'list'),   // list | grid (album cards)
  folder: null,        // active folder-tree path ('' none)
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
  // Mix length is 20–150 tracks (the server clamps to the same range); keep the box honest.
  // (Minutes mode overrides this in doMix.)
  p.set('size', Math.min(Math.max(+$('mixSize').value || 100, 20), 150));
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

/* ------------------------------------------------------------------ columns */
const COLS = [
  { id: 'track',  label: 'Track',  cls: 'c-track'  },
  { id: 'title',  label: 'Title',  cls: 'c-title'  },
  { id: 'length', label: 'Length', cls: 'c-len'    },
  { id: 'artist', label: 'Artist', cls: 'c-artist' },
  { id: 'album',  label: 'Album',  cls: 'c-album'  },
  { id: 'genre',  label: 'Genre',  cls: 'c-genre'  },
  { id: 'year',   label: 'Year',   cls: 'c-year'   },
  { id: 'rating', label: 'Rating', cls: 'c-rating' },
  { id: 'plays',  label: 'Plays',  cls: 'c-plays'  },
  { id: 'status', label: 'Status', cls: 'c-status' },
];
let visCols = new Set(store.get('cols',
  ['track', 'title', 'length', 'artist', 'album', 'year', 'rating', 'plays', 'status']));

function starsHtml(r, cls = 'stars') {
  let h = `<span class="${cls}" data-i="${r.i}">`;
  for (let n = 1; n <= 5; n++)
    h += `<i data-s="${n}" class="${n <= (r.rating || 0) ? 'on' : ''}">${n <= (r.rating || 0) ? '★' : '☆'}</i>`;
  return h + '</span>';
}

function cellHtml(c, r) {
  switch (c.id) {
    case 'track':  return r.track || '';
    case 'title':  return esc(r.title);
    case 'length': return r.length;
    case 'artist': return esc(r.artist);
    case 'album':  return esc(r.album);
    case 'genre':  return esc(r.genre || '');
    case 'year':   return r.year || '';
    case 'rating': return starsHtml(r);
    case 'plays':  return r.plays || '';
    case 'status': return esc(r.status);
  }
  return '';
}

function renderHead() {
  $('thead-row').innerHTML = COLS.filter(c => visCols.has(c.id)).map(c =>
    `<th data-sort="${c.id}" class="${c.cls}">${c.label}</th>`).join('');
  setHeaderSort();
}

/* ------------------------------------------------------------------ table */
function renderRows(rows, opts = {}) {
  S.rows = rows;
  // mix view shows the inline More/Less Like This (tune) buttons; every other view hides them
  document.querySelector('#tbl').classList.toggle('mixview', S.view === 'mix');
  const tb = $('tbody');
  tb.innerHTML = rowsHtml(rows, opts);
  $('empty').hidden = rows.length > 0;
  updateSelStatus();
}

/* inline thumbs for a mix row — same S.liked/S.disliked + refine() path as the
   context menu's More/Less Like This, just visible on the row itself */
function tuneHtml(i) {
  const lk = S.liked.includes(i), dk = S.disliked.includes(i);
  return `<span class="tune${lk || dk ? ' voted' : ''}" data-i="${i}">` +
    `<button class="tb more${lk ? ' on' : ''}" title="More Like This">+</button>` +
    `<button class="tb less${dk ? ' on' : ''}" title="Less Like This">−</button></span>`;
}
function rowsHtml(rows, opts = {}) {
  const seed = opts.seed ?? (S.view === 'mix' ? S.seed : null);
  const playing = (typeof Player !== 'undefined') ? Player.currentPool() : -1;
  const cols = COLS.filter(c => visCols.has(c.id));
  const draggable = S.view === 'nowplaying';
  return rows.map(r => {
    const cls = [];
    if (S.sel.has(r.i)) cls.push('sel');
    if (r.i === playing && r.i >= 0) cls.push('playing');
    if (seed !== null && r.i === seed) cls.push('seed');
    if (r.missing) cls.push('missing');
    const tune = S.view === 'mix' && S.stats.engine === 'v2' &&
                 r.i >= 0 && r.i !== seed;
    const tds = cols.map(c => {
      const extra = (c.id === 'status' && r.status !== 'Analyzed') ? ' no' : '';
      const cell = cellHtml(c, r) + (tune && c.id === 'title' ? tuneHtml(r.i) : '');
      return `<td class="${c.cls}${extra}">${cell}</td>`;
    }).join('');
    return `<tr data-i="${r.i}" ${draggable ? 'draggable="true"' : ''} class="${cls.join(' ')}">${tds}</tr>`;
  }).join('');
}
function appendRows(rows) {
  const opts = {};
  $('tbody').insertAdjacentHTML('beforeend', rowsHtml(rows, opts));
  S.rows = S.rows.concat(rows);
  updateSelStatus();
}
/* re-paint only row classes (selection/playing) without rebuilding cells */
function repaintRowState() {
  const playing = (typeof Player !== 'undefined') ? Player.currentPool() : -1;
  const seed = S.view === 'mix' ? S.seed : null;
  $('tbody').querySelectorAll('tr').forEach(tr => {
    const i = +tr.dataset.i;
    tr.classList.toggle('sel', S.sel.has(i));
    tr.classList.toggle('playing', i === playing && i >= 0);
    tr.classList.toggle('seed', seed !== null && i === seed);
  });
  updateSelStatus();
}

function setHeaderSort() {
  document.querySelectorAll('thead th').forEach(th => {
    th.classList.toggle('sorted', th.dataset.sort === S.sort);
    th.classList.toggle('asc', th.dataset.sort === S.sort && !S.desc);
  });
}

/* ------------------------------------------------------------------ views */
const SMART_LABELS = { loved: 'Loved', toprated: 'Top Rated', recent: 'Recently Added',
  mostplayed: 'Most Played', neverplayed: 'Never Played' };

function setTableMode(grid) {
  $('tableWrap').classList.toggle('gridmode', grid);
  document.querySelector('#tbl').hidden = grid;
  $('albumGrid').hidden = !grid;
  $('btnViewList').classList.toggle('on', !grid);
  $('btnViewGrid').classList.toggle('on', grid);
}

async function loadLibrary(resetOffset) {
  if (resetOffset) S.offset = 0;
  S.view = 'library';
  // loadLibrary is never the folder-view renderer (openFolder does its own fetch), so
  // reaching here means we've navigated away from any folder — clear the stale marker.
  S.folder = null;
  document.querySelectorAll('#folderTree li.on, #slList li.on').forEach(l => l.classList.remove('on'));
  // album-grid mode is a library-only presentation; delegate and stop.
  if (S.viewMode === 'grid') { setTableMode(true); return loadAlbums(); }
  setTableMode(false);
  const smartName = S.smart ? SMART_LABELS[S.smart] : 'Library';
  $('viewLabel').textContent = smartName;
  $('btnBackLib').hidden = true;
  $('queueTools').hidden = true;
  markTree(S.smart ? null : 'library');
  const p = facetQS();
  if (S.smart) p.set('smart', S.smart);
  // let smart views apply their own natural sort unless the user has clicked a header
  if (!(S.smart && !S._userSorted)) { p.set('sort', S.sort); if (S.desc) p.set('desc', '1'); }
  p.set('offset', S.offset); p.set('limit', S.limit);
  const j = await jget('/api/lib/tracks?' + p);
  S.total = j.total;
  renderRows(j.rows);
  $('tableWrap').scrollTop = 0;
  setHeaderSort();
  const hrs = (j.seconds / 3600).toFixed(1);
  $('viewSub').textContent = `${fmt(j.total)} songs · ${hrs} h`;
  renderPager();
  loadFacets();
}

function loadSmart(view) {
  S.smart = view;
  S.folder = null;
  S._userSorted = false;
  document.querySelectorAll('#tree li[data-smart]').forEach(l =>
    l.classList.toggle('on', l.dataset.smart === view));
  document.querySelectorAll('#tree li[data-view]').forEach(l => l.classList.remove('on'));
  document.querySelectorAll('#folderTree li').forEach(l => l.classList.remove('on'));
  document.querySelectorAll('#plList li').forEach(l => l.classList.remove('on'));
  loadLibrary(true);
}

/* -------- album grid -------- */
async function loadAlbums() {
  $('viewLabel').textContent = S.smart ? SMART_LABELS[S.smart] : 'Albums';
  $('btnBackLib').hidden = true; $('queueTools').hidden = true; $('pager').innerHTML = '';
  markTree(S.smart ? null : 'library');
  const p = facetQS();
  if (S.smart) p.set('smart', S.smart);
  const j = await jget('/api/lib/albums?' + p);
  const g = $('albumGrid');
  g.innerHTML = j.albums.map((a, k) => `
    <div class="acard" data-k="${k}">
      <div class="cover">
        <img loading="lazy" src="/api/art?i=${a.seed}" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'noart',textContent:'♪'}))">
        <button class="play" title="Play album" data-play="${k}">▶</button>
      </div>
      <div class="meta">
        <div class="al" title="${esc(a.album)}">${esc(a.album)}</div>
        <div class="ar" title="${esc(a.artist)}">${esc(a.artist)}</div>
        <div class="sub">${a.n} tracks · ${a.length}${a.year ? ' · ' + a.year : ''}</div>
      </div>
    </div>`).join('');
  $('empty').hidden = j.albums.length > 0;
  $('viewSub').textContent = `${fmt(j.total)} albums`;
  S._albums = j.albums;
  g.scrollTop = 0;
}

/* -------- folder tree (lazy) -------- */
async function toggleFolderChildren(li) {
  const path = li.dataset.path;
  const kids = li._kids;
  if (kids) {                                   // already expanded — collapse
    kids.forEach(k => k.remove());
    li._kids = null;
    li.querySelector('.tw').textContent = '▸';
    return;
  }
  const j = await jget('/api/lib/folders?path=' + encodeURIComponent(path));
  li.querySelector('.tw').textContent = '▾';
  const depth = (+li.dataset.depth || 0) + 1;
  const frag = j.folders.map(f => folderLi(f, depth));
  li._kids = frag;
  let anchor = li;
  for (const k of frag) { anchor.after(k); anchor = k; }
}
function folderLi(f, depth) {
  const li = document.createElement('li');
  li.dataset.path = f.path; li.dataset.depth = depth;
  li.style.paddingLeft = (6 + depth * 12) + 'px';
  li.innerHTML = `<span class="tw">▸</span><span class="fname">${esc(f.name)}</span>` +
    `<span class="fn">${fmt(f.n)}</span>`;
  return li;
}
async function loadFolderRoots() {
  const j = await jget('/api/lib/folders?path=');
  const ft = $('folderTree');
  ft.innerHTML = '';
  j.folders.forEach(f => ft.appendChild(folderLi(f, 0)));
}
async function openFolder(path) {
  S.folder = path; S.smart = '';
  S.facets = { genre: new Set(), artist: new Set(), album: new Set() };
  document.querySelectorAll('#folderTree li').forEach(l =>
    l.classList.toggle('on', l.dataset.path === path));
  document.querySelectorAll('#tree li[data-view],#tree li[data-smart]').forEach(l => l.classList.remove('on'));
  S.view = 'library';
  if (S.viewMode === 'grid') { S.viewMode = 'list'; store.set('viewMode', 'list'); setTableMode(false); }
  $('viewLabel').textContent = path.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || path;
  $('btnBackLib').hidden = false; $('queueTools').hidden = true; $('pager').innerHTML = '';
  try {
    const j = await jget('/api/lib/folders?path=' + encodeURIComponent(path));
    renderRows(j.rows);
    $('tableWrap').scrollTop = 0;
    const secs = j.rows.reduce((a, r) => a + r.seconds, 0);
    $('viewSub').textContent = `${j.rows.length} tracks · ${hms(secs)}` +
      (j.folders.length ? ` · ${j.folders.length} subfolders` : '');
  } catch (e) { toast(e.message, true); }
}

/* infinite scroll: pull the next page when the scroller nears the bottom */
async function maybeLoadMore() {
  if (S.view !== 'library' || S.viewMode === 'grid' || S.folder || S.loading) return;
  if (S.rows.length >= S.total) return;
  const w = $('tableWrap');
  if (w.scrollTop + w.clientHeight < w.scrollHeight - 600) return;
  S.loading = true;
  try {
    S.offset += S.limit;
    const p = facetQS();
    if (S.smart) p.set('smart', S.smart);
    p.set('sort', S.sort); if (S.desc) p.set('desc', '1');
    p.set('offset', S.offset); p.set('limit', S.limit);
    const j = await jget('/api/lib/tracks?' + p);
    appendRows(j.rows);
    renderPager();
  } catch (e) { toast(e.message, true); }
  finally { S.loading = false; }
}

function renderPager() {
  const pg = $('pager');
  if (S.view !== 'library' || S.total <= S.limit) { pg.innerHTML = ''; return; }
  pg.innerHTML = `<span>showing ${fmt(Math.min(S.rows.length, S.total))} of ${fmt(S.total)}</span>
    <span class="hint">— scroll for more</span>`;
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
  const want = Math.max(1, +$('mixSize').value || 100);
  const p = mixParams();
  p.set('i', seedI);
  // /api/mix speaks track counts (20–150). For a minutes-length mix, ask for the max and
  // trim by cumulative duration below.
  if (byMinutes) p.set('size', 150);
  $('btnMix').disabled = true;
  try {
    const j = await jget('/api/mix?' + p);
    let ids = (j.tracks || []).map(x => x.i).filter(i => i !== seedI);
    S.seed = seedI;
    S.liked = []; S.disliked = [];
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
  $('queueTools').hidden = true;
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
  $('queueTools').hidden = false;
  const q = Player.q;
  if (!q.length) { renderRows([]); $('viewSub').textContent = 'queue is empty'; return; }
  const j = await jget('/api/lib/rows?' + q.map(i => `i=${i}`).join('&'));
  const byI = new Map(j.rows.map(r => [r.i, r]));
  renderRows(q.map(i => byI.get(i)).filter(Boolean));
  const secs = S.rows.reduce((a, r) => a + r.seconds, 0);
  $('viewSub').textContent = `${q.length} queued · ${hms(secs)}`;
}

async function showPlaylist(name) {
  S.view = 'playlist'; S.playlist = name;
  document.querySelectorAll('#tree li').forEach(l => l.classList.remove('on'));
  document.querySelectorAll('#plList li').forEach(l =>
    l.classList.toggle('on', l.dataset.name === name));
  $('viewLabel').textContent = name;
  $('btnBackLib').hidden = false; $('pager').innerHTML = '';
  $('queueTools').hidden = true;
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
  $('exportDir').textContent = j.dir ? 'Folder: ' + j.dir : 'No playlist folder configured';
  $('btnSaveDir').disabled = !j.dir;
}

function markTree(v) {
  document.querySelectorAll('#tree li[data-view]').forEach(l =>
    l.classList.toggle('on', l.dataset.view === v));
  document.querySelectorAll('#plList li').forEach(l => l.classList.remove('on'));
}

function setViewMode(mode) {
  if (S.viewMode === mode) return;
  S.viewMode = mode; store.set('viewMode', mode);
  // grid is a library-only presentation; snap to Library if we're elsewhere
  if (mode === 'grid' && !['library'].includes(S.view)) { S.smart = ''; S.folder = null; }
  loadLibrary(true);
}

let _folderPaneLoaded = false;
async function toggleFolderPane() {
  const ft = $('folderTree');
  const open = ft.hidden;
  ft.hidden = !open;
  $('foldersTog').textContent = open ? '▾' : '▸';
  if (open && !_folderPaneLoaded) { _folderPaneLoaded = true; await loadFolderRoots(); }
}

function refreshView() {
  if (S.view === 'library') loadLibrary(false);
  else if (S.view === 'mix') showMix();
  else if (S.view === 'nowplaying') showNowPlaying();
  else if (S.view === 'playlist' && S.playlist) showPlaylist(S.playlist);
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
  // A smart/auto-playlist can match more than the 500 rows the table renders; export the
  // FULL id list (S._slIds) so saving a >500-track auto-playlist isn't silently truncated.
  if (S.view === 'smartlist' && S._slIds && S._slIds.length) return S._slIds;
  return S.rows.filter(r => r.i >= 0).map(r => r.i);
}
function firstSelected() {
  for (const r of S.rows) if (S.sel.has(r.i) && r.i >= 0) return r.i;
  return null;
}
function selectedIds() {
  return S.rows.filter(r => S.sel.has(r.i) && r.i >= 0).map(r => r.i);
}

/* ------------------------------------------------------------------ ratings */
async function rateTrack(i, rating) {
  try {
    await jpost('/api/track/rate', { i, rating });
    for (const r of S.rows) if (r.i === i) r.rating = rating;
    // repaint the one row's stars everywhere it appears
    $('tbody').querySelectorAll(`tr[data-i="${i}"] .stars`).forEach(el => {
      el.outerHTML = starsHtml({ i, rating });
    });
    if (Player.currentPool() === i) Player.paintNowPlayingMeta({ rating });
  } catch (e) { toast(e.message, true); }
}
async function loveTrack(i, loved) {
  try {
    await jpost('/api/track/loved', { i, loved });
    for (const r of S.rows) if (r.i === i) r.loved = loved;
    if (Player.currentPool() === i) Player.paintNowPlayingMeta({ loved });
    toast(loved ? '♥ Loved' : 'Un-loved');
  } catch (e) { toast(e.message, true); }
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
    $('exportMsg').textContent = `Plex: ${j.matched ?? j.count ?? '?'} added` +
      (j.missed && j.missed.length ? `, ${j.missed.length} missed` : '');
  } catch (e) { $('exportMsg').className = 'msg err'; $('exportMsg').textContent = e.message; }
}

/* -- Take It With You: copy the actual audio files (+ relative m3u8) to a folder/USB.
   Reuses the server-side folder picker (Prefs.pickFolder) and the same on-screen track
   list every other export uses (currentExportIds). Backend job = exportjob.py. */
let copyDest = '';
let copyTimer = 0;
function copyErr(m) { $('copyMsg').className = 'msg err'; $('copyMsg').textContent = m; }
async function copyBrowse() {
  const picked = await Prefs.pickFolder(copyDest || '');
  if (picked) { copyDest = picked; $('copyDest').value = picked; }
}
async function startCopy() {
  const ids = currentExportIds();
  if (!ids.length) return copyErr('Nothing to export');
  if (!copyDest) return copyErr('Pick a destination folder first');
  const folder = $('plName').value.trim() || 'Attune mix';
  const layout = $('copyLayout').value;
  $('copyMsg').className = 'msg'; $('copyMsg').textContent = 'Starting…';
  try {
    await jpost('/api/export/copy', { ids, dest: copyDest, folder, layout });
    startCopyPoll();
  } catch (e) { copyErr(e.message); }
}
async function cancelCopy() { try { await jpost('/api/export/copy/cancel'); } catch (e) { copyErr(e.message); } }
function paintCopy(st) {
  const pct = st.total ? Math.round(st.copied / st.total * 100) : 0;
  $('copyBar').hidden = !(st.running || st.copied);
  $('copyFill').style.width = pct + '%';
  $('btnCopy').hidden = st.running;
  $('btnCopyCancel').hidden = !st.running;
  if (st.running) {
    $('copyMsg').className = 'msg';
    $('copyMsg').textContent = `Copying ${st.copied}/${st.total}… ${st.current || ''}`.trim();
  } else if (copyTimer && st.done) {
    stopCopyPoll();
    if (st.error) return copyErr(st.error);
    const skip = st.skipped && st.skipped.length ? `, ${st.skipped.length} skipped` : '';
    if (st.cancelled) {
      $('copyMsg').className = 'msg';
      $('copyMsg').textContent = `Cancelled — ${st.copied} copied${skip}`;
    } else {
      $('copyMsg').className = 'msg ok';
      $('copyMsg').textContent = `Copied ${st.copied} track${st.copied === 1 ? '' : 's'}${skip} → ${st.dest}`;
    }
  }
}
async function pollCopy() {
  try { paintCopy(await jget('/api/export/copy/status')); } catch { /* transient */ }
}
function startCopyPoll() { if (copyTimer) return; copyTimer = setInterval(pollCopy, 800); pollCopy(); }
function stopCopyPoll() { clearInterval(copyTimer); copyTimer = 0; }

/* ------------------------------------------------------------------ why-this-pick */
async function showWhy(i, x, y) {
  if (S.seed == null) return toast('Only available inside a mix', true);
  if (S.stats.engine !== 'v2') return toast('Why-this-pick needs the V2 engine', true);
  try {
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

function tuneTrack(i, dir) {
  // toggle semantics: clicking the same vote again withdraws it; More and Less
  // are mutually exclusive per track. Then re-rank through the same refine() path.
  const add = dir === 'more' ? S.liked : S.disliked;
  const other = dir === 'more' ? S.disliked : S.liked;
  const at = add.indexOf(i);
  if (at >= 0) add.splice(at, 1);
  else {
    add.push(i);
    const o = other.indexOf(i);
    if (o >= 0) other.splice(o, 1);
  }
  refine();
}

async function refine() {
  if (S.seed == null) return toast('Create a mix first', true);
  if (S.stats.engine !== 'v2') return toast('More/Less Like This needs the V2 engine', true);
  try {
    const j = await jpost('/api/refine', {
      i: S.seed, size: Math.min(Math.max(+$('mixSize').value || 100, 20), 150),
      liked: S.liked, disliked: S.disliked,
    });
    const ids = (j.tracks || []).map(x => x.i);
    S.mix = [S.seed, ...ids.filter(i => i !== S.seed)];
    $('mixN').textContent = S.mix.length;
    showMix(); toast(`Refined (+${S.liked.length} / −${S.disliked.length})`);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ column chooser */
function openColMenu(x, y) {
  const m = $('colMenu');
  m.innerHTML = COLS.map(c =>
    `<li data-col="${c.id}"><span class="ck">${visCols.has(c.id) ? '✓' : ''}</span>${c.label}</li>`).join('');
  m.style.left = Math.min(x, innerWidth - 180) + 'px';
  m.style.top = Math.min(y, innerHeight - 320) + 'px';
  m.hidden = false;
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
  // table rows: selection, stars, dblclick-to-play
  $('tbody').addEventListener('click', e => {
    const tb = e.target.closest('.tb');
    if (tb) {
      const wrap = tb.closest('.tune');
      tuneTrack(+wrap.dataset.i, tb.classList.contains('more') ? 'more' : 'less');
      return;
    }
    const star = e.target.closest('.stars i');
    if (star) {
      const wrap = star.closest('.stars');
      const i = +wrap.dataset.i;
      const cur = S.rows.find(r => r.i === i)?.rating || 0;
      const n = +star.dataset.s;
      rateTrack(i, n === cur ? 0 : n);       // click the same star again to clear
      return;
    }
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
    repaintRowState();
  });
  $('tbody').addEventListener('dblclick', e => {
    if (e.target.closest('.stars')) return;
    const tr = e.target.closest('tr'); if (!tr) return;
    const i = +tr.dataset.i; if (i < 0) return;
    if (S.view === 'nowplaying') {
      Player.playAt([...$('tbody').children].indexOf(tr));
    } else {
      // double-click = play this row now, and queue the rest of the view after it
      // (the standard player behaviour), not just a bare one-track queue.
      const rest = S.rows.filter(r => r.i >= 0).map(r => r.i);
      Player.playList(rest, rest.indexOf(i));
    }
  });

  // infinite scroll
  $('tableWrap').addEventListener('scroll', maybeLoadMore);

  // sorting + column chooser
  $('thead-row').addEventListener('click', e => {
    const th = e.target.closest('th'); if (!th) return;
    const s = th.dataset.sort;
    if (S.sort === s) S.desc = !S.desc; else { S.sort = s; S.desc = false; }
    if (S.view === 'library') { S._userSorted = true; loadLibrary(true); }
    else {
      const keyf = { track: r => r.track, title: r => r.title.toLowerCase(),
        length: r => r.seconds, artist: r => r.artist.toLowerCase(),
        album: r => r.album.toLowerCase(), year: r => r.year || 0,
        genre: r => (r.genre || '').toLowerCase(),
        rating: r => r.rating || 0, plays: r => r.plays || 0,
        status: r => r.status }[s];
      if (!keyf) return;
      const rows = S.rows.slice().sort((a, b) =>
        keyf(a) < keyf(b) ? -1 : keyf(a) > keyf(b) ? 1 : 0);
      if (S.desc) rows.reverse();
      // keep S.mix in step with what's on screen -- currentExportIds() reads S.mix in the
      // mix view, so a stale S.mix would export a different order than the user is looking at
      if (S.view === 'mix') S.mix = rows.map(r => r.i);
      renderRows(rows, { seed: S.seed }); setHeaderSort();
    }
  });
  $('thead-row').addEventListener('contextmenu', e => {
    e.preventDefault();
    openColMenu(e.clientX, e.clientY);
  });
  $('colMenu').addEventListener('click', e => {
    const li = e.target.closest('li[data-col]'); if (!li) return;
    const c = li.dataset.col;
    if (visCols.has(c)) { if (visCols.size > 2) visCols.delete(c); }
    else visCols.add(c);
    store.set('cols', [...visCols]);
    renderHead();
    renderRows(S.rows, { seed: S.seed });
    openColMenu(parseInt($('colMenu').style.left), parseInt($('colMenu').style.top));
  });

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
    if (e.target.closest('#foldersHead')) { toggleFolderPane(); return; }
    const smartLi = e.target.closest('li[data-smart]');
    if (smartLi) { loadSmart(smartLi.dataset.smart); return; }
    const li = e.target.closest('li[data-view]'); if (!li) return;
    if (li.dataset.view === 'library') { S.smart = ''; S.folder = null; }
    ({ library: () => loadLibrary(true), mix: showMix, nowplaying: showNowPlaying })[li.dataset.view]();
  });

  // folder tree: triangle toggles children, name opens the folder in the table
  $('folderTree').addEventListener('click', e => {
    const li = e.target.closest('li'); if (!li) return;
    if (e.target.closest('.tw')) toggleFolderChildren(li);
    else openFolder(li.dataset.path);
  });

  // view mode toggle (list ↔ album grid)
  $('btnViewList').onclick = () => setViewMode('list');
  $('btnViewGrid').onclick = () => setViewMode('grid');

  // album card: click opens the album in list view; ▶ plays the whole album
  $('albumGrid').addEventListener('click', async e => {
    const card = e.target.closest('.acard'); if (!card) return;
    const a = (S._albums || [])[+card.dataset.k]; if (!a) return;
    if (e.target.closest('[data-play]')) {
      Player.playList(a.ids, 0);
      return;
    }
    // open: filter to this album (facet) in list view
    S.viewMode = 'list'; store.set('viewMode', 'list');
    S.smart = '';
    S.facets.album = new Set([a.album]);
    S.facets.artist = new Set(); S.facets.genre = new Set();
    await loadLibrary(true);
    $('viewLabel').textContent = a.album;      // name the album, not just "Library"
  });
  $('plList').addEventListener('click', e => {
    const li = e.target.closest('li'); if (li) showPlaylist(li.dataset.name);
  });
  $('btnBackLib').onclick = () => loadLibrary(false);

  // queue tools
  $('qClear').onclick = () => { Player.clearQueue(); showNowPlaying(); };
  $('qShuffle').onclick = () => { Player.shuffleQueue(); showNowPlaying(); };
  $('qSave').onclick = () => {
    if (!Player.q.length) return toast('Queue is empty', true);
    $('exportPanel').hidden = false;
    $('plName').value = 'Queue ' + new Date().toISOString().slice(0, 10);
    updateSelStatus();
  };

  // drag-reorder inside the queue view
  let dragFrom = null;
  $('tbody').addEventListener('dragstart', e => {
    if (S.view !== 'nowplaying') return;
    const tr = e.target.closest('tr'); if (!tr) return;
    dragFrom = [...$('tbody').children].indexOf(tr);
    e.dataTransfer.effectAllowed = 'move';
  });
  $('tbody').addEventListener('dragover', e => {
    if (S.view !== 'nowplaying' || dragFrom === null) return;
    e.preventDefault();
    const tr = e.target.closest('tr'); if (!tr) return;
    $('tbody').querySelectorAll('tr.dragover').forEach(t => t.classList.remove('dragover'));
    tr.classList.add('dragover');
  });
  $('tbody').addEventListener('drop', e => {
    if (S.view !== 'nowplaying' || dragFrom === null) return;
    e.preventDefault();
    const tr = e.target.closest('tr'); if (!tr) return;
    const to = [...$('tbody').children].indexOf(tr);
    Player.moveInQueue(dragFrom, to);
    dragFrom = null;
    showNowPlaying();
  });
  $('tbody').addEventListener('dragend', () => {
    dragFrom = null;
    $('tbody').querySelectorAll('tr.dragover').forEach(t => t.classList.remove('dragover'));
  });

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
    $('flavorRoot').textContent = v ? 'Paths will read: ' + v + '\\…' : 'Not configured in .env';
  };
  $('flavor').addEventListener('change', showRoot);
  showRoot._run = showRoot;
  $('btnSaveDir').onclick = exportSaveDir;
  $('btnDownload').onclick = exportDownload;
  $('btnPlex').onclick = exportPlex;
  $('copyBrowse').onclick = copyBrowse;
  $('btnCopy').onclick = startCopy;
  $('btnCopyCancel').onclick = cancelCopy;
  // if a copy is already running (started before this page loaded), surface it
  jget('/api/export/copy/status').then(st => { if (st.running) startCopyPoll(); }).catch(() => {});

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
    if (!S.sel.has(i)) { S.sel.clear(); S.sel.add(i); S.anchor = i; repaintRowState(); }
    ctx.dataset.i = i;
    const row = S.rows.find(r => r.i === i) || {};
    // live rate + love state inside the menu
    $('ctxStars').dataset.i = i;
    $('ctxStars').innerHTML = [1, 2, 3, 4, 5].map(n =>
      `<i data-s="${n}" class="${n <= (row.rating || 0) ? 'on' : ''}">${n <= (row.rating || 0) ? '★' : '☆'}</i>`).join('');
    ctx.querySelector('[data-act="love"] span').textContent =
      row.loved ? '♥ Un-love' : '♥ Love';
    ctx.style.left = Math.min(e.clientX, innerWidth - 240) + 'px';
    ctx.style.top = Math.min(e.clientY, innerHeight - 430) + 'px';
    ctx.hidden = false;
  });
  ctx.addEventListener('click', async e => {
    const star = e.target.closest('#ctxStars i');
    const li = e.target.closest('li[data-act]'); if (!li) return;
    const i = +ctx.dataset.i;
    const row = S.rows.find(r => r.i === i) || {};
    if (star) {
      const n = +star.dataset.s;
      rateTrack(i, n === (row.rating || 0) ? 0 : n);
      ctx.hidden = true;
      return;
    }
    if (li.dataset.act === 'rate') return;      // only the stars inside act
    ctx.hidden = true;
    switch (li.dataset.act) {
      case 'mix': doMix(i); break;
      case 'play': Player.playTrack(i); break;
      case 'playnext': Player.queueAdd(selectedIds().length ? selectedIds() : [i], { next: true });
        toast('Playing next'); break;
      case 'queue': Player.queueAdd(selectedIds().length ? selectedIds() : [i]);
        toast('Queued'); break;
      case 'love': loveTrack(i, !row.loved); break;
      case 'tags': Prefs.openTagEditor(i); break;
      case 'more': tuneTrack(i, 'more'); break;
      case 'less': tuneTrack(i, 'less'); break;
      case 'artist': S.facets.artist = new Set([row.artist]); loadLibrary(true); break;
      case 'album': S.facets.album = new Set([row.album]); loadLibrary(true); break;
      case 'genre': S.facets.genre = new Set([(row.genre || '').split(/[;,]/)[0].trim()]);
        loadLibrary(true); break;
      case 'why': showWhy(i, e.clientX, e.clientY); break;
      case 'copy': navigator.clipboard.writeText(`${row.artist} — ${row.title}`)
        .then(() => toast('Copied')); break;
      case 'reveal': jpost('/api/reveal', { i }).catch(err => toast(err.message, true)); break;
    }
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('.ctxmenu')) $('ctx').hidden = true;
    if (!e.target.closest('#colMenu') && !e.target.closest('#thead-row')) $('colMenu').hidden = true;
    if (!e.target.closest('.why')) $('why').hidden = true;
    if (!e.target.closest('.popover') && !e.target.closest('#toolbar button'))
      { $('optionsPanel').hidden = true; $('exportPanel').hidden = true; }
    if (!e.target.closest('#eqPanel') && !e.target.closest('#tEq')) $('eqPanel').hidden = true;
  });

  // keyboard — the full map (see also player.js for transport keys it owns)
  document.addEventListener('keydown', e => {
    if (e.target.matches('input,select,textarea')) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    const k = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && k === 'm' && !e.shiftKey) { e.preventDefault(); doMix(firstSelected()); }
    else if ((e.ctrlKey || e.metaKey) && e.shiftKey && k === 'm') { e.preventDefault(); Prefs.toggleMini(); }
    else if ((e.ctrlKey || e.metaKey) && k === ',') { e.preventDefault(); Prefs.open(); }
    else if ((e.ctrlKey || e.metaKey) && k === 'a') {
      e.preventDefault();
      S.rows.forEach(r => { if (r.i >= 0) S.sel.add(r.i); });
      repaintRowState();
    }
    else if (e.key === '/') { e.preventDefault(); $('q').focus(); $('q').select(); }
    else if (e.key === ' ') { e.preventDefault(); Player.togglePlay(); }
    else if (k === 'q') {
      const ids = selectedIds();
      if (ids.length) { Player.queueAdd(ids); toast(`Queued ${ids.length}`); }
    }
    else if (k === 'z') Player.prev();
    else if (k === 'b') Player.next();
    else if (k === 's') Player.toggleShuffle();
    else if (k === 'r') Player.cycleRepeat();
    else if (k === 'd') Player.toggleAutoDj();
    else if (k === 'e') Player.toggleEqPanel();
    else if (e.key === 'F2') { const i = firstSelected(); if (i != null) Prefs.openTagEditor(i); }
    else if (e.key === 'Delete' && S.view === 'nowplaying') {
      const ids = selectedIds();
      if (ids.length) { Player.removeFromQueue(ids); showNowPlaying(); }
    }
    else if (e.key >= '0' && e.key <= '5' && !e.ctrlKey && !e.metaKey) {
      const i = firstSelected(); if (i != null) rateTrack(i, +e.key);
    }
    else if (e.key === 'Escape') { $('ctx').hidden = true; $('why').hidden = true;
      $('eqPanel').hidden = true; $('colMenu').hidden = true; }
  });
}

/* ------------------------------------------------------------------ boot (called by boot.js) */

/* Fetch that tolerates a server still coming up. The window can be on screen
   before the library finishes loading, so a refused connection early in boot is
   normal, not fatal — retry with backoff instead of killing the UI. */
async function jgetReady(url, { tries = 40, delay = 250, maxDelay = 1500 } = {}) {
  let last;
  for (let n = 0; n < tries; n++) {
    try { return await jget(url); }
    catch (e) {
      last = e;
      await new Promise(r => setTimeout(r, Math.min(delay * Math.pow(1.3, n), maxDelay)));
    }
  }
  throw last;
}

/* Never throws. Returns {ok, error} so boot.js can bring the rest of the UI up
   regardless. Each section below degrades on its own: a dead playlist folder
   must not cost you the library, and neither must cost you the player. */
async function initCore() {
  try { renderHead(); bindSliders(); bindEvents(); }
  catch (e) { console.error('[core] chrome', e); }

  try {
    S.stats = await jgetReady('/api/lib/stats');
  } catch (e) {
    toast('Cannot reach server: ' + e.message, true);
    return { ok: false, error: e.message };
  }

  try {
    const s = S.stats;
    $('engineName').textContent = s.engine === 'musicip' ? 'MusicIP Mixer (live)' : 'Attune V2';
    $('mipControls').hidden = s.engine !== 'musicip';
    $('v2Controls').hidden = s.engine === 'musicip';
    $('btnPlex').disabled = !s.plex;
    $('sLib').textContent = `Songs: ${fmt(s.songs)} (${fmt(s.analyzed)} analyzed)`;
    $('sTot').textContent = `${fmt(s.songs)} songs · ${s.gb} GB · ${s.hours} h · ` +
      `${fmt(s.genres)} genres · ${fmt(s.artists)} artists · ${fmt(s.albums)} albums`;
    $('flavor').dispatchEvent(new Event('change'));
  } catch (e) { console.error('[core] header', e); }

  // Playlists live in a folder that may be a network drive (M:) — slow or gone.
  // Isolated: losing the playlist tree must never cost the library or the player.
  let softFail = null;
  try { await loadPlaylists(); }
  catch (e) {
    console.error('[core] playlists', e);
    softFail = 'playlist folder unavailable';
    try { $('exportDir').textContent = 'Playlist folder unavailable'; } catch {}
  }

  try { await loadLibrary(true); }
  catch (e) {
    console.error('[core] library', e);
    softFail = 'library view failed to load';
  }

  return softFail ? { ok: false, error: softFail } : { ok: true };
}
