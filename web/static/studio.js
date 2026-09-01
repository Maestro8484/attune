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
  ban: [],             // pool indices thrown out of the mix (live, undoable)
  banLabel: {},        // pool index -> label, captured at removal time (see dropTracks)
  banArtists: [],      // artist names thrown out of the mix (as displayed)
  likedArtists: [],    // artist-level "more like this"
  dislikedArtists: [],
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

/* ------------------------------------------------------------------ hot pool reload
   scanjob's own docstring used to end with "tracks this job adds only become mixable
   after an app restart" -- POST /api/lib/reload (libreload.py) removes that restart.
   prefs.js calls offerReload(n) when a scan finishes with new tracks; this owns the
   banner/button/poll because it needs S/loadLibrary, which live here. */
let reloadTimer = 0;

function offerReload(newCount) {
  $('reloadMsg').textContent =
    `${fmt(newCount)} new track${newCount === 1 ? '' : 's'} — ready to load`;
  $('reloadBar').hidden = true;
  const btn = $('reloadNow');
  btn.textContent = 'Load now'; btn.disabled = false; btn.onclick = startReload;
  $('reloadBanner').hidden = false;
}

async function startReload() {
  const btn = $('reloadNow');
  btn.disabled = true;
  $('reloadMsg').textContent = 'Reloading library…';
  $('reloadBar').hidden = false;
  $('reloadFill').style.width = '25%';
  try {
    await jpost('/api/lib/reload', {});
  } catch (e) {
    // fallback: reload itself couldn't even start -- the old restart path still works
    $('reloadMsg').textContent = `Reload failed: ${e.message} — restart Attune to load them.`;
    btn.textContent = 'Retry'; btn.disabled = false;
    return;
  }
  clearInterval(reloadTimer);
  reloadTimer = setInterval(pollReload, 800);
}

async function pollReload() {
  let st;
  try { st = await jget('/api/lib/reload/status'); }
  catch { return; }        // server briefly busy — keep polling, same as pollScan()
  if (st.running) { $('reloadFill').style.width = '70%'; return; }
  clearInterval(reloadTimer); reloadTimer = 0;
  if (st.error) {
    $('reloadMsg').textContent = `Reload failed: ${st.error} — restart Attune to load them.`;
    const btn = $('reloadNow');
    btn.textContent = 'Retry'; btn.disabled = false;
    return;
  }
  $('reloadFill').style.width = '100%';
  const added = Math.max(0, (st.new_count || 0) - (st.old_count || 0));
  $('reloadMsg').textContent = `Loaded — ${fmt(st.new_count)} tracks in the mixable pool.`;
  toast(added ? `Library reloaded — ${fmt(added)} new track${added === 1 ? '' : 's'} now mixable`
              : 'Library reloaded');
  // refresh everything the reload just changed underneath us
  try {
    S.stats = await jget('/api/lib/stats');
    paintStats(S.stats);
  } catch (e) { console.error('[reload] stats refresh', e); }
  try {
    if (S.view === 'library') await loadLibrary(false);
  } catch (e) { console.error('[reload] library refresh', e); }
  setTimeout(() => { $('reloadBanner').hidden = true; }, 2500);
}

/* ---------------------------------------------------------------- header counts
   ONE writer for the header/status-bar numbers -- boot and the post-reload repaint used
   to keep their own copy of the same four lines.

   `songs` is the MIXABLE POOL, and always was. When the library holds MORE than that, the
   difference is tracks analysis never got through, and both numbers get said out loud:
   reporting only the pool is what made a library with 217 unusable tracks read as
   "21,236 songs, 21,236 analyzed", i.e. perfect (MORNING_REPORT_2026-07-29.md §4.1). */
function paintStats(s) {
  const failed = s.failed || 0;
  $('sLib').textContent = `Songs: ${fmt(s.songs)} (${fmt(s.analyzed)} analyzed)` +
    (failed ? ` · ${fmt(failed)} not mixable` : '');
  $('sLib').title = failed
    ? `Your library holds ${fmt(s.db_tracks)} tracks. ${fmt(s.songs)} of them can go in a `
      + `mix; ${fmt(failed)} could not be analyzed — see "Not Mixable" in the sidebar.`
    : 'Every track in your library can go in a mix.';
  $('sTot').textContent = `${fmt(s.songs)} songs · ${s.gb} GB · ${s.hours} h · ` +
    `${fmt(s.genres)} genres · ${fmt(s.artists)} artists · ${fmt(s.albums)} albums` +
    (failed ? ` · library holds ${fmt(s.db_tracks)}` : '');
  $('missingN').textContent = fmt(s.missing || 0);
  $('failedN').textContent = fmt(failed);
  // nothing to show, no sidebar entry -- it appears the moment a scan leaves something behind
  const li = document.querySelector('#tree li[data-view="failures"]');
  if (li) li.hidden = !failed;
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
  // live mix filters. ban_artist is repeated, never comma-joined: artist names
  // contain commas ("Earth, Wind & Fire") and the server splits `ban` on commas.
  for (const i of S.ban) p.append('ban', i);
  for (const a of S.banArtists) p.append('ban_artist', a);
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

/* ------------------------------------------------------------------ recipes
   A recipe is a named preset for the mix dials -- the five weight sliders, MMR
   variety, flow ordering, dedup, and mix size -- stored server-side (recipes.py).
   Choosing one WRITES its params into the existing dial UI; mixParams() then keeps
   reading the DOM exactly as it always has, so a recipe is a shortcut for setting
   sliders by hand, never a second request-building path. */
let RECIPES = [];             // [{id,name,params,builtin}], server order (builtins first)
let RECIPE_DEFAULT = '';      // name of the server-configured default recipe, '' = none
let curRecipe = '';           // name of the currently applied recipe, '' = Dials (manual)
let ENGINE_DEFAULTS = null;   // {clap,lib,genre,bpm,era} captured from the DOM at boot --
                               // BEFORE any recipe ever touches the sliders. The HTML's
                               // slider value= attributes already mirror hybrid.py's
                               // DEFAULT_WEIGHTS (verified against recipes.py's own
                               // BUILTINS comment, web/recipes.py:103-106); reading them
                               // from the DOM instead of re-typing the numbers here means
                               // this file never hardcodes an engine weight.

function captureEngineDefaults() {
  ENGINE_DEFAULTS = {};
  for (const k of ['clap', 'lib', 'genre', 'bpm', 'era']) ENGINE_DEFAULTS[k] = $(k).value;
}

function findRecipe(name) {
  return RECIPES.find(r => r.name === name) || null;
}

/* Full current dial state, in the shape /api/recipe/save expects. Deliberately NOT
   mixParams() (that builds a GET querystring shaped for /api/mix and branches on
   the musicip engine); this is its own small reader so mixParams() stays untouched. */
function currentDialParams() {
  const p = {};
  for (const k of ['clap', 'lib', 'genre', 'bpm', 'era']) p[k] = +$(k).value;
  p.variety = $('mmr').checked ? 1 : 0;
  p.flow = $('flow').checked ? 1 : 0;
  if ($('dedupOn').checked) p.dedup = $('dedupField').value;
  p.size = Math.min(Math.max(+$('mixSize').value || 100, 20), 150);
  return p;
}

/* Write `params` (or nothing, for "Dials (manual)") into the dial UI.
   - weights: written if present, else reset to ENGINE_DEFAULTS (never left stale
     from whatever recipe was applied before).
   - variety/flow: written if present, else OFF -- there is no "leave alone" state
     for a checkbox the way there is for size/dedup below.
   - size/dedup: written ONLY when params actually specifies them; omitted means
     "no opinion", so whatever the user last set stays put. This is the one place
     this function does NOT reset to a default. */
function applyRecipe(params) {
  const p = params || {};
  for (const k of ['clap', 'lib', 'genre', 'bpm', 'era']) {
    $(k).value = (p[k] != null) ? p[k] : ENGINE_DEFAULTS[k];
    $(k).dispatchEvent(new Event('input'));
  }
  $('mmr').checked = !!p.variety;
  $('flow').checked = !!p.flow;
  if (p.size != null) $('mixSize').value = p.size;
  if (p.dedup != null) { $('dedupOn').checked = true; $('dedupField').value = p.dedup; }
}

function paintRecipeActions() {
  $('recipeActions').hidden = !curRecipe;
}

function paintRecipeOptions() {
  const sel = $('recipeSel');
  const cur = sel.value;
  sel.innerHTML = '<option value="">Dials (manual)</option>' + RECIPES.map(r =>
    `<option value="${esc(r.name)}">${r.name === RECIPE_DEFAULT ? '★ ' : ''}${esc(r.name)}</option>`
  ).join('');
  sel.value = RECIPES.some(r => r.name === cur) ? cur : '';
}

async function loadRecipes() {
  try {
    const j = await jget('/api/recipe/list');
    RECIPES = j.recipes || [];
    RECIPE_DEFAULT = j.default || '';
  } catch (e) { RECIPES = []; RECIPE_DEFAULT = ''; }
  paintRecipeOptions();
}

function selectRecipe(name, { persist = true } = {}) {
  curRecipe = name || '';
  applyRecipe(curRecipe ? (findRecipe(curRecipe) || {}).params : null);
  $('recipeSel').value = curRecipe;
  if (persist) store.set('recipe', curRecipe);
  paintRecipeActions();
}

async function saveAsRecipe() {
  const name = (prompt('Save current dials as a new recipe:', '') || '').trim();
  if (!name) return;
  try {
    await jpost('/api/recipe/save', { name, params: currentDialParams() });
    await loadRecipes();
    selectRecipe(name);
    toast(`Saved recipe "${name}"`);
  } catch (e) { toast(e.message, true); }
}

async function updateRecipe() {
  const rec = findRecipe(curRecipe); if (!rec) return;
  try {
    await jpost('/api/recipe/save', { id: rec.id, name: rec.name, params: currentDialParams() });
    await loadRecipes();
    selectRecipe(rec.name);
    toast(`Updated "${rec.name}"`);
  } catch (e) { toast(e.message, true); }
}

async function renameRecipe() {
  const rec = findRecipe(curRecipe); if (!rec) return;
  const name = (prompt('Rename recipe:', rec.name) || '').trim();
  if (!name || name === rec.name) return;
  try {
    await jpost('/api/recipe/save', { id: rec.id, name, params: rec.params });
    await loadRecipes();
    selectRecipe(name);
    toast(`Renamed to "${name}"`);
  } catch (e) { toast(e.message, true); }
}

async function deleteRecipe() {
  const rec = findRecipe(curRecipe); if (!rec) return;
  if (!confirm(`Delete recipe "${rec.name}"? This cannot be undone.`)) return;
  try {
    await jpost('/api/recipe/delete', { id: rec.id });
    await loadRecipes();
    selectRecipe('');           // Dials (manual) -- restores engine-default weights
    toast(`Deleted "${rec.name}"`);
  } catch (e) { toast(e.message, true); }
}

async function setDefaultRecipe() {
  const rec = findRecipe(curRecipe); if (!rec) return;
  try {
    await jpost('/api/settings', { default_recipe: rec.name });
    RECIPE_DEFAULT = rec.name;
    paintRecipeOptions();
    toast(`"${rec.name}" set as default`);
  } catch (e) { toast(e.message, true); }
}

function bindRecipeEvents() {
  $('recipeSel').addEventListener('change', () => selectRecipe($('recipeSel').value));
  $('btnRecipeSave').onclick = saveAsRecipe;
  $('btnRecipeUpdate').onclick = updateRecipe;
  $('btnRecipeRename').onclick = renameRecipe;
  $('btnRecipeDelete').onclick = deleteRecipe;
  $('btnRecipeSetDefault').onclick = setDefaultRecipe;
}

/* Boot-time recipe setup. Order per spec: a stored selection wins, then the
   server's configured default, then "Dials (manual)" untouched -- with NO stored
   selection and NO server default, this function must not write a single slider
   value, so a fresh boot's /api/mix and /api/export/m3u requests stay byte-identical
   to pre-recipe behaviour. Must run AFTER S.stats is loaded (engine check), and
   captures ENGINE_DEFAULTS from the DOM before any of that. */
async function initRecipes() {
  captureEngineDefaults();
  const isMip = S.stats && S.stats.engine === 'musicip';
  $('recipeSel').hidden = isMip;
  $('recipeControls').hidden = isMip;
  $('btnGenius').hidden = isMip;         // genius applies recipes, which are v2-param shaped
  if (isMip) return;           // recipes are v2-param shaped; nothing to do under musicip
  await loadRecipes();
  // three distinct stored states: null = never chose (fall back to the server default),
  // '' = explicitly chose Dials (manual) (respect it, do NOT reapply the default),
  // a name = reapply it if it still exists, else forget it and treat as "never chose"
  // (the recipe was deleted, possibly from another browser).
  let stored = store.get('recipe', null);
  if (stored && !RECIPES.some(r => r.name === stored)) {
    try { localStorage.removeItem('attune.recipe'); } catch {}
    stored = null;
  }
  if (stored) {
    selectRecipe(stored, { persist: false });
  } else if (stored === null && RECIPE_DEFAULT && RECIPES.some(r => r.name === RECIPE_DEFAULT)) {
    selectRecipe(RECIPE_DEFAULT, { persist: false });
  } else {
    curRecipe = '';
    $('recipeSel').value = '';
    paintRecipeActions();      // no DOM write beyond this -- dials stay exactly as authored
  }
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
  // File + encoding detail. Off by default so nobody's existing layout changes;
  // turn them on from the right-click menu on the column headers.
  { id: 'bpm',      label: 'BPM',       cls: 'c-bpm'    },
  { id: 'bitrate',  label: 'Bitrate',   cls: 'c-bitr'   },
  { id: 'format',   label: 'Format',    cls: 'c-fmt'    },
  { id: 'size',     label: 'Size',      cls: 'c-size'   },
  { id: 'filedate', label: 'File date', cls: 'c-fdate'  },
  { id: 'added',    label: 'Added',     cls: 'c-added'  },
  { id: 'path',     label: 'File path', cls: 'c-path'   },
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
    case 'bpm':    return r.bpm || '';
    // Real bitrate off the file header (audioinfo.py), not size/duration arithmetic.
    // Blank while the fill pass is still working through the library — never "0".
    // The CBR/VBR suffix only appears when the file actually declares one; plenty of
    // older mp3s carry no Xing/LAME header and inventing a mode would be a lie.
    case 'bitrate': return r.bitrate ? `${r.bitrate}${r.brmode ? ' ' + esc(r.brmode) : ''}` : '';
    case 'format':  return esc(r.format || '');
    case 'size':    return r.bytes ? mb(r.bytes) : '';
    case 'filedate':return r.mtime ? ymd(r.mtime) : '';
    case 'added':   return r.added ? ymd(r.added) : '';
    case 'path':    return esc(r.path || '');
  }
  return '';
}

function mb(b) {
  return b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.round(b / 1024) + ' KB';
}
function ymd(unix) {
  const d = new Date(unix * 1000);
  if (isNaN(d)) return '';
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-` +
         `${String(d.getDate()).padStart(2, '0')}`;
}

function renderHead() {
  $('thead-row').innerHTML = COLS.filter(c => visCols.has(c.id)).map(c =>
    `<th data-sort="${c.id}" class="${c.cls}">${c.label}</th>`).join('');
  setHeaderSort();
}

/* ------------------------------------------------------------------ table */
/* Every table-row view (library/mix/now-playing/playlist/smart auto-playlist -- the last
   one lives in smartlist.js, which calls this directly) funnels through here. That makes
   it the one choke point that can restore the ordinary table chrome: leaving the album
   grid or the Diagnostics panel (setTableMode()/showDiagnostics(), below) self-heals
   through ANY of those paths, without every caller needing to know grid/Diagnostics exist. */
function renderRows(rows, opts = {}) {
  document.querySelector('#tbl').hidden = false;
  $('albumGrid').hidden = true;
  $('tableWrap').classList.remove('gridmode');
  $('diagView').hidden = true;
  $('diagUnavailable').hidden = true;
  $('azBar').hidden = !(S.view === 'library' && !S.folder);
  // Missing Files owns two pieces of chrome: its "Check files now" button, and a body class
  // that stops the "Hide missing files" toggle from emptying the one view whose entire job
  // is listing missing files (see studio.css). Both live here, at the same choke point the
  // A-Z bar and the Diagnostics panel use, so every view transition clears them for free.
  const inMissing = S.view === 'library' && S.smart === 'missing' && !S.folder;
  $('verifyTools').hidden = !inMissing;
  document.body.classList.toggle('inMissingView', inMissing);
  S.rows = rows;
  // Crash/restart slice 3: "the mix the user last had open" means literally that -- only
  // persisted while Mix is the view actually on screen, matching slices 1-2 (window
  // geometry, throttled seek restore), which also restore whatever was true at the moment
  // of closing rather than "the last mix that ever existed". Leaving Mix for any other
  // view clears it right here, so a deliberate "back to Library" before quitting is
  // honoured on the next launch instead of being overridden. See restoreLastMix() below.
  store.set('lastMix', (S.view === 'mix' && S.seed != null && S.mix.length) ?
    { seed: S.seed, mix: S.mix } : null);
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
    // "More Like This" anchors: pinned under the seed with the green edge + badge
    if (S.view === 'mix' && r.i !== seed && S.liked.includes(r.i)) cls.push('pinned');
    if (r.missing) cls.push('missing');
    if (r.unmixable) cls.push('unmixable');
    const tune = S.view === 'mix' && S.stats.engine === 'v2' &&
                 r.i >= 0 && r.i !== seed;
    const tds = cols.map(c => {
      const extra = (c.id === 'status' && r.status !== 'Analyzed') ? ' no' : '';
      const cell = cellHtml(c, r) + (tune && c.id === 'title' ? tuneHtml(r.i) : '');
      // A path is wider than any sane column, so the cell truncates and hover shows
      // the whole thing rather than forcing the table to scroll to read one row.
      const ct = (c.id === 'path' && r.path) ? ` title="${esc(r.path)}"` : '';
      return `<td class="${c.cls}${extra}"${ct}>${cell}</td>`;
    }).join('');
    // r.tip: hover text for the whole row. Set by the Not Mixable view, which has to be
    // able to answer "which file is this?" — those tracks appear in no other view, so the
    // path is the only handle on them.
    const tip = r.tip ? ` title="${esc(r.tip)}"` : '';
    return `<tr data-i="${r.i}"${tip} ${draggable ? 'draggable="true"' : ''} class="${cls.join(' ')}">${tds}</tr>`;
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
  mostplayed: 'Most Played', neverplayed: 'Never Played', missing: 'Missing Files' };

function setTableMode(grid) {
  $('tableWrap').classList.toggle('gridmode', grid);
  document.querySelector('#tbl').hidden = grid;
  $('albumGrid').hidden = !grid;
  // Entering grid is the one table-row-view transition that does NOT go through
  // renderRows() (loadAlbums paints #albumGrid directly) -- so the A-Z bar/Diagnostics
  // panel/lastMix flag need their own explicit clear here. Leaving grid (grid=false) is
  // always immediately followed by a renderRows() call from the caller, which handles it.
  if (grid) {
    $('azBar').hidden = true; $('diagView').hidden = true; $('diagUnavailable').hidden = true;
    $('verifyTools').hidden = true; document.body.classList.remove('inMissingView');
    store.set('lastMix', null);      // the album grid is a Library presentation, not Mix
  }
  $('btnViewList').classList.toggle('on', !grid);
  $('btnViewGrid').classList.toggle('on', grid);
}

// Shared by loadLibrary and maybeLoadMore so a smart view's natural sort (until the
// user clicks a header) stays consistent across every page — paging past row 200
// must not re-sort mid-scroll just because one call site forgot the guard.
function applySortParams(p) {
  if (!(S.smart && !S._userSorted)) { p.set('sort', S.sort); if (S.desc) p.set('desc', '1'); }
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
  applySortParams(p);
  p.set('offset', S.offset); p.set('limit', S.limit);
  const j = await jget('/api/lib/tracks?' + p);
  S.total = j.total;
  renderRows(j.rows);
  $('tableWrap').scrollTop = 0;
  setHeaderSort();
  const hrs = (j.seconds / 3600).toFixed(1);
  $('viewSub').textContent = `${fmt(j.total)} songs · ${hrs} h`;
  // Missing Files: an empty table there means one of two completely different things --
  // "every file is where it should be" or "nobody has ever looked" -- and it used to show
  // the same blank either way, because nothing in the window could start the check.
  // (The button + the CSS guard are handled centrally in renderRows().)
  if (S.smart === 'missing') {
    const checked = (S.stats && S.stats.verified) || 0;
    $('viewSub').textContent = !checked
      ? 'Nothing has been checked yet, so this list is empty for that reason and no other. '
        + 'Press "Check files now".'
      : (!j.total
          ? `All ${fmt(checked)} checked tracks were found on disk.`
          : `${fmt(j.total)} of ${fmt(checked)} checked tracks are gone from disk. They stay `
            + `in the library and can still turn up in a mix — right-click a row to relink `
            + `it to its new home, or remove it.`);
  }
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
    applySortParams(p);
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

/* ------------------------------------------------------------------ A-Z jump bar
   Beside the Library list (see #azBar's hidden toggle in renderRows()/setTableMode() --
   list only, never grid, never a folder, never a smart view's own natural order). Jumping
   needs an exact ROW OFFSET into the server's sorted list, and there is no "give me the
   offset for letter X" endpoint -- this stream owns studio.js only, not studio.py. So this
   binary-searches the offset space instead, against the SAME /api/lib/tracks endpoint the
   table already pages through (limit=1 probes, ~log2(21000) ≈ 15 round trips for the
   current library), then loads the real page at the offset it converges on -- the target
   row is genuinely fetched, never a jump to nothing. */
const AZ_LETTERS = ['#', ...'ABCDEFGHIJKLMNOPQRSTUVWXYZ'];

function azBucket(name) {
  const c = String(name || '').normalize('NFKD').replace(/\p{M}/gu, '')
    .trim().toUpperCase()[0] || '';
  return (c >= 'A' && c <= 'Z') ? c : '#';
}

function renderAzBar() {
  const bar = $('azBar');
  if (!bar || bar.children.length) return;      // build once
  bar.innerHTML = AZ_LETTERS.map(l => `<button type="button" data-l="${l}">${l}</button>`).join('');
}

async function jumpToLetter(letter) {
  if (S.view !== 'library' || S.viewMode === 'grid' || S.folder) return;
  const bar = $('azBar');
  bar.classList.add('busy');
  try {
    // A jump only makes sense against an artist-sorted, ascending list -- force it, the
    // same as clicking the Artist column header would. S._userSorted stops a smart
    // view's natural sort (applySortParams()) from clobbering it back on the next page.
    S.sort = 'artist'; S.desc = false; S._userSorted = true;
    setHeaderSort();
    const p = facetQS();
    if (S.smart) p.set('smart', S.smart);
    p.set('sort', 'artist');
    let lo = 0, hi = S.total;
    if (letter !== '#') {
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        const q = new URLSearchParams(p); q.set('offset', mid); q.set('limit', 1);
        const j = await jget('/api/lib/tracks?' + q);
        const bucket = j.rows[0] ? azBucket(j.rows[0].artist) : letter;
        if (bucket < letter) lo = mid + 1; else hi = mid;
      }
    }
    S.offset = lo;
    const q = new URLSearchParams(p); q.set('offset', S.offset); q.set('limit', S.limit);
    const j = await jget('/api/lib/tracks?' + q);
    S.total = j.total;
    renderRows(j.rows);
    $('tableWrap').scrollTop = 0;
    renderPager();
    bar.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.l === letter));
    // A letter no artist starts with converges on the offset where it WOULD be and loads
    // whatever sits there -- or nothing at all, past the end of the list. Either way the
    // letter is a lie, so say so instead of leaving a table that looks broken.
    const landed = j.rows.length ? azBucket(j.rows[0].artist) : '';
    if (letter !== '#' && landed !== letter) {
      $('viewSub').textContent = landed
        ? `No artist starts with "${letter}" — showing "${landed}" instead.`
        : `No artist starts with "${letter}".`;
      $('empty').textContent = `No artist starts with "${letter}".`;
    } else {
      // put the ordinary count line back: a previous jump may have left a "no artist
      // starts with X" message sitting there, and this jump did land.
      $('viewSub').textContent = `${fmt(j.total)} songs · ${(j.seconds / 3600).toFixed(1)} h`;
      $('empty').textContent = 'Nothing here.';
    }
  } catch (e) { toast(e.message, true); }
  finally { bar.classList.remove('busy'); }
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

async function doMix(seedI, opts) {
  if (seedI == null) return toast('Select a track first', true);
  // A brand-new mix starts clean; a filter-driven re-mix keeps the filters.
  if (!(opts && opts.keepFilters)) {
    S.ban = []; S.banArtists = []; S.likedArtists = []; S.dislikedArtists = [];
    S.banLabel = {};
  }
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

/* Blend: 2+ selected rows -> a mix that sounds like ALL of them (ruling A1, an
   acoustic blend against the seeds' shared center). The server reports `cohesion`
   (how alike the seeds themselves are); a low number is surfaced instead of
   silently serving a stretch (ruling A4; threshold provisional pending ears). */
/* Ceiling on how many tracks can seed one blend. This is not tidiness: the blend ranks
   against the seeds' shared centre, and the centre of a very large set converges on the
   library average, which is the "everything sounds the same" failure LAW 1 exists to
   stop. Cohesion over that many seeds carries no information either. 25 is provisional
   pending the ear sitting, exactly like the 0.5 cohesion threshold below, and when it
   bites the toast SAYS so rather than quietly dropping tracks. */
const MAX_BLEND_SEEDS = 25;

async function doBlend(seedIds, sourceLabel) {
  if (!seedIds || seedIds.length < 2) return toast('Pick two or more tracks first', true);
  let dropped = 0;
  if (seedIds.length > MAX_BLEND_SEEDS) {
    dropped = seedIds.length - MAX_BLEND_SEEDS;
    seedIds = seedIds.slice(0, MAX_BLEND_SEEDS);
  }
  S.ban = []; S.banArtists = []; S.likedArtists = []; S.dislikedArtists = [];
  S.banLabel = {};
  const p = new URLSearchParams();
  seedIds.forEach(i => p.append('i', i));
  p.set('size', Math.max(20, Math.min(150, +$('mixSize').value || 100)));
  try {
    const j = await jget('/api/mix/blend?' + p);
    S.seed = seedIds[0];
    S.liked = []; S.disliked = [];
    const ids = (j.tracks || []).map(x => x.i).filter(i => !seedIds.includes(i));
    S.mix = [...seedIds, ...ids];
    $('mixN').textContent = S.mix.length;
    const c = j.cohesion;
    const from = sourceLabel ? ` from the ${sourceLabel}` : '';
    const cut = dropped ? ` · used the first ${seedIds.length}, ignored ${dropped}` : '';
    if (c != null && c < 0.5) {
      toast(`These ${seedIds.length} tracks don't blend well (cohesion ${c.toFixed(2)}) — ` +
            `expect a stretch${cut}`, true);
    } else if (c != null) {
      toast(`Playlist from ${seedIds.length} tracks${from} · cohesion ${c.toFixed(2)}${cut}`);
    }
    await showMix();
  } catch (e) { toast(e.message, true); }
}

/* The queue as a seed set. Selection wins when there is one -- the operator asked for
   "the selected files in the queue" in as many words -- and with nothing selected it
   falls back to everything still TO COME (q from the current position), not the whole
   queue, because blending tracks already played reruns the evening.
   The result opens as a mix and never overwrites the queue: a playlist here is a
   capture in time (ruling C4), so nothing destructive happens behind the operator. */
function blendFromQueue() {
  const sel = selectedIds();
  const useSel = sel.length >= 2;
  const ids = [...new Set(useSel ? sel : Player.q.slice(Player.pos).filter(i => i >= 0))];
  if (ids.length < 2) {
    return toast(useSel ? 'Pick two or more tracks first'
                        : 'Not enough tracks left in the queue to build from', true);
  }
  doBlend(ids, useSel ? 'tracks you picked' : 'queue');
}

/* Adventure: exactly two selected rows -> an ordered walk FROM the first TO the
   last (row order), endpoints included (ruling A1; the Sonic Adventure shape on
   our own vectors). */
async function doAdventure(seedIds) {
  if (!seedIds || seedIds.length !== 2) return toast('Select exactly two tracks (start and destination)', true);
  S.ban = []; S.banArtists = []; S.likedArtists = []; S.dislikedArtists = [];
  S.banLabel = {};
  const p = new URLSearchParams({ a: seedIds[0], b: seedIds[1],
    size: Math.max(3, Math.min(100, +$('mixSize').value || 25)) });
  try {
    const j = await jget('/api/mix/adventure?' + p);
    S.seed = seedIds[0];
    S.liked = []; S.disliked = [];
    S.mix = (j.tracks || []).map(x => x.i);
    $('mixN').textContent = S.mix.length;
    toast(`Adventure: ${j.a.label} → ${j.b.label}`);
    await showMix();
  } catch (e) { toast(e.message, true); }
}

/* FLIP: capture where each row sits, re-render, then slide survivors from their old
   position to the new one and fade newcomers in. This is what makes a re-rank read as
   "the mix responded to me" instead of a flicker. Web Animations API — no class juggling. */
function captureRowTops() {
  const m = new Map();
  document.querySelectorAll('#tbody tr[data-i]').forEach(tr =>
    m.set(tr.dataset.i, tr.getBoundingClientRect().top));
  return m;
}
function animateReorder(before) {
  document.querySelectorAll('#tbody tr[data-i]').forEach(tr => {
    const old = before.get(tr.dataset.i);
    const now = tr.getBoundingClientRect().top;
    if (old === undefined) {
      tr.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 240, easing: 'ease-out' });
    } else if (Math.abs(old - now) > 1) {
      tr.animate([{ transform: `translateY(${old - now}px)` }, { transform: 'translateY(0)' }],
                 { duration: 280, easing: 'cubic-bezier(.2,.7,.3,1)' });
    }
  });
}

/* Genius: one click, zero dialogs, from app-open to music playing.
   1. apply a recipe to the dials (default recipe wins; otherwise leave whatever's
      already selected; otherwise fall back to "Classic Journey" if it exists)
   2. ask the server for a seed (tiered: loved+rested -> rating>=4 -> any analyzed)
   3. run it through the exact same doMix() path a manual "Create Mix" click takes
   4. hand the resulting S.mix straight to the player, starting at the seed --
      the same Player.playList() call the dblclick-to-play and album-play-button
      paths already use, so this never reimplements queueing.
   S.seed === i (only set by doMix on success) guards against playing a stale
   leftover mix if the mix request itself failed. */
async function geniusMix() {
  if (RECIPE_DEFAULT && findRecipe(RECIPE_DEFAULT)) {
    selectRecipe(RECIPE_DEFAULT, { persist: false });
  } else if (!curRecipe && findRecipe('Classic Journey')) {
    selectRecipe('Classic Journey', { persist: false });
  }
  $('btnGenius').disabled = true;
  try {
    const { i } = await jget('/api/recipe/genius_seed');
    // doMix swallows its own errors; on success it assigns a NEW S.mix array. Comparing
    // array identity (not just seed) distinguishes a fresh mix from a stale one left by
    // an earlier run of the SAME seed (small loved/rated pools repeat seeds often).
    const before = S.mix;
    await doMix(i);
    if (S.mix !== before && S.mix.length && S.mix[0] === i) Player.playList(S.mix, 0);
  } catch (e) { toast(e.message, true); }
  finally { $('btnGenius').disabled = false; }
}

async function showMix(opts = {}) {
  S.view = 'mix'; markTree('mix');
  $('viewLabel').textContent = 'Mix';
  $('btnBackLib').hidden = false;
  $('queueTools').hidden = true;
  $('pager').innerHTML = '';
  // recipe name badge next to the mix header, when a saved recipe (not manual dials) is active
  const recipeTag = $('mixRecipeTag');
  if (curRecipe) { recipeTag.textContent = '✦ ' + curRecipe; recipeTag.hidden = false; }
  else recipeTag.hidden = true;
  if (!S.mix.length) { renderRows([]); return; }
  const j = await jget('/api/lib/rows?' + S.mix.map(i => `i=${i}`).join('&'));
  const byI = new Map(j.rows.map(r => [r.i, r]));
  const rows = S.mix.map(i => byI.get(i)).filter(Boolean);
  // capture AFTER the fetch, immediately before the DOM swap (rects go stale if the
  // user scrolls during the await). Only animate mix->mix re-ranks: on a fresh mix the
  // old rows are library rows that may share pool ids, and FLIPping those looks chaotic.
  const before = opts.animate ? captureRowTops() : null;
  renderRows(rows, { seed: S.seed });
  if (before) animateReorder(before);
  const secs = rows.reduce((a, r) => a + r.seconds, 0);
  const sr = byI.get(S.seed);
  const votes = S.liked.length + S.disliked.length;
  $('viewSub').innerHTML =
    `${rows.length} tracks · ${hms(secs)} · seed: ${esc(sr ? sr.artist + ' — ' + sr.title : '?')}` +
    (votes ? ` · <span class="steer">steering +${S.liked.length} −${S.disliked.length}</span>` +
             ` <button class="chip" id="steerReset" title="Clear all steering and re-mix from the seed">reset</button>`
           : '');
}

/* Crash/restart slice 3: reopen the mix the user last had open. `saved` is read from
   localStorage BEFORE initCore()'s own loadLibrary(true) call -- that call's renderRows()
   writes S.view==='library' through the SAME store.set('lastMix', ...) line showMix()'s
   renderRows() writes, which would otherwise clobber the very value this function needs
   to read. See the call site in initCore().
   Rehydration reads rows via /api/lib/rows -- the exact GET showMix() always makes to
   paint -- never /api/mix, so reopening never recomputes a mix (LAW 1: no metric ever
   picks what ships, and re-running the engine on restart would silently do exactly that
   for whichever candidates happened to still be available). A track deleted/relinked
   between sessions just drops out of the restored list; if fewer than 2 tracks (the seed
   plus at least one companion) survive, restoring isn't "safe" per the spec -- fall back
   silently to whatever loadLibrary(true) already rendered (current empty-state behaviour). */
async function restoreLastMix(saved) {
  if (!saved || saved.seed == null || !Array.isArray(saved.mix) || saved.mix.length < 2) return;
  try {
    const j = await jget('/api/lib/rows?' + saved.mix.map(i => `i=${i}`).join('&'));
    const byI = new Map(j.rows.map(r => [r.i, r]));
    const survived = saved.mix.filter(i => byI.has(i));
    if (!byI.has(saved.seed) || survived.length < 2) return;
    S.seed = saved.seed;
    S.mix = survived;
    $('mixN').textContent = S.mix.length;
    await showMix();
  } catch (e) { console.error('[core] restoreLastMix', e); }   // stays on the library view
}

/* ------------------------------------------------------------------ diagnostics
   Read-only viewer for the server's log files -- audit item 9's "first slice"
   (structured logging), client half. The server contract is fixed and owned by a
   parallel stream (Stream C), not this file:
     GET /api/diag/logs                       -> {dir, files:[{name,size,mtime}, ...]} newest first
     GET /api/diag/logs/tail?name=&lines=      -> {name, lines:[...], truncated}
   Until that stream merges into this tree the endpoints simply 404; jget() already
   turns a non-OK response into a thrown Error (see jget's `if (!r.ok) throw ...`), so
   the plain try/catch below is the whole "degrade honestly" story -- no 404-specific
   branch needed. Read-only: no delete, no edit, no download anywhere in this view. */
let _diagFiles = [];
let _diagActiveFile = null;

function fmtBytes(n) {
  n = +n || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}
// mtime's units aren't nailed down by the contract ("mtime", no unit stated) -- Python's
// os.stat().st_mtime is epoch SECONDS, the common case, but this guards the epoch-ms case
// too rather than assuming and risking every date reading as 1970.
function fmtDiagDate(mtime) {
  if (!mtime) return '';
  const ms = mtime < 1e12 ? mtime * 1000 : mtime;
  try { return new Date(ms).toLocaleString(); } catch { return ''; }
}

async function showDiagnostics() {
  S.view = 'diagnostics'; markTree('diagnostics');
  document.querySelectorAll('#folderTree li.on, #slList li.on').forEach(l => l.classList.remove('on'));
  $('viewLabel').textContent = 'Diagnostics';
  $('viewSub').textContent = '';
  $('btnBackLib').hidden = false;
  $('queueTools').hidden = true;
  $('pager').innerHTML = '';
  $('filterBar').hidden = true;
  // Diagnostics doesn't render table rows, so it never passes through renderRows()'s
  // chrome-restore choke point -- hide the table-view pieces explicitly on entry, the
  // same way setTableMode(true) does for the album grid.
  document.querySelector('#tbl').hidden = true;
  $('albumGrid').hidden = true;
  $('tableWrap').classList.remove('gridmode');
  $('azBar').hidden = true;
  store.set('lastMix', null);      // Diagnostics is not the Mix view either
  await loadDiagLogs();
}

async function loadDiagLogs() {
  try {
    const j = await jget('/api/diag/logs');
    _diagFiles = j.files || [];
    $('diagUnavailable').hidden = true;
    $('diagView').hidden = false;
    $('diagDir').textContent = j.dir ? 'Folder: ' + j.dir : '';
    renderDiagFileList();
    if (_diagFiles.length) {
      const keep = _diagActiveFile && _diagFiles.some(f => f.name === _diagActiveFile);
      await loadDiagTail(keep ? _diagActiveFile : _diagFiles[0].name);
    } else {
      _diagActiveFile = null;
      $('diagTailName').textContent = '—';
      $('diagTailBody').textContent = '';
      $('diagTailTruncated').hidden = true;
    }
  } catch (e) {
    // Covers both "Stream C hasn't merged yet" (404 -> jget throws "HTTP 404") and any
    // other transient failure -- same honest, non-throwing degrade either way.
    _diagFiles = []; _diagActiveFile = null;
    $('diagView').hidden = true;
    $('diagUnavailable').hidden = false;
    $('diagUnavailable').textContent = 'Diagnostics endpoint not available.';
  }
}

function renderDiagFileList() {
  const list = $('diagFileList');
  $('diagFilesEmpty').hidden = _diagFiles.length > 0;
  list.innerHTML = _diagFiles.map(f => `
    <li data-name="${esc(f.name)}" class="${f.name === _diagActiveFile ? 'on' : ''}">
      <span class="dn">${esc(f.name)}</span>
      <span class="dm">${fmtBytes(f.size)} · ${fmtDiagDate(f.mtime)}</span>
    </li>`).join('');
}

async function loadDiagTail(name) {
  _diagActiveFile = name;
  renderDiagFileList();
  $('diagTailName').textContent = name;
  $('diagTailBody').textContent = 'Loading…';
  $('diagTailTruncated').hidden = true;
  const lines = +$('diagLines').value || 200;
  try {
    const j = await jget(`/api/diag/logs/tail?name=${encodeURIComponent(name)}&lines=${lines}`);
    $('diagTailBody').textContent = (j.lines || []).join('\n');
    if (j.truncated) {
      $('diagTailN').textContent = fmt((j.lines || []).length);
      $('diagTailTruncated').hidden = false;
    }
    // newest lines are what you came here for -- land at the bottom, not the top
    $('diagTailBody').scrollTop = $('diagTailBody').scrollHeight;
  } catch (e) {
    $('diagTailBody').textContent = 'Could not read this file: ' + e.message;
  }
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

/* ---------------------------------------------------- right rail: Up Next + Info
   The queue has ONE owner: Player.q. #upNext is a second VIEW of it, never a second
   copy — it re-reads Player.q/Player.pos on every paint, and every queue mutation
   already funnels through paintTransport() (plus moveInQueue/shuffleQueue), which
   calls queueChanged() here. showNowPlaying() (the main-view queue) is untouched.  */
let _upNextT = 0, _upNextSeq = 0;

function queueChanged() {                      // called from player.js on any q change
  clearTimeout(_upNextT);
  _upNextT = setTimeout(renderUpNext, 60);     // coalesce bursts (queueAdd of 20 ids)
}

function railIsShowingQueue() {
  return !document.body.classList.contains('norail') && !$('railQueue').hidden;
}

async function renderUpNext() {
  const list = $('upNext');
  if (!list || typeof Player === 'undefined') return;
  const q = Player.q, pos = Player.pos;
  $('railQN').textContent = q.length;
  // Two or more still to come is exactly the condition the blend needs, so the button
  // appears only when pressing it could actually work.
  const qt = $('railQTools');
  if (qt) qt.hidden = q.slice(pos).filter(i => i >= 0).length < 2;
  if (!railIsShowingQueue()) return;            // collapsed or on the Info tab: no fetch
  if (!q.length) { list.innerHTML = ''; $('upNextEmpty').hidden = false; return; }
  $('upNextEmpty').hidden = true;
  const seq = ++_upNextSeq;
  let byI;
  try {
    const j = await jget('/api/lib/rows?' + [...new Set(q)].map(i => `i=${i}`).join('&'));
    byI = new Map(j.rows.map(r => [r.i, r]));
  } catch { return; }                           // transient — the next change repaints
  if (seq !== _upNextSeq) return;               // a newer render already won
  list.innerHTML = q.map((i, k) => {
    const r = byI.get(i);
    if (!r) return '';
    const cls = k === pos ? 'playing' : (k < pos ? 'done' : '');
    return `<li data-k="${k}" class="${cls}">` +
      `<span class="n">${k === pos ? '▶' : k + 1}</span>` +
      `<span class="qm"><span class="qt">${esc(r.title)}</span>` +
      `<span class="qa">${esc(r.artist)}</span></span>` +
      `<button class="qx" title="Remove from queue">✕</button></li>`;
  }).join('');
}

function setRailTab(id) {
  store.set('railTab', id);
  $('railTabs').querySelectorAll('button').forEach(b =>
    b.classList.toggle('on', b.dataset.rail === id));
  $('railQueue').hidden = id !== 'queue';
  $('railInfo').hidden = id !== 'info';
  if (id === 'queue') renderUpNext();           // the tab was not rendering while hidden
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

/* The tracks your library holds that analysis never got through. They are in no mix, no
   facet, no folder and no other view -- before this there was no screen anywhere that
   admitted they existed (MORNING_REPORT_2026-07-29.md §4.1). Rows come back with i = -1
   because they have no pool index: nothing to play, nothing to mix from, nothing to
   select, which every row handler already understands from the playlist view's
   not-found rows. The reason sits in the Status column, the full path in the row tooltip. */
async function showFailures() {
  S.view = 'failures'; S.smart = ''; S.folder = null;
  document.querySelectorAll('#tree li[data-smart],#folderTree li,#plList li,#slList li')
    .forEach(l => l.classList.remove('on'));
  markTree('failures');
  $('viewLabel').textContent = 'Not Mixable';
  $('btnBackLib').hidden = false; $('pager').innerHTML = '';
  $('queueTools').hidden = true;
  try {
    const j = await jget('/api/lib/failures');
    renderRows(j.rows.map(r => ({
      i: -1, track: 0, title: r.title, length: r.length, artist: r.artist,
      album: r.album, genre: r.genre, year: r.year, rating: 0, plays: 0,
      seconds: r.seconds, status: r.why, unmixable: true, tip: r.path })));
    $('viewSub').textContent = `${fmt(j.total)} of ${fmt(j.db_tracks)} tracks could not be `
      + `analyzed, so they are in no mix. ${fmt(j.pool)} tracks are mixable. `
      + `Re-scan a track to try again; hover a row for its file path.`;
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
  else if (S.view === 'failures') showFailures();
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

/* -- the file check behind the Missing Files view.
   libverify.py has run this pass since S13, but nothing in the window ever started it, so
   `filestate` stayed empty on the operator's real library: the view sat at 0, which reads
   as "all your files are there" and was actually "nobody looked" -- while 22 tracks in the
   live pool pointed at audio that is gone (MORNING_REPORT_2026-07-29.md §4.2). Ruled
   2026-07-29: surface it here, and do NOT change what the pool contains (mix semantics,
   LAW 1). Reads only: os.stat per track, nothing moved, deleted or re-tagged. */
let verifyTimer = 0;
function verifyMsg(text, cls = 'hint') { const el = $('verifyMsg'); el.className = cls; el.textContent = text; }

async function startVerify() {
  $('btnVerify').disabled = true;
  try {
    await jpost('/api/lib/verify', {});
  } catch (e) { $('btnVerify').disabled = false; return verifyMsg(e.message, 'hint err'); }
  $('btnVerifyCancel').hidden = false;
  clearInterval(verifyTimer);
  verifyTimer = setInterval(pollVerify, 800);
  pollVerify();
}

async function pollVerify() {
  let st;
  try { st = await jget('/api/lib/verify/status'); }
  catch (e) { clearInterval(verifyTimer); $('btnVerify').disabled = false; return; }
  if (st.running) {
    const pct = st.total ? Math.round(100 * st.checked / st.total) : 0;
    return verifyMsg(`Checking… ${fmt(st.checked)} of ${fmt(st.total)} (${pct}%) · `
      + `${fmt(st.missing)} missing so far`);
  }
  clearInterval(verifyTimer);
  verifyTimer = 0;
  $('btnVerify').disabled = false;
  $('btnVerifyCancel').hidden = true;
  if (st.error) return verifyMsg('Check failed: ' + st.error, 'hint err');
  if (!st.total && !st.checked) return;            // nothing has run in this session
  verifyMsg(st.cancelled
    ? `Stopped after ${fmt(st.checked)} of ${fmt(st.total)} · ${fmt(st.missing)} missing`
    : `Checked ${fmt(st.checked)} tracks · ${fmt(st.missing)} missing`);
  // the pass writes lib.missing in place, so the smart view and the counts are already
  // live -- just re-read them
  try { S.stats = await jget('/api/lib/stats'); paintStats(S.stats); } catch {}
  if (S.view === 'library' && S.smart === 'missing') loadLibrary(true);
}

/* ------------------------------------------------------------------ missing files */
// "Locate file…" — reuses the SAME server-side folder browser the Library-folder
// picker uses (Prefs.pickFolder), in file-picking mode (Prefs.pickFile): browse to
// the folder, click the actual audio file, POST /api/track/relink.
async function locateFile(i) {
  const picked = await Prefs.pickFile('');
  if (!picked) return;
  try {
    const j = await jpost('/api/track/relink', { i, new_path: picked });
    for (const r of S.rows) if (r.i === i) Object.assign(r, j.row, { missing: false });
    renderRows(S.rows, { seed: S.seed });
    toast(`Relinked — ${j.row.title || 'file'}`);
  } catch (e) { toast(e.message, true); }
}

// "Remove from library…" — a real confirm (count + names), Cancel is the native
// default action. Only touches Attune's own bookkeeping (tracks/features/clap/
// usermeta/filestate); the audio file on disk is never touched. The in-RAM pool
// still lists the track(s) until the next hot reload -- offer one right after.
async function deleteTracks(ids) {
  if (!ids.length) return;
  const names = ids.map(i => {
    const r = S.rows.find(x => x.i === i);
    return r ? `${r.artist || '?'} - ${r.title || '?'}` : `#${i}`;
  });
  const preview = names.slice(0, 8).join('\n') + (names.length > 8 ? `\n… and ${names.length - 8} more` : '');
  const ok = confirm(
    `Remove ${ids.length} track${ids.length === 1 ? '' : 's'} from the Attune library?\n\n` +
    `${preview}\n\nThe audio file(s) on disk are NOT touched. This cannot be undone.`);
  if (!ok) return;
  let removed = 0;
  for (const i of ids) {
    try { await jpost('/api/track/delete', { i }); removed++; }
    catch (e) { toast(`Failed on one track: ${e.message}`, true); }
  }
  if (!removed) return;
  toast(`Removed ${removed} track${removed === 1 ? '' : 's'} from the library.`);
  S.sel.clear();
  if (confirm(`Reload the library now so ${removed === 1 ? 'it drops' : 'they drop'} out of mixes and search?`)) {
    startReload();
  }
}

/* ------------------------------------------------------------------ export */
/* One sentence for "some or all of these paths could not be rewritten", used by BOTH export
   buttons. The server hands back local paths rather than refusing the whole export or
   inventing a root (ruling (a), MORNING_REPORT §5.1) -- so the export succeeding is not the
   whole story, and this is the part the operator has to see. */
function fallbackNote(rep) {
  if (!rep || !rep.fallback) return '';
  const all = rep.fallback_count >= rep.total;
  return (all ? `All ${rep.total} paths stayed local`
              : `${rep.fallback_count} of ${rep.total} paths stayed local`)
    + ` instead of ${rep.requested === 'unc' ? 'network' : rep.requested} form`
    + (rep.reason ? `: ${rep.reason}` : '.');
}

async function exportSaveDir() {
  const ids = currentExportIds();
  if (!ids.length) return toast('Nothing to export', true);
  const name = $('plName').value.trim() || 'Attune mix';
  try {
    const j = await jpost('/api/export/m3u_dir',
      { ids, name, flavor: $('flavor').value });
    const note = fallbackNote(j.fallback);
    $('exportMsg').className = note ? 'msg warn' : 'msg ok';
    $('exportMsg').textContent = `Wrote ${j.count} tracks → ${j.name}` + (note ? `. ${note}` : '');
    loadPlaylists();
  } catch (e) { $('exportMsg').className = 'msg err'; $('exportMsg').textContent = e.message; }
}

/* Fetched, not navigated to. A plain `window.location = url` download cannot read a
   response header, so the server's X-Attune-Export-Fallback* report was invisible here and
   this button quietly handed over local paths while the Save-to-folder button beside it
   said what happened (MORNING_REPORT §5.2). Same fetch-then-blob shape app.py's own /mix
   page already uses. */
async function exportDownload() {
  const seed = S.seed ?? firstSelected();
  if (seed == null) return toast('Create a mix first', true);
  const p = mixParams(); p.set('i', seed); p.set('flavor', $('flavor').value);
  $('exportMsg').className = 'msg'; $('exportMsg').textContent = 'Building playlist…';
  try {
    const r = await fetch('/api/export/m3u?' + p);
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      $('exportMsg').className = 'msg err';
      $('exportMsg').textContent = d.error || `export failed (HTTP ${r.status})`;
      return;
    }
    const blob = await r.blob();
    const m = /filename="([^"]+)"/.exec(r.headers.get('Content-Disposition') || '');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = m ? m[1] : 'Attune-mix.m3u8';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(a.href);
    const note = fallbackNote({
      fallback: r.headers.get('X-Attune-Export-Fallback') === '1',
      fallback_count: +r.headers.get('X-Attune-Export-Fallback-Count') || 0,
      total: +r.headers.get('X-Attune-Export-Total') || 0,
      requested: r.headers.get('X-Attune-Export-Flavor-Requested') || '',
      reason: r.headers.get('X-Attune-Export-Fallback-Reason') || '',
    });
    $('exportMsg').className = note ? 'msg warn' : 'msg ok';
    $('exportMsg').textContent = note || `Downloaded ${a.download}`;
  } catch (e) {
    $('exportMsg').className = 'msg err'; $('exportMsg').textContent = e.message;
  }
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
// `overrideIds`, when a real array, wins over the on-screen currentExportIds() — this
// is how the context-menu "Send to Folder" entries (below) target exactly the clicked
// selection or the whole mix regardless of what happens to be selected on screen,
// without a second copy pipeline: same /api/export/copy job, same status polling.
// Guarded with Array.isArray because the export panel binds this straight to
// btnCopy.onclick, which calls it with a MouseEvent as the first argument — a bare
// `overrideIds || currentExportIds()` would treat that event object as the id list.
async function startCopy(overrideIds) {
  const ids = Array.isArray(overrideIds) ? overrideIds : currentExportIds();
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

/* -- Mirror a folder onto a Plex playlist (backend: web/plexsyncjob.py).
   TWO buttons on purpose. "Check the folder" reads and reports and writes nothing;
   "Update Plex" then writes EXACTLY the list the check just showed, which is why the
   Update button only appears after a check and disappears again the moment the folder
   or playlist name is edited. One button would let the folder change between looking
   and writing, and you would be approving a list nobody had read. */
let psTimer = 0;
function psErr(m) { $('psMsg').className = 'msg err'; $('psMsg').textContent = m; }
function psShowApply(on) { $('btnPsApply').hidden = !on; }

async function psBrowse() {
  const picked = await Prefs.pickFolder($('psFolder').value || '');
  if (picked) { $('psFolder').value = picked; psShowApply(false); $('psReport').hidden = true; }
}
async function psCheck() {
  const folder = $('psFolder').value.trim(), title = $('psTitle').value.trim();
  if (!folder) return psErr('Pick the folder to mirror first');
  if (!title) return psErr('Give the playlist a name');
  $('psMsg').className = 'msg'; $('psMsg').textContent = 'Reading your Plex library…';
  psShowApply(false); $('psReport').hidden = true;
  try { await jpost('/api/plexsync/preview', { folder, title }); startPsPoll(); }
  catch (e) { psErr(e.message); }
}
async function psApply() {
  const folder = $('psFolder').value.trim(), title = $('psTitle').value.trim();
  const no_prune = $('psPrune').value === 'keep';
  $('psMsg').className = 'msg'; $('psMsg').textContent = 'Updating the playlist…';
  try { await jpost('/api/plexsync/apply', { folder, title, no_prune }); startPsPoll(); }
  catch (e) { psErr(e.message); }
}
async function psCancel() { try { await jpost('/api/plexsync/cancel'); } catch (e) { psErr(e.message); } }
// Plex only knows about files it has scanned, so a track you restored five minutes ago
// is genuinely absent from its index and the check reports it as missing -- honestly,
// and uselessly. This is the fix, and it runs on the server for minutes.
async function psRescan() {
  $('psMsg').className = 'msg'; $('psMsg').textContent = 'Asking Plex to re-read your library…';
  try {
    await jpost('/api/plexsync/rescan', {});
    $('psMsg').className = 'msg';
    $('psMsg').textContent = 'Plex is re-reading your library. It takes a few minutes on a big ' +
      'library — check the folder again once it settles.';
  } catch (e) { psErr(e.message); }
}
// Human confirmation, not ours: this opens the real playlist on the real server in a
// real browser, so the operator sees it rather than taking Attune's count on trust.
// Server-side webbrowser.open, because a plain link inside the pywebview shell can land
// in the embedded view or nowhere.
let psWebUrl = '';
async function psOpen() {
  if (!psWebUrl) return;
  try { await jpost('/api/plexsync/open', { url: psWebUrl }); }
  catch (e) { psErr(e.message + ' — open it yourself at: ' + psWebUrl); }
}
function psSetLink(url) { psWebUrl = url || ''; $('btnPsOpen').hidden = !psWebUrl; }

function psRows(rows, cls) {
  return rows.map(r => {
    const right = r.status === 'missing'
      ? `<span class="psbad">not in your library, or not scanned yet — looked for ${esc(r.looked_for)}</span>`
      : `${esc(r.artist || '?')} — ${esc(r.title || '?')}` +
        (r.drift != null ? ` <span class="psdrift">${r.drift}s different</span>` : '') +
        (r.tied && r.tied.length ? ` <span class="psdrift">two copies in your library</span>` : '');
    return `<div class="psrow ${cls}"><b>${esc(r.file)}</b><span>${right}</span></div>`;
  }).join('');
}
function paintPs(st) {
  const idx = st.index_total ? Math.round(st.indexed / st.index_total * 100) : 0;
  $('psBar').hidden = !st.running;
  $('psFill').style.width = (st.phase === 'reading plex' ? idx : 100) + '%';
  $('btnPsCheck').hidden = st.running;
  $('btnPsCancel').hidden = !st.running;
  if (st.running) {
    $('psMsg').className = 'msg';
    $('psMsg').textContent = st.phase === 'reading plex'
      ? `Reading your Plex library… ${st.indexed}/${st.index_total || '?'}`
      : (st.lines[st.lines.length - 1] || 'Working…');
    return;
  }
  if (!psTimer || !st.done) return;
  stopPsPoll();
  if (st.error) { psShowApply(false); return psErr(st.error); }
  // An apply that just finished reports what moved; otherwise show the check's answer.
  const a = st.applied, p = st.preview;
  if (a && st.finished && a.when && Math.abs(a.when - st.finished) <= 2) {
    psShowApply(false);
    psSetLink(a.web_url);
    // `a.after` is read back off the server after the write, not a count of what we
    // sent -- so this line is what Plex holds, and the button proves it.
    $('psMsg').className = 'msg ok';
    $('psMsg').textContent = `${a.created ? 'Created' : 'Updated'} “${a.title}” — ` +
      `Plex now holds ${a.after} track${a.after === 1 ? '' : 's'}` +
      (a.added ? `, ${a.added} added` : '') + (a.removed ? `, ${a.removed} removed` : '') +
      (!a.added && !a.removed ? ', nothing to change' : '') +
      '. Click “See it in Plex” to look at it yourself.';
    return;
  }
  if (!p) return;
  const c = p.counts;
  psSetLink(p.web_url);
  $('psMsg').className = c.residue ? 'msg warn' : 'msg ok';
  $('psMsg').textContent = `${c.resolved} of ${c.source} matched` +
    (c.residue ? `, ${c.residue} not in your library` : '') +
    (c.dupes ? `, ${c.dupes} duplicate` : '') +
    (p.existing == null ? ' — the playlist does not exist yet'
                        : ` — the playlist holds ${p.existing} today`) +
    // A library mid-scan has not finished telling Plex what it owns, so "missing" and
    // "not indexed yet" look identical. Say which it is rather than let it read as loss.
    (p.scanning ? ' — Plex is still scanning, so anything below may just not be indexed yet' : '');
  const bits = [];
  if (p.missing.length) bits.push(`<div class="pshd">Cannot be added</div>${psRows(p.missing, 'bad')}`);
  if (p.flagged.length) bits.push(`<div class="pshd">Matched, but worth a look</div>${psRows(p.flagged, 'warn')}`);
  if (p.dupes.length) bits.push(`<div class="pshd">The same song twice in the folder</div>` +
    p.dupes.map(d => `<div class="psrow"><b>${esc(d.file)}</b><span>same as ${esc(d.same_as)}</span></div>`).join(''));
  $('psReport').innerHTML = bits.join('') ||
    '<div class="pshd">Every file matched cleanly.</div>';
  $('psReport').hidden = false;
  psShowApply(true);
}
async function pollPs() {
  try { paintPs(await jget('/api/plexsync/status')); } catch { /* transient */ }
}
function startPsPoll() { if (psTimer) return; psTimer = setInterval(pollPs, 700); pollPs(); }
function stopPsPoll() { clearInterval(psTimer); psTimer = 0; }

// "Send to Folder" (right-click context menu, both scopes) — opens the SAME export
// panel and runs the SAME copy job as the "Take it with you" button above, just with
// an explicit `ids` list instead of currentExportIds(): scope lives entirely in which
// ids the caller passes in, not in a second pipeline.
async function sendToFolder(ids) {
  if (!ids.length) return toast('Nothing to send', true);
  const picked = await Prefs.pickFolder(copyDest || '');
  if (!picked) return;
  copyDest = picked; $('copyDest').value = picked;
  $('optionsPanel').hidden = true;
  $('exportPanel').hidden = false;
  updateSelStatus();
  await startCopy(ids);
}

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
    placeFloating(el, x, y);
  } catch (e) { toast(e.message, true); }
}

/* ---------------------------------------------------------- live mix filters */

/* Everything below re-runs the CURRENT mix through the server with the filters
   applied, so removing a track or an artist backfills from the next-best
   candidates instead of just leaving a shorter list. */

function artistOf(i) {
  const row = S.rows.find(r => r.i === i);
  return (row && row.artist) || '';
}

async function remixWithFilters(msg) {
  if (S.seed == null) return toast('Create a mix first', true);
  // Voting (More/Less) owns the refine path; plain filters re-run the mix path.
  if (S.liked.length || S.disliked.length ||
      S.likedArtists.length || S.dislikedArtists.length) {
    await refine(undefined, msg);
  } else {
    await doMix(S.seed, { keepFilters: true, msg });
  }
  renderFilterBar();
}

function dropTracks(ids) {
  const add = ids.filter(i => i !== S.seed && !S.ban.includes(i));
  if (!add.length) return toast('Nothing to remove (the seed stays)', true);
  // Remember the label NOW: once the track leaves the mix it is gone from S.rows,
  // and the chip would otherwise read "track 1849" instead of the song name.
  for (const i of add) {
    const r = S.rows.find(x => x.i === i);
    if (r) S.banLabel[i] = r.artist ? `${r.artist} — ${r.title}` : r.title;
  }
  S.ban.push(...add);
  remixWithFilters(`Removed ${add.length} track${add.length > 1 ? 's' : ''}`);
}

function dropArtists(names) {
  const add = names.filter(a => a && !S.banArtists.includes(a));
  if (!add.length) return;
  S.banArtists.push(...add);
  remixWithFilters(`Blocked ${add.join(', ')}`);
}

function tuneArtist(names, dir) {
  // accepts one name or a list: the menu items act on the whole selection, same as
  // drop/dropartist. Each name toggles independently; ONE re-mix at the end.
  names = [...new Set((Array.isArray(names) ? names : [names]).filter(Boolean))];
  if (!names.length) return toast('No artist on that track', true);
  const add = dir === 'more' ? S.likedArtists : S.dislikedArtists;
  const other = dir === 'more' ? S.dislikedArtists : S.likedArtists;
  for (const name of names) {
    const at = add.indexOf(name);
    if (at >= 0) { add.splice(at, 1); continue; }
    add.push(name);
    const o = other.indexOf(name);
    if (o >= 0) other.splice(o, 1);
  }
  const label = names.join(', ');
  remixWithFilters(dir === 'more' ? `More like ${label}` : `Less like ${label}`);
}

function clearFilters() {
  S.ban = []; S.banArtists = []; S.likedArtists = []; S.dislikedArtists = [];
  S.liked = []; S.disliked = []; S.banLabel = {};
  remixWithFilters('Filters cleared');
}

function renderFilterBar() {
  const bar = $('filterBar'), chips = $('fbChips');
  if (!bar || !chips) return;
  const items = [
    ...S.ban.map(i => ({ kind: 'track', key: String(i),
                         text: S.banLabel[i]
                               || (S.rows.find(r => r.i === i) || {}).title
                               || `track ${i}` })),
    ...S.banArtists.map(a => ({ kind: 'artist', key: a, text: a })),
    ...S.likedArtists.map(a => ({ kind: 'more', key: a, text: '+ ' + a })),
    ...S.dislikedArtists.map(a => ({ kind: 'less', key: a, text: '− ' + a })),
  ];
  bar.hidden = !items.length;
  chips.innerHTML = items.map(it =>
    `<span class="chip chip-${it.kind}" data-kind="${it.kind}" data-key="${esc(it.key)}"
       title="Click to undo">${esc(it.text)} <b>×</b></span>`).join('');
}

function undoFilter(kind, key) {
  const drop = (arr, v) => { const at = arr.indexOf(v); if (at >= 0) arr.splice(at, 1); };
  if (kind === 'track') drop(S.ban, +key);
  else if (kind === 'artist') drop(S.banArtists, key);
  else if (kind === 'more') drop(S.likedArtists, key);
  else if (kind === 'less') drop(S.dislikedArtists, key);
  remixWithFilters('Filter removed');
}

async function tuneTrack(i, dir) {
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
  // INSTANT acknowledgement — the button lights and (for a fresh "less" vote) the row
  // starts sliding out NOW, not after the server round-trip. Perceived latency is the
  // click-to-first-pixel gap, and this makes it one frame.
  const tr = document.querySelector(`#tbody tr[data-i="${i}"]`);
  if (tr) {
    tr.querySelectorAll('.tb.more').forEach(b => b.classList.toggle('on', S.liked.includes(i)));
    tr.querySelectorAll('.tb.less').forEach(b => b.classList.toggle('on', S.disliked.includes(i)));
    if (dir === 'less' && S.disliked.includes(i)) {
      tr.classList.add('rowOut');
      await new Promise(r => setTimeout(r, 190));   // let the slide-out land first
    }
  }
  // follow a fresh "more" vote to its pinned position; a withdrawn vote or a removal
  // doesn't need the camera move.
  refine(dir === 'more' && S.liked.includes(i) ? i : undefined);
}

async function refine(focusI, msg) {
  if (S.seed == null) return toast('Create a mix first', true);
  if (S.stats.engine !== 'v2') return toast('More/Less Like This needs the V2 engine', true);
  try {
    const j = await jpost('/api/refine', {
      i: S.seed, size: Math.min(Math.max(+$('mixSize').value || 100, 20), 150),
      liked: S.liked, disliked: S.disliked,
      // live filters travel with the refine too, so a removed track/artist can't
      // reappear the moment you vote on something else
      ban: S.ban, ban_artist: S.banArtists,
      liked_artists: S.likedArtists, disliked_artists: S.dislikedArtists,
    });
    const ids = (j.tracks || []).map(x => x.i);
    // Compose: seed, then the liked ANCHORS pinned in the order they were liked, then
    // the server's re-ranked picks. The server excludes liked/disliked from its list
    // (you already have the liked ones — they are pinned, not re-suggested).
    const anchors = S.liked.filter(i => i !== S.seed);
    S.mix = [S.seed, ...anchors,
             ...ids.filter(i => i !== S.seed && !anchors.includes(i))];
    $('mixN').textContent = S.mix.length;
    await showMix({ animate: true });
    renderFilterBar();
    // land the eye: pulse the row you just voted, and if it docked off-screen
    // (a "more" vote pins it up under the seed), follow it there.
    if (focusI != null) {
      const tr = document.querySelector(`#tbody tr[data-i="${focusI}"]`);
      if (tr) {
        tr.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        tr.animate([{ filter: 'brightness(1.7)' }, { filter: 'brightness(1)' }],
                   { duration: 650, easing: 'ease-out' });
      }
    }
    const votes = S.liked.length + S.likedArtists.length;
    const against = S.disliked.length + S.dislikedArtists.length;
    toast(msg || `Steering: +${votes} / −${against}`);
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------- floating-layer placement
   Every fixed-position floater (#ctx, #colMenu, .why) used to be clamped against a
   HARDCODED height budget -- #ctx assumed 430px while the menu had grown to 683px, so
   it hung 253px off the bottom of an 800px viewport with no scroll and no edge flip.
   A budget cannot track content. Measure instead: unhide off-screen, read the real
   box, then place it. Paired with `max-height:calc(100vh - 16px); overflow:auto` in
   the CSS, so a floater taller than the whole window scrolls rather than clipping. */
function placeFloating(el, x, y, pad = 6) {
  el.style.left = '-9999px';
  el.style.top = '0px';
  el.hidden = false;                       // must be laid out before it can be measured
  const w = el.offsetWidth, h = el.offsetHeight;
  el.style.left = Math.max(pad, Math.min(x, innerWidth - w - pad)) + 'px';
  el.style.top = Math.max(pad, Math.min(y, innerHeight - h - pad)) + 'px';
}

/* ------------------------------------------------------------------ column chooser */
function openColMenu(x, y) {
  const m = $('colMenu');
  m.innerHTML = COLS.map(c =>
    `<li data-col="${c.id}"><span class="ck">${visCols.has(c.id) ? '✓' : ''}</span>${c.label}</li>`).join('');
  placeFloating(m, x, y);
}

/* ------------------------------------------------------------------ wiring */
function bindSliders() {
  const link = (id, dp) => {
    const el = $(id), out = $(id + 'V');
    if (!el || !out) return;
    const upd = () => out.textContent = dp ? Number(el.value).toFixed(dp) : el.value;
    el.addEventListener('input', upd); upd();
  };
  ['style', 'variety', 'radioVariety'].forEach(i => link(i, 0));
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
    ({ library: () => loadLibrary(true), mix: showMix, nowplaying: showNowPlaying,
       diagnostics: showDiagnostics, failures: showFailures })[li.dataset.view]();
  });

  // A-Z jump bar
  $('azBar').addEventListener('click', e => {
    const b = e.target.closest('button[data-l]'); if (!b) return;
    jumpToLetter(b.dataset.l);
  });

  // Missing Files: run the file check / stop it
  $('btnVerify').onclick = startVerify;
  $('btnVerifyCancel').onclick = async () => {
    try { await jpost('/api/lib/verify/cancel', {}); } catch (e) { verifyMsg(e.message, 'hint err'); }
  };
  // a check started before this page loaded (a refresh mid-pass) -- surface it, same as
  // the library reload and the copy job above
  jget('/api/lib/verify/status').then(st => {
    if (!st.running) return;
    $('btnVerify').disabled = true;
    $('btnVerifyCancel').hidden = false;
    clearInterval(verifyTimer);
    verifyTimer = setInterval(pollVerify, 800);
  }).catch(() => {});

  // diagnostics: file list selection, refresh, line-count change
  $('diagFileList').addEventListener('click', e => {
    const li = e.target.closest('li[data-name]'); if (!li) return;
    loadDiagTail(li.dataset.name);
  });
  $('diagRefresh').onclick = loadDiagLogs;
  $('diagLines').addEventListener('change', () => {
    if (_diagActiveFile) loadDiagTail(_diagActiveFile);
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

  // right rail: tabs, queue rows, per-row remove
  $('railTabs').addEventListener('click', e => {
    const b = e.target.closest('button[data-rail]'); if (!b) return;
    setRailTab(b.dataset.rail);
  });
  setRailTab(store.get('railTab', 'queue'));
  $('upNext').addEventListener('click', e => {
    const li = e.target.closest('li[data-k]'); if (!li) return;
    if (e.target.closest('.qx')) {
      const i = Player.q[+li.dataset.k];
      if (i != null) {
        Player.removeFromQueue([i]);
        if (S.view === 'nowplaying') showNowPlaying();
      }
      return;
    }
    $('upNext').querySelectorAll('li.sel').forEach(x => x.classList.remove('sel'));
    li.classList.add('sel');
  });
  $('upNext').addEventListener('dblclick', e => {
    const li = e.target.closest('li[data-k]'); if (!li) return;
    Player.playAt(+li.dataset.k);
  });

  // queue tools
  $('qBlend').onclick = blendFromQueue;
  // The rail has no selection of its own, so it always means "what's still to come".
  // Clearing S.sel first stops a stale library selection from hijacking it.
  $('railBlend').onclick = () => {
    const ids = [...new Set(Player.q.slice(Player.pos).filter(i => i >= 0))];
    if (ids.length < 2) return toast('Not enough tracks left in the queue to build from', true);
    doBlend(ids, 'queue');
  };
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

  // steering reset (delegated — viewSub is re-rendered on every showMix)
  $('viewSub').addEventListener('click', e => {
    if (e.target.id === 'steerReset' && S.seed != null) doMix(S.seed);
  });

  // toolbar
  $('btnMix').onclick = () => doMix(firstSelected());
  $('btnGenius').onclick = geniusMix;
  // right rail, Track Info tab: pivot the mix onto the track you're hearing.
  // Same doMix() path as Create Mix — different seed integer, so no ear gate.
  $('tiMixFrom').onclick = () => {
    const i = Player.currentPool();
    if (i < 0) return toast('Nothing is playing', true);
    doMix(i);
  };
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
  // What the paths in the exported playlist will actually say, before you press anything.
  // Attune never guesses a root (ruling (a), MORNING_REPORT §5.1): if one is missing, the
  // export keeps LOCAL paths, and this line has to say so up front -- and name Preferences,
  // which is where the roots live now. It used to read "Not configured in .env", which was
  // the wrong place and said nothing about what would happen.
  const showRoot = () => {
    const r = (S.stats && S.stats.roots) || {};
    const flavor = $('flavor').value;
    const el = $('flavorRoot');
    const target = r[flavor] || '';
    const missing = [];
    if (flavor !== 'local' && !target)
      missing.push(flavor === 'unc' ? 'the network folder other players see'
                                    : 'the library path your Plex server uses');
    // every rewrite is relative to the local root, so an unset local root breaks unc/plex too
    if (flavor !== 'local' && !r.local) missing.push('your own library folder');
    if (flavor === 'local') {
      el.className = 'hint';
      el.textContent = 'Paths will read exactly as your library stores them.';
    } else if (!missing.length) {
      // the root is set -- but does it cover the library? A root one folder off rewrites
      // every path it does cover and gets all of them wrong, which is the failure refusing
      // to guess cannot catch (see PathMapper.coverage).
      const cov = (S.stats && S.stats.root_coverage) || {};
      const short = cov.total && cov.ok < cov.total;
      const tail = target + (flavor === 'plex' ? '/…' : '\\…');   // Plex paths are POSIX
      el.className = short ? 'hint warn' : 'hint';
      el.textContent = short
        ? `Paths will read: ${tail} — but only ${fmt(cov.ok)} of ${fmt(cov.total)} tracks `
          + `sit under your library folder, so ${fmt(cov.total - cov.ok)} will keep local `
          + `paths. Check the Local library root in Preferences, Advanced.`
        : 'Paths will read: ' + tail;
    } else {
      el.className = 'hint warn';
      el.textContent = `Paths will stay local: Attune still needs ${missing.join(' and ')}. `
        + `Set it in Preferences, Advanced`
        + (r.local_suggested && !r.local ? ` (your music looks like it lives under ${r.local_suggested})` : '')
        + '.';
    }
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

  // folder -> Plex playlist mirror. Editing either field retracts the Update button:
  // the parked preview belongs to the folder+title it was taken for, and the server
  // refuses a mismatch anyway — this just stops the button lying about it first.
  $('psBrowse').onclick = psBrowse;
  $('btnPsCheck').onclick = psCheck;
  $('btnPsApply').onclick = psApply;
  $('btnPsCancel').onclick = psCancel;
  $('btnPsRescan').onclick = psRescan;
  $('btnPsOpen').onclick = psOpen;
  $('psTitle').addEventListener('input', () => {
    psShowApply(false); $('psReport').hidden = true; psSetLink('');
  });
  // /api/settings answers {settings, path, restart_keys, env_overrides} -- the values
  // are one level down. Reading the top level looked like it worked and silently left
  // both fields blank on every load.
  jget('/api/settings').then(r => {
    const s = (r && r.settings) || {};
    $('psFolder').value = s.plex_sync_folder || '';
    $('psTitle').value = s.plex_sync_title || 'Car-MP3usb';
  }).catch(() => {});
  jget('/api/plexsync/status').then(st => { if (st.running) startPsPoll(); }).catch(() => {});

  $('reloadNow').onclick = startReload;
  // if a reload was already running before this page loaded (e.g. a refresh mid-reload),
  // surface it instead of silently missing the in-flight job.
  jget('/api/lib/reload/status').then(st => {
    if (!st.running) return;
    $('reloadMsg').textContent = 'Reloading library…';
    $('reloadBar').hidden = false;
    $('reloadNow').disabled = true;
    $('reloadBanner').hidden = false;
    clearInterval(reloadTimer);
    reloadTimer = setInterval(pollReload, 800);
  }).catch(() => {});

  // "Hide missing files" — purely a client-side display toggle (row visibility via
  // CSS; the underlying query/mixes are unchanged). Default off, persisted.
  $('hideMissing').checked = store.get('hideMissing', false);
  document.body.classList.toggle('hideMissing', $('hideMissing').checked);
  $('hideMissing').addEventListener('change', () => {
    store.set('hideMissing', $('hideMissing').checked);
    document.body.classList.toggle('hideMissing', $('hideMissing').checked);
  });

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
    placeFloating(ctx, e.clientX, e.clientY);
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
      case 'blend': doBlend(selectedIds()); break;
      case 'adventure': { const s = selectedIds(); doAdventure(s); break; }
      case 'play': Player.playTrack(i); break;
      case 'playnext': Player.queueAdd(selectedIds().length ? selectedIds() : [i], { next: true });
        if (S.view === 'nowplaying') showNowPlaying();
        toast('Playing next'); break;
      case 'queue': Player.queueAdd(selectedIds().length ? selectedIds() : [i]);
        if (S.view === 'nowplaying') showNowPlaying();
        toast('Queued'); break;
      case 'love': loveTrack(i, !row.loved); break;
      case 'tags': Prefs.openTagEditor(i); break;
      case 'more': tuneTrack(i, 'more'); break;
      case 'less': tuneTrack(i, 'less'); break;
      case 'moreartist':
      case 'lessartist': {
        const names = (selectedIds().length ? selectedIds() : [i])
          .map(artistOf).filter(Boolean);
        tuneArtist(names.length ? names : [row.artist],
                   li.dataset.act === 'moreartist' ? 'more' : 'less');
        break;
      }
      // filters act on the whole selection when there is one, so you can throw out
      // a block of tracks in one go
      case 'drop': dropTracks(selectedIds().length ? selectedIds() : [i]); break;
      case 'dropartist': {
        const names = (selectedIds().length ? selectedIds() : [i])
          .map(artistOf).filter(Boolean);
        dropArtists([...new Set(names)]);
        break;
      }
      case 'artist': S.facets.artist = new Set([row.artist]); loadLibrary(true); break;
      case 'album': S.facets.album = new Set([row.album]); loadLibrary(true); break;
      case 'genre': S.facets.genre = new Set([(row.genre || '').split(/[;,]/)[0].trim()]);
        loadLibrary(true); break;
      case 'why': showWhy(i, e.clientX, e.clientY); break;
      case 'copy': navigator.clipboard.writeText(`${row.artist} — ${row.title}`)
        .then(() => toast('Copied')); break;
      case 'reveal': jpost('/api/reveal', { i }).catch(err => toast(err.message, true)); break;
      case 'locate': locateFile(i); break;
      case 'delete': deleteTracks(selectedIds().length ? selectedIds() : [i]); break;
      // "Send to Folder", both scopes (P0 operator request #1). Appended here rather
      // than interleaved above so this stays a clean, separately-mergeable addition.
      case 'sendsel': sendToFolder(selectedIds().length ? selectedIds() : [i]); break;
      case 'sendmix':
        if (!S.mix.length) { toast('Create a mix first', true); break; }
        sendToFolder(S.mix);
        break;
    }
  });
  // live filter chips: click a chip to undo that one filter, "Clear all" to drop them all
  const fb = $('fbChips');
  if (fb) fb.addEventListener('click', e => {
    const chip = e.target.closest('.chip'); if (!chip) return;
    undoFilter(chip.dataset.kind, chip.dataset.key);
  });
  const fbc = $('fbClear');
  if (fbc) fbc.addEventListener('click', clearFilters);

  document.addEventListener('click', e => {
    if (!e.target.closest('.ctxmenu')) $('ctx').hidden = true;
    if (!e.target.closest('#colMenu') && !e.target.closest('#thead-row')) $('colMenu').hidden = true;
    if (!e.target.closest('.why')) $('why').hidden = true;
    if (!e.target.closest('.popover') && !e.target.closest('#toolbar button')
        && !e.target.closest('#queueTools button'))
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
    else if ((e.ctrlKey || e.metaKey) && k === 'g' && !e.shiftKey) { e.preventDefault(); geniusMix(); }
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
      if (ids.length) {
        Player.queueAdd(ids);
        if (S.view === 'nowplaying') showNowPlaying();
        toast(`Queued ${ids.length}`);
      }
    }
    else if (k === 'z') Player.prev();
    else if (k === 'v') Player.stop();
    else if (k === 'b') Player.next();
    else if (k === 's') Player.toggleShuffle();
    else if (k === 'r') Player.cycleRepeat();
    else if (k === 'd') Player.toggleAutoDj();
    else if (k === 'j') Player.toggleRadio();
    else if (k === 'e') Player.toggleEqPanel();
    else if (e.key === 'F2') { const i = firstSelected(); if (i != null) Prefs.openTagEditor(i); }
    else if (e.key === 'Delete' && S.view === 'nowplaying') {
      const ids = selectedIds();
      if (ids.length) { Player.removeFromQueue(ids); showNowPlaying(); }
    }
    // Del in the mix view throws the selection out of the mix (undoable via the chips)
    else if (e.key === 'Delete' && S.view === 'mix') {
      const ids = selectedIds();
      if (ids.length) dropTracks(ids);
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
  try { renderHead(); bindSliders(); bindEvents(); bindRecipeEvents(); renderAzBar(); }
  catch (e) { console.error('[core] chrome', e); }

  // Read BEFORE loadLibrary(true) below runs -- its renderRows() call clears this same
  // key (S.view is 'library' by then), so the value must be captured into a local first.
  // See the comment on restoreLastMix() for why.
  const savedMix = store.get('lastMix', null);

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
    paintStats(s);
  } catch (e) { console.error('[core] header', e); }

  // Recipes load before the flavor dispatch (the applied default writes the dials the
  // dispatch then reflects), isolated like every other section: a failed
  // /api/recipe/list costs the recipe select, never the library or the player.
  try { await initRecipes(); }
  catch (e) { console.error('[core] recipes', e); }

  try { $('flavor').dispatchEvent(new Event('change')); }
  catch (e) { console.error('[core] flavor', e); }

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

  // Soft, best-effort: a failed rehydration just leaves whatever loadLibrary(true)
  // already rendered on screen, never costs the rest of boot.
  try { await restoreLastMix(savedMix); }
  catch (e) { console.error('[core] restoreLastMix wrapper', e); }

  return softFail ? { ok: false, error: softFail } : { ok: true };
}
