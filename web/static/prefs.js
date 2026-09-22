/* Attune preferences + appearance + tag editor + scan UI + mini mode + splitters.
   Server settings live in settings.json (/api/settings); presentation state
   (theme, splitter sizes, mini) also mirrors to localStorage for instant boot. */
'use strict';

const Prefs = (() => {
  const THEMES = [
    { id: 'bee',      name: 'Bee',      sw: ['#1a1c20', '#212429', '#3d84c6', '#d0a032'] },
    { id: 'attune',   name: 'Attune',   sw: ['#17191c', '#22252a', '#f0a92e', '#00e800'] },
    { id: 'obsidian', name: 'Obsidian', sw: ['#0e1014', '#151922', '#6d8cff', '#8b5cf6'] },
    { id: 'aurora',   name: 'Aurora',   sw: ['#060a08', '#0b120e', '#00e07f', '#37cdbe'] },
    { id: 'crimson',  name: 'Crimson',  sw: ['#120d0d', '#1a1212', '#ff5d47', '#e0a030'] },
    { id: 'daylight', name: 'Daylight', sw: ['#eef0f4', '#ffffff', '#4463d6', '#7c4dd6'] },
  ];
  let serverSettings = null;
  let scanTimer = 0;
  let tagI = null;
  // Plex connection (CONTRACT_CONNECT_2026-09-20 §E, §H, §I). The key field is always
  // blank when the modal opens -- GET /api/settings never returns it -- so `dirty`
  // is the only way save() can tell "typed a new key" apart from "left it alone",
  // which matters because sending a blank one would wipe the saved key (§H).
  let plexTokenDirty = false;
  let plexTest = null;   // last successful POST /api/plex/test response this session
  let recipesLoaded = false;  // the Mixing recipe list has arrived; until then its
                              // <select> reads "" and must not be saved over the top

  /* ---------------------------------------------------------------- theme */
  function applyTheme(id) {
    document.documentElement.dataset.theme = id;
    store.set('theme', id);
  }
  /* Display colour: the LCD readout and the spectrum, on their own. Four classic
     hardware display colours, chosen on 2026-09-22 when he asked for "a modern take on old
     school PC desktop staple": green phosphor (the Winamp default and the house one),
     amber phosphor (the P3 monochrome monitor), the blue-green of a hi-fi deck's vacuum
     fluorescent display, and a modern white. Kept in the browser like the theme's instant
     boot copy; nothing on the server needs it. */
  const LCDS = [
    { id: 'green', name: 'Green phosphor', c: '#00e800' },
    { id: 'amber', name: 'Amber',          c: '#ffb000' },
    { id: 'vfd',   name: 'Hi-fi blue',     c: '#4ff2e0' },
    { id: 'ice',   name: 'Modern white',   c: '#e8f1ff' },
  ];
  function applyLcd(id) {
    if (!LCDS.some(l => l.id === id)) id = 'green';
    if (id === 'green') delete document.documentElement.dataset.lcd;
    else document.documentElement.dataset.lcd = id;
    store.set('lcd', id);
  }
  function paintLcdGrid() {
    const cur = store.get('lcd', 'green');
    $('lcdGrid').innerHTML = LCDS.map(l => `
      <button type="button" class="lcdcard ${l.id === cur ? 'on' : ''}" data-lcd="${l.id}">
        <span class="lcdsw" style="color:${l.c}">0:00</span><span class="nm">${l.name}</span>
      </button>`).join('');
  }
  function applyThemeEarly() {
    applyLcd(store.get('lcd', 'green'));
    // The house default is now 'bee' (the MusicBee-style flat skin); 'attune' remains a
    // first-class choice, one click away. The GUARD is unchanged: a stored theme only
    // wins once the user has EXPLICITLY picked one (themeChosen) — earlier builds
    // persisted 'obsidian' on every boot, which would otherwise pin old installs to a
    // look nobody chose. An existing themeChosen install therefore keeps its theme.
    applyTheme(store.get('themeChosen', false) ? store.get('theme', 'bee') : 'bee');
  }
  function paintThemeGrid() {
    // The setting wins once it is known (contract F2) -- fall back to the localStorage
    // guess only before settings have loaded at all.
    const cur = (serverSettings && serverSettings.theme) || store.get('theme', 'bee');
    $('themeGrid').innerHTML = THEMES.map(t => `
      <div class="themecard ${t.id === cur ? 'on' : ''}" data-theme="${t.id}">
        <div class="sw">${t.sw.map(c => `<i style="background:${c}"></i>`).join('')}</div>
        <div class="nm">${t.name}</div>
      </div>`).join('');
  }

  /* ---------------------------------------------------------------- modal */
  function open() {
    $('prefsWrap').hidden = false;
    $('prefsMsg').textContent = '';
    plexTokenDirty = false;
    paintThemeGrid();
    paintLcdGrid();
    jget('/api/settings').then(j => {
      serverSettings = j.settings;
      $('prefsPath').textContent = j.path;
      $('prefsPath').title = j.path;   // the header clips it to one line; Advanced shows it whole
      // Same value, shown a second time inside the Advanced section itself (contract
      // §I) -- #prefsPath in the modal head stays the id prefs.js has always read/set
      // (kept unchanged so nothing about the existing open/close wiring can break);
      // this mirrors it rather than duplicating that id, which HTML does not allow.
      if ($('prefsPathAdvanced')) $('prefsPathAdvanced').textContent = j.path;
      $('prefDb').value = serverSettings.db_path || '';
      $('prefPlaylistDir').value = serverSettings.playlist_dir || '';
      $('prefEngine').value = serverSettings.engine || 'auto';
      $('prefMipUrl').value = serverSettings.musicip_url || '';
      $('prefMlPython').value = serverSettings.ml_venv_python || '';
      $('prefLocalRoot').value = serverSettings.local_library_root || '';
      $('prefUncRoot').value = serverSettings.unc_library_root || '';
      $('prefPlexRoot').value = serverSettings.plex_library_root || '';
      $('prefScanLaunch').checked = !!serverSettings.scan_on_launch;
      $('prefWatch').checked = !!serverSettings.watch_folders;
      paintFolders(serverSettings.library_folders || []);
      paintFolders(serverSettings.exclude_folders || [], 'excludeFolders');
      paintEnvOverrides(j.env_overrides || {});

      // Playlists and export
      $('prefNameTemplate').value = serverSettings.playlist_name_template || 'like-{seed}';
      $('prefFlavor').value = serverSettings.path_flavor || 'unc';
      $('prefCopyLayout').value = serverSettings.copy_layout || 'flat';
      updateNamePreview();
      updatePlexUrlValidity();
      updateFlavorEnabled();

      // Plex. The key itself never comes back from the server (contract H) -- the
      // field starts blank every time and only the saved/not-saved fact is shown.
      $('prefPlexUrl').value = serverSettings.plex_url || '';
      $('prefPlexToken').value = '';
      $('prefPlexToken').type = 'password';
      $('prefPlexTokenShow').textContent = 'Show';
      $('prefPlexTokenStatus').textContent = serverSettings.plex_token_set
        ? 'A key is saved.' : 'No key saved yet.';
      $('prefPlexTestMsg').className = 'hint'; $('prefPlexTestMsg').textContent = '';
      $('prefPlexServer').textContent = serverSettings.plex_server_name
        ? `Connected to ${serverSettings.plex_server_name}.` : '';
      plexTest = null;
      paintPlexSectionOptions(null);
      $('prefSyncFolder').value = serverSettings.plex_sync_folder || '';
      $('prefSyncTitle').value = serverSettings.plex_sync_title || '';
      $('prefSyncOrder').value = serverSettings.plex_sync_order || 'folder';
      updatePlexSyncEnabled();

      // Mixing — the recipe list is a separate endpoint (GET /api/recipe/list), not
      // part of /api/settings, so it loads independently and degrades on its own.
      loadRecipeOptions();

      // Analysis. ffmpeg_found is nested in settings (web/app.py _public_settings()),
      // not top-level on the response -- verified against the live route, which
      // landed while this file was being written.
      $('prefFfmpegStatus').textContent = serverSettings.ffmpeg_found || 'not found';

      // Theme grid painted again now settings are actually in hand (contract F2) —
      // the earlier call above was only so the grid isn't blank while this loads.
      paintThemeGrid();
    }).catch(e => { $('prefsMsg').className = 'msg err'; $('prefsMsg').textContent = e.message; });
    // playback tab mirrors the player's persisted knobs
    $('prefXfade').value = store.get('xfade', 0);
    $('prefXfadeV').textContent = store.get('xfade', 0) + 's';
    $('prefRg').checked = store.get('rg', false);
  }
  function close() { $('prefsWrap').hidden = true; }

  /* --------------------------------------------------------- Playlists and export
     Name-pattern live preview + validation, and the path-flavor parent/child
     enabling. All three CONTRACT_CONNECT_2026-09-20 §G/§I. */
  const NAME_ILLEGAL_RE = /[<>:"/\\|?*\x00-\x1f]/g;
  // A fixed, recognizable example track — not read from anywhere, just something a
  // person can tell is a preview and not a real result.
  const PREVIEW_SEED = 'Black Dog', PREVIEW_ARTIST = 'Led Zeppelin';
  function dateToken(d, which) {
    const pad = n => String(n).padStart(2, '0');
    const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
      'August', 'September', 'October', 'November', 'December'];
    if (which === 'date') return String(d.getFullYear()).slice(-2) + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    if (which === 'ymd') return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    if (which === 'year') return String(d.getFullYear());
    if (which === 'month') return MONTHS[d.getMonth()];
    return null;
  }
  // Mirrors src/plexmatch.py's resolve_title(): {date}/{ymd}/{year}/{month}, an
  // unknown token left standing as literal text. {seed}/{artist} are this preview's
  // stand-ins for what the server adds by passing them into that same expander
  // (contract G) — this file never re-implements the server's token table, just the
  // display of it, with fixed sample values.
  function expandNameTemplate(tpl, when) {
    const d = when || new Date();
    return (tpl || 'like-{seed}').replace(/\{(\w+)\}/g, (whole, token) => {
      if (token === 'seed') return PREVIEW_SEED;
      if (token === 'artist') return PREVIEW_ARTIST;
      const v = dateToken(d, token);
      return v === null ? whole : v;
    });
  }
  // Mirrors web/app.py export_m3u()'s sanitizer exactly: keep only alphanumerics,
  // space, hyphen and underscore, strip, cap at 60, 'mix' if that leaves nothing.
  function sanitizeFileStem(s) {
    const kept = Array.from(s).filter(c => /[a-zA-Z0-9 _-]/.test(c)).join('');
    return kept.trim().slice(0, 60) || 'mix';
  }
  function updateNamePreview() {
    const tpl = $('prefNameTemplate').value;
    const errEl = $('prefNameTemplateErr');
    if (!tpl.trim()) {
      errEl.hidden = false; errEl.textContent = 'Give your playlists a name pattern.';
    } else {
      const bad = [...new Set(tpl.match(NAME_ILLEGAL_RE) || [])];
      if (bad.length) {
        errEl.hidden = false;
        errEl.textContent = `The character${bad.length > 1 ? 's' : ''} `
          + bad.map(c => `"${c}"`).join(', ')
          + `${bad.length > 1 ? " aren't" : " isn't"} allowed in a file name.`;
      } else { errEl.hidden = true; errEl.textContent = ''; }
    }
    $('prefNamePreview').textContent =
      'Example file name: ' + sanitizeFileStem(expandNameTemplate(tpl)) + '.m3u8';
  }
  const FLAVOR_ROOT = { local: 'prefLocalRoot', unc: 'prefUncRoot', plex: 'prefPlexRoot' };
  function updateFlavorEnabled() {
    const chosen = $('prefFlavor').value;
    for (const [flavor, inputId] of Object.entries(FLAVOR_ROOT)) {
      const input = $(inputId);
      const on = flavor === chosen;
      input.disabled = !on;
      const row = input.closest('.prefrow');
      const reason = row && row.querySelector('.flavorReason');
      if (reason) reason.hidden = on;
    }
  }

  /* ------------------------------------------------------------------------ Plex
     Test connection, forget, and the mirror fields that only turn on once a library
     is picked. CONTRACT_CONNECT_2026-09-20 §E/§I. */
  // Kept in step with src/export.py's valid_plex_url, which is the one that decides.
  // A hint that reddens a field the server then accepts is worse than no hint: it makes
  // a person retype something that was already right. So an IPv6 literal in brackets
  // passes, and so does a trailing slash, which the server strips on save. A name and
  // password in the address is refused in both places.
  const PLEX_URL_RE = /^https?:\/\/(\[[0-9a-f:]+\]|[^/\s:@]+)(:\d+)?\/?$/i;
  function updatePlexUrlValidity() {
    const v = $('prefPlexUrl').value.trim();
    const errEl = $('prefPlexUrlErr');
    if (!v || PLEX_URL_RE.test(v)) { errEl.hidden = true; errEl.textContent = ''; }
    else {
      errEl.hidden = false;
      errEl.textContent = 'That does not look like a server address. It should look '
        + 'like http://192.168.1.50:32400.';
    }
  }
  // libraries === null: nothing tested yet this session — show what is already saved
  // (if anything), from the raw key alone, since a name for it is only known after a
  // test. libraries === []/[...]: a fresh POST /api/plex/test result.
  function paintPlexSectionOptions(libraries) {
    const sel = $('prefPlexSection');
    if (libraries) {
      // Plex does not promise a track count on a section, and a missing one used to
      // render as "Music (null tracks)". Show the count only when there is one.
      sel.innerHTML = libraries.length
        ? libraries.map(l => {
            const n = Number(l.count);
            const size = Number.isFinite(n) && n > 0 ? ` (${n.toLocaleString()} tracks)` : '';
            return `<option value="${esc(l.key)}">${esc(l.title)}${size}</option>`;
          }).join('')
        : '<option value="">No music libraries found</option>';
      const want = serverSettings && serverSettings.plex_section_key;
      if (want && libraries.some(l => String(l.key) === String(want))) sel.value = want;
    } else if (serverSettings && serverSettings.plex_section_key) {
      // This dropdown asks WHICH LIBRARY, and until a test runs Attune only has its
      // number, not its name. Showing the SERVER's name here would answer a different
      // question with something that looks like an answer to this one.
      sel.innerHTML = `<option value="${esc(serverSettings.plex_section_key)}">`
        + `Your saved library - press Test connection to see its name</option>`;
    } else {
      sel.innerHTML = '<option value="">Test the connection to see your libraries</option>';
    }
  }
  function updatePlexSyncEnabled() {
    const on = !!$('prefPlexSection').value;
    $('plexSyncBlock').classList.toggle('off', !on);
    $('plexSyncReason').hidden = on;
    ['prefSyncFolder', 'syncFolderBrowse', 'prefSyncTitle', 'prefSyncOrder'].forEach(id => {
      const el = $(id); if (el) el.disabled = !on;
    });
  }
  async function testPlex() {
    const url = $('prefPlexUrl').value.trim();
    $('prefPlexTestMsg').className = 'hint'; $('prefPlexTestMsg').textContent = 'Testing...';
    const body = { url };
    // Omitted entirely unless this session actually typed a new key — "use the
    // stored one" is how Test works again after a restart when the field is blank.
    if (plexTokenDirty && $('prefPlexToken').value) body.token = $('prefPlexToken').value;
    try {
      const j = await jpost('/api/plex/test', body);
      plexTest = j;
      const libs = j.libraries || [];
      paintPlexSectionOptions(libs);
      $('prefPlexServer').textContent = j.server_name ? `Connected to ${j.server_name}.` : '';
      $('prefPlexTestMsg').className = 'hint ok';
      // A server that answers but will not give up its name leaves server_name empty,
      // and "Connected to , 1 music libraries found." is exactly the broken sentence
      // this message was fixed for once already. Say it without the name instead, and
      // count in English rather than always plural.
      const n = libs.length;
      const found = `${n} music ${n === 1 ? 'library' : 'libraries'} found.`;
      $('prefPlexTestMsg').textContent = j.server_name
        ? `Connected to ${j.server_name}, ${found}`
        : `Connected, ${found}`;
      updatePlexSyncEnabled();
    } catch (e) {
      // e.message is the server's own sentence (contract E4) — never the raw
      // exception, and never logged: nothing here touches console.*.
      $('prefPlexTestMsg').className = 'hint err';
      $('prefPlexTestMsg').textContent = e.message;
    }
  }
  async function forgetPlex() {
    if (!confirm('Remove the saved Plex address and key?')) return;
    try { await jpost('/api/plex/forget', {}); }
    catch (e) {
      $('prefPlexTestMsg').className = 'hint err'; $('prefPlexTestMsg').textContent = e.message;
      return;
    }
    plexTest = null;
    plexTokenDirty = false;
    $('prefPlexUrl').value = '';
    $('prefPlexToken').value = '';
    $('prefPlexToken').type = 'password';
    $('prefPlexTokenShow').textContent = 'Show';
    $('prefPlexTokenStatus').textContent = 'No key saved yet.';
    $('prefPlexServer').textContent = '';
    $('prefPlexTestMsg').className = 'hint'; $('prefPlexTestMsg').textContent = '';
    if (serverSettings) {
      serverSettings.plex_url = ''; serverSettings.plex_token_set = false;
      serverSettings.plex_section_key = ''; serverSettings.plex_machine_id = '';
      serverSettings.plex_server_name = '';
    }
    paintPlexSectionOptions(null);
    updatePlexSyncEnabled();
  }

  /* ---------------------------------------------------------------------- Mixing */
  async function loadRecipeOptions() {
    recipesLoaded = false;
    const sel = $('prefDefaultRecipe');
    sel.innerHTML = '<option value="">- engine defaults -</option>';
    try {
      const j = await jget('/api/recipe/list');
      const cur = (serverSettings && serverSettings.default_recipe) || '';
      sel.innerHTML += (j.recipes || [])
        .map(r => `<option value="${esc(r.name)}">${esc(r.name)}</option>`).join('');
      // A saved default whose recipe has since been renamed or deleted is no longer in
      // the list, and assigning it to a <select> silently does nothing — the control
      // would then read "- engine defaults -" and Save would make that true. Carry it
      // as its own option instead, so what is stored is what is shown.
      if (cur && ![...sel.options].some(o => o.value === cur)) {
        sel.insertAdjacentHTML('beforeend',
          `<option value="${esc(cur)}">${esc(cur)} (not in your recipes any more)</option>`);
      }
      sel.value = cur;
      recipesLoaded = true;
    } catch { /* keep the default-only option — the rest of Mixing still works */ }
  }

  /* A .env file next to the app outranks these fields by design, and nothing used to say
     so: you could edit a root here, save it, restart, and find the old value back, with no
     explanation anywhere (MORNING_REPORT_2026-07-29.md §5.2). The server reports which keys
     a .env is currently shadowing (GET /api/settings -> env_overrides); this says it on the
     field itself and shows the value actually in force. */
  const ENV_NOTE = { local_library_root: ['envLocalRoot', 'prefLocalRoot'],
                     unc_library_root:   ['envUncRoot',   'prefUncRoot'],
                     plex_library_root:  ['envPlexRoot',  'prefPlexRoot'] };
  function paintEnvOverrides(overrides) {
    for (const [key, [noteId, inputId]] of Object.entries(ENV_NOTE)) {
      const note = $(noteId), input = $(inputId);
      if (!note) continue;
      const val = overrides[key];
      note.hidden = !val;
      input.classList.toggle('shadowed', !!val);
      if (val) note.textContent = `A .env file is setting this and wins over anything you `
        + `type here: currently "${val}". Remove it from .env to control this from `
        + `Preferences.`;
    }
  }

  // One widget, three lists, and they are not the same request: the music-folder lists
  // ask for a folder to ADD, the skip list asks for one to LEAVE OUT. Sharing the empty
  // line told someone looking at Folders to skip to "add your music folder(s)", which is
  // the opposite of what that list does.
  const EMPTY_LINE = {
    excludeFolders: 'Nothing skipped. Add a folder here to leave it out of the library.',
  };
  function paintFolders(folders, containerId = 'libFolders') {
    $(containerId).innerHTML = folders.map((f, k) => `
      <div class="fr"><input type="text" value="${esc(f)}" data-k="${k}">
        <button data-del="${k}" title="Remove">✕</button></div>`).join('')
      || `<span class="hint">${EMPTY_LINE[containerId]
           || 'No folders yet — add your music folder(s).'}</span>`;
  }
  function collectFolders(containerId = 'libFolders') {
    return [...$(containerId).querySelectorAll('input')]
      .map(x => x.value.trim()).filter(Boolean);
  }
  // Raw input values with empty rows PRESERVED. collectFolders() (above) filters empties for
  // save/scan; the add-row and browse actions must instead KEEP a blank row the user is
  // about to fill — otherwise "+ Add another" on an all-empty list drops the blank before
  // re-adding one and appears to do nothing.
  function readFolderInputs(containerId = 'libFolders') {
    return [...$(containerId).querySelectorAll('input')].map(x => x.value);
  }
  function focusLast(containerId) {
    const inputs = $(containerId).querySelectorAll('input');
    if (inputs.length) inputs[inputs.length - 1].focus();
  }
  function addFolderRow(containerId, value) {
    const cur = readFolderInputs(containerId).filter(v => v.trim());  // drop blank placeholders
    cur.push(value);
    paintFolders(cur, containerId);
  }
  function firstFolder(containerId) {
    return readFolderInputs(containerId).map(v => v.trim()).filter(Boolean)[0] || '';
  }

  async function save() {
    const patch = {
      db_path: $('prefDb').value.trim(),
      playlist_dir: $('prefPlaylistDir').value.trim(),
      engine: $('prefEngine').value,
      musicip_url: $('prefMipUrl').value.trim() || 'http://localhost:10002',
      ml_venv_python: $('prefMlPython').value.trim(),
      local_library_root: $('prefLocalRoot').value.trim(),
      unc_library_root: $('prefUncRoot').value.trim(),
      plex_library_root: $('prefPlexRoot').value.trim(),
      scan_on_launch: $('prefScanLaunch').checked,
      watch_folders: $('prefWatch').checked,
      library_folders: collectFolders(),
      exclude_folders: collectFolders('excludeFolders'),
      theme: store.get('theme', 'bee'),
      playlist_name_template: $('prefNameTemplate').value.trim(),
      path_flavor: $('prefFlavor').value,
      copy_layout: $('prefCopyLayout').value,
      plex_url: $('prefPlexUrl').value.trim(),
      plex_sync_folder: $('prefSyncFolder').value.trim(),
      plex_sync_title: $('prefSyncTitle').value.trim(),
      plex_sync_order: $('prefSyncOrder').value,
    };
    // Only send the default recipe once its list has actually arrived. The list is
    // fetched asynchronously when the window opens; press Save before it lands, or
    // after that fetch failed, and the control reads "" — which would store "" and
    // silently clear a default the person never touched.
    if (recipesLoaded) patch.default_recipe = $('prefDefaultRecipe').value;
    // The key travels ONLY when this session actually typed one (contract H) — a
    // present-but-blank field would otherwise wipe whatever key was already saved.
    if (plexTokenDirty && $('prefPlexToken').value) patch.plex_token = $('prefPlexToken').value;
    // machine_id/server_name are never typed by hand (contract E4/B): they come from
    // the last successful Test this session; if the modal was reopened and Save
    // pressed without re-testing, keep whatever was already saved instead of
    // blanking them just because this session never ran a test.
    const chosenKey = $('prefPlexSection').value || '';
    patch.plex_section_key = chosenKey;
    if (plexTest && chosenKey) {
      patch.plex_machine_id = plexTest.machine_id || '';
      patch.plex_server_name = plexTest.server_name || '';
    } else {
      patch.plex_machine_id = (serverSettings && serverSettings.plex_machine_id) || '';
      patch.plex_server_name = (serverSettings && serverSettings.plex_server_name) || '';
    }
    try {
      const j = await jpost('/api/settings', patch);
      serverSettings = j.settings;
      plexTokenDirty = false;
      $('prefPlexToken').value = '';
      $('prefPlexToken').type = 'password';
      $('prefPlexTokenShow').textContent = 'Show';
      $('prefPlexTokenStatus').textContent = serverSettings.plex_token_set
        ? 'A key is saved.' : 'No key saved yet.';
      $('prefsMsg').className = 'msg ok';
      $('prefsMsg').textContent = j.needs_restart && j.needs_restart.length
        ? `Saved. Restart Attune for: ${j.needs_restart.join(', ')}`
        : 'Saved.';
    } catch (e) {
      $('prefsMsg').className = 'msg err'; $('prefsMsg').textContent = e.message;
    }
  }

  /* ---------------------------------------------------------------- scan */
  // Rough prior throughput (CPU ONNX embed, ~500 tracks, one earlier session) — used ONLY
  // as a fallback before a live rate is measurable this run. Always labelled "rough".
  const FALLBACK_RATE = 0.83; // tracks/sec

  function fmtDuration(sec) {
    const m = sec / 60;
    if (m < 1) return 'under a minute';
    if (m < 90) return `${Math.round(m)} min`;
    return `${(m / 60).toFixed(1)} hr`;
  }

  function scanEta(st) {
    // Defensive: progress is null early in a stage, and total is never trusted to be >0.
    if (!st.progress || !st.progress[1]) return null;
    const [cur, total] = st.progress;
    const remain = Math.max(0, total - cur);
    if (remain === 0) return { text: 'finishing…', rough: false };
    // Elapsed time in the CURRENT stage only: progress resets to null at the start of each
    // stage (scanjob.py _exec), so subtract seconds already banked by completed stages.
    const priorStagesSec = (st.stages || []).reduce((a, s) => a + (s.seconds || 0), 0);
    const stageElapsed = Math.max(0, (Date.now() / 1000) - st.started - priorStagesSec);
    let rate = null, rough = false;
    if (stageElapsed > 3 && cur > 0) rate = cur / stageElapsed;   // live rate, this run
    if (!rate || !isFinite(rate) || rate <= 0) { rate = FALLBACK_RATE; rough = true; }
    return { text: fmtDuration(remain / rate), rough };
  }

  function paintScan(st) {
    const haveProgress = !!(st.progress && st.progress[1]);
    const pct = haveProgress ? Math.round(st.progress[0] / st.progress[1] * 100) : 0;
    const running = st.running;
    $('scanBanner').hidden = !running;
    if (running) {
      $('scanStage').textContent = st.stage || '…';
      $('scanFill').style.width = pct + '%';
      $('scanPct').textContent = haveProgress
        ? `${st.progress[0].toLocaleString()}/${st.progress[1].toLocaleString()}`
        : '';
      const eta = scanEta(st);
      $('scanEta').textContent = eta
        ? `~${eta.text} left${eta.rough ? ' (rough)' : ''}`
        : (haveProgress ? '' : 'starting…');
    }
    // details inside Preferences (if open)
    if (!$('prefsWrap').hidden) {
      $('scanDetail').hidden = !running && !st.lines.length;
      $('scanFill2').style.width = pct + '%';
      $('scanNote').textContent = running
        ? 'Safe to close the lid — the scan picks up where it left off next time, nothing is lost.'
        : '';
      const log = $('scanLog');
      log.textContent = st.lines.join('\n');
      log.scrollTop = log.scrollHeight;
    }
    $('btnRescan').textContent = running ? '⏹ Cancel scan' : '⟳ Rescan library';
    if (wizLive) paintWizScan(st);
    if (!running && scanTimer && st.finished) {
      stopScanPoll();
      // WHAT IS WORTH LOADING is not the same as what was imported. A scan resumed after
      // a cancel, or one whose first attempt died at the embed stage, adds no catalog
      // rows the second time and does all its real work in the analyze/embed stages --
      // so new_tracks is 0 while hundreds of tracks became mixable. Offering the reload
      // on new_tracks alone left exactly that run stranded until the next restart.
      // scanjob.py reports both deltas now.
      const loadable = (st.new_tracks || 0) + (st.new_analyzed || 0) + (st.new_embedded || 0);
      if (st.error) {
        toast('Scan: ' + st.error, true);
        // The raw error is a line for the scan log, not for someone meeting this app for
        // the first time: it reads "embed failed (rc=1) — see log tail". Say what to
        // press. (Cold Fable audit, 2026-09-20.)
        if (wizLive) wizStop('Attune could not finish reading your music. Open ' +
                             'Preferences, choose Library, and press Rescan library to ' +
                             'try again — it picks up where this left off.', true);
      } else if (st.cancelled) {
        toast('Scan cancelled');
        // Cancelled is not wasted: whatever finished is already in the library.
        if (loadable && typeof offerReload === 'function') offerReload(loadable);
        if (wizLive) wizStop('Stopped. Whatever finished is kept — press Rescan library ' +
                             'in Preferences to carry on where this left off.');
      } else if (loadable) {
        toast(st.new_tracks
          ? `Scan done — ${st.new_tracks.toLocaleString()} new track${st.new_tracks === 1 ? '' : 's'} found.`
          : `Scan done — ${st.new_analyzed.toLocaleString()} track${st.new_analyzed === 1 ? '' : 's'} now analyzed.`);
        // studio.js owns the reload banner/button (it needs S/loadLibrary) -- this
        // module only reports the count, same cross-file global-function pattern
        // studio.js's own toast() already uses from here.
        if (typeof offerReload === 'function') offerReload(loadable);
        // FIRST RUN presses it for them. Someone who has never seen this app should not
        // have to work out that a button called "Load now" is the only thing standing
        // between them and the music they just waited for.
        if (wizLive && typeof startReload === 'function') {
          $('wizStageLine').textContent = 'Loading your library…';
          $('wizFill').style.width = '100%';
          startReload();
        }
      } else {
        toast('Scan done — library is up to date.');
        if (wizLive) wizStop('Nothing new to add — that folder holds no music Attune ' +
                             'can read. Close this, open Preferences and try another one.');
      }
    }
  }
  async function pollScan() {
    try { paintScan(await jget('/api/scan/status')); }
    catch { /* server briefly busy — keep polling */ }
  }
  function startScanPoll() {
    if (scanTimer) return;
    scanTimer = setInterval(pollScan, 1200);
    pollScan();
  }
  function stopScanPoll() { clearInterval(scanTimer); scanTimer = 0; }

  async function rescan() {
    const st = await jget('/api/scan/status').catch(() => null);
    if (st && st.running) {
      await jpost('/api/scan/cancel').catch(() => {});
      return;
    }
    const folders = collectFolders();
    try {
      await jpost('/api/scan/start', folders.length ? { folders } : {});
      startScanPoll();
      $('scanDetail').hidden = false;
    } catch (e) { toast(e.message, true); }
  }

  /* ---------------------------------------------------------------- first-run wizard

     Two panes in one modal: pick folders, then watch the scan through to music you can
     actually mix. `wizLive` is true from pressing Scan until the handover (or an error),
     and it is what lets paintScan() above drive the second pane and load the library
     without making a first-time user find the "Load now" button themselves. */
  let wizLive = false;

  async function checkFirstRun() {
    let j;
    try { j = await jget('/api/settings'); } catch { return; }  // unreachable — don't nag on a fluke
    serverSettings = j.settings;
    // Theme: the SETTING wins on first paint once settings are known (contract F2).
    // applyThemeEarly() already painted a guess from localStorage before this fetch
    // resolved (so the window never flashes unstyled); this is the earliest point in
    // boot serverSettings is known, so it corrects that guess to what was actually
    // saved. checkFirstRun() runs on every boot regardless of whether the wizard
    // itself ends up showing (the early returns below are after this line).
    if (serverSettings.theme) applyTheme(serverSettings.theme);

    // NOTHING TO PLAY is the one condition that opens this, and it is a fact about the
    // library, not about the settings. A configured scan folder is not the same thing as
    // having a library: library_folders only tells the scanner where to look for NEW
    // music and is routinely empty on a perfectly good install (cleared after a scan
    // test, say). Gating on it alone once put a full-screen modal over a working
    // 21,236-track library and made the whole app unclickable, so `analyzed > 0` is the
    // gate. Unknown (stats missing) is NOT a reason to block the UI.
    //
    // It no longer returns early when library_folders is set, either: a first run
    // interrupted mid-scan saves the folder and then has nothing analyzed, and bailing
    // out there left exactly the person who most needed this staring at an empty window.
    // Their folders come back prefilled instead.
    if (!S.stats || (S.stats.analyzed || 0) > 0) return;
    if (store.get('wizardSkipped', false)) return;   // don't re-nag every launch

    const known = (serverSettings.library_folders || []).filter(Boolean);
    paintFolders(known.length ? known : [''], 'wizFolders');
    wizLive = false;
    $('wizSetup').hidden = false;
    $('wizProgress').hidden = true;
    $('wizStart').hidden = false;
    $('wizFinish').hidden = true;
    $('wizSkip').textContent = 'Skip for now';
    $('wizMsg').className = 'msg';
    $('wizMsg').textContent = '';
    $('wizWrap').hidden = false;
  }

  function wizShowProgress() {
    wizLive = true;
    $('wizSetup').hidden = true;
    $('wizProgress').hidden = false;
    $('wizStart').hidden = true;
    $('wizFinish').hidden = true;
    $('wizSkip').textContent = 'Close';
    $('wizMsg').className = 'msg';
    $('wizMsg').textContent = '';
    $('wizStageLine').textContent = 'Reading your music…';
    $('wizFill').style.width = '0%';
  }

  function paintWizScan(st) {
    const have = !!(st.progress && st.progress[1]);
    if (have) $('wizFill').style.width = Math.round(st.progress[0] / st.progress[1] * 100) + '%';
    if (!st.running) return;
    const eta = scanEta(st);
    $('wizStageLine').textContent =
      (st.stage ? st.stage[0].toUpperCase() + st.stage.slice(1) : 'Working') +
      (have ? ` — ${st.progress[0].toLocaleString()} of ${st.progress[1].toLocaleString()}` : '') +
      (eta ? `, about ${eta.text} left${eta.rough ? ' (rough)' : ''}` : '');
  }

  /* The scan ended and there is nothing to hand over: a failure, a cancel, or a folder
     with no music in it. Say which, leave the way back on screen, and stop pretending
     progress is still happening. */
  function wizStop(msg, isErr) {
    wizLive = false;
    $('wizStageLine').textContent = msg;
    $('wizFill').style.width = '0%';
    $('wizMsg').className = isErr ? 'msg err' : 'msg';
    $('wizSkip').textContent = 'Close';
    $('wizFinish').hidden = true;
  }

  /* The music is loaded. Whether any of it is MIXABLE is a separate question and the
     answer is the pool count, so this is the one place the wizard is allowed to claim
     success -- and only when that number is above zero.

     A POOL OF ZERO IS NOT A HAPPY ENDING. Point a first run at a folder of files Attune
     cannot read (iTunes .m4a on a build with no ffmpeg is the easy one) and the import
     stage happily adds them all, every analysis fails, the scan still exits clean, and
     the reload comes back having loaded nothing. Saying "your library is loaded" there,
     over a list reading "Nothing here", is the quiet wrong answer this whole stream
     exists to remove. Both cold auditors found it independently, 2026-09-20. */
  function wizReady(pool) {
    wizLive = false;
    $('wizFill').style.width = '100%';
    $('wizFinish').hidden = false;
    if (!pool) {
      $('wizStageLine').textContent = 'Attune read your files, but none of them is ready to mix.';
      $('wizNote').textContent =
        'Usually that means Attune could not open the audio. Open the Library list and ' +
        'choose Not Mixable: it gives a reason for each file.';
      return;
    }
    $('wizStageLine').textContent =
      `${pool.toLocaleString()} song${pool === 1 ? '' : 's'} ready to mix.`;
    $('wizNote').textContent =
      'Pick any song in the Library list and press Create Mix. Attune builds a playlist ' +
      'of tracks that sound like it.';
  }

  // studio.js's pollReload() calls this when a hot reload finishes, whichever way it
  // went -- the same cross-file global-function pattern offerReload() uses in the other
  // direction. Only the wizard cares, and only while it is on screen.
  window.onLibraryReloaded = function (st) {
    if ($('wizWrap').hidden) return;
    if (st && st.error) {
      wizStop('Your music is scanned, but Attune could not load it just now (' +
              st.error + '). Close and start Attune again and it will be there.', true);
      return;
    }
    wizReady((st && st.new_count) || 0);
  };

  async function wizStart() {
    const folders = collectFolders('wizFolders');
    if (!folders.length) {
      $('wizMsg').className = 'msg err'; $('wizMsg').textContent = 'Add at least one folder.';
      return;
    }
    $('wizMsg').className = 'msg'; $('wizMsg').textContent = 'Saving…';
    try {
      const j = await jpost('/api/settings', { library_folders: folders });
      serverSettings = j.settings;
      await jpost('/api/scan/start', { folders });
      wizShowProgress();
      startScanPoll();
    } catch (e) {
      $('wizMsg').className = 'msg err'; $('wizMsg').textContent = e.message;
    }
  }

  /* ---------------------------------------------------------------- folder picker */
  // Server-side directory browser (/api/fs/dirs). pickFolder() returns Promise<string|null>:
  // the absolute path chosen, or null if cancelled. Same behaviour in the browser and the
  // desktop window — the listing comes from the server's filesystem, no native dialog.
  let fsResolve = null;      // resolver of the in-flight pickFolder()/pickFile() promise
  let fsCurrent = '';        // folder currently listed ('' = the drives / top level)
  let fsUpTarget = null;     // where Up navigates (null = already at the top level)
  let fsFileMode = false;    // true while pickFile() is open: also list+accept audio files

  async function fsLoad(path) {
    $('fsMsg').textContent = '';
    let j;
    try { j = await jget('/api/fs/dirs?path=' + encodeURIComponent(path || '')); }
    catch (e) { $('fsMsg').className = 'msg err'; $('fsMsg').textContent = e.message; return; }
    fsCurrent = j.path || '';
    fsUpTarget = (j.parent === null || j.parent === undefined) ? null : j.parent;
    // fsPath is a real <input>, not a label: the value is selectable/copyable by the
    // browser for free, and it doubles as the address bar (see the keydown/paste
    // wiring in bind()). Empty (top level) leaves the value blank so a bare Enter
    // doesn't re-submit the literal placeholder text as a path.
    $('fsPath').value = fsCurrent;
    $('fsUp').disabled = fsUpTarget === null;
    // In file mode there is no valid "choose the folder itself" outcome — only a
    // listed audio file resolves the picker (see the fsList click handler below).
    $('fsChoose').disabled = fsFileMode || !fsCurrent;
    $('fsChoose').title = fsFileMode ? 'Pick an audio file below' : '';
    $('fsNewFolder').disabled = !fsCurrent;
    let html = j.dirs.length
      ? j.dirs.map(d =>
          `<div class="fsrow" data-path="${esc(d.path)}" title="${esc(d.path)}">` +
          `<span class="fsname">📁 ${esc(d.name)}</span>` +
          `<button type="button" class="fsren" data-rename="${esc(d.path)}" data-name="${esc(d.name)}" title="Rename">✎</button>` +
          `</div>`).join('')
      : '';
    // File mode (the relink "Locate file…" flow): also list audio files directly in
    // this folder — a sibling call to /api/fs/dirs, same loopback-guarded shape,
    // extension-filtered server-side (GET /api/fs/files, libverify.py).
    if (fsFileMode && fsCurrent) {
      try {
        const jf = await jget('/api/fs/files?path=' + encodeURIComponent(fsCurrent));
        html += jf.files.map(f =>
          `<div class="fsrow fsfile" data-file="${esc(f.path)}" title="${esc(f.path)}">` +
          `<span class="fsname">🎵 ${esc(f.name)}</span></div>`).join('');
      } catch { /* non-fatal — folder navigation still works */ }
    }
    $('fsList').innerHTML = html ||
      `<div class="hint" style="padding:8px">No ${fsFileMode ? 'sub-folders or audio files' : 'sub-folders'} here.</div>`;
    $('fsList').scrollTop = 0;
  }
  function pickFolder(startPath) {
    // a Browse re-click while the picker is already open must not orphan the first
    // caller's promise -- resolve it as a cancel before installing the new resolver.
    if (fsResolve) { const r = fsResolve; fsResolve = null; r(null); }
    fsFileMode = false;
    $('fsChoose').textContent = 'Choose this folder';
    $('fsWrap').hidden = false;
    fsLoad(startPath || '');
    return new Promise(res => { fsResolve = res; });
  }
  // Same modal, file-picking mode: navigate folders exactly like pickFolder, but a
  // click on a listed audio file resolves immediately with that file's path. Backs
  // studio.js's "Locate file…" (relink) flow — reuse, not a second dialog.
  function pickFile(startPath) {
    if (fsResolve) { const r = fsResolve; fsResolve = null; r(null); }
    fsFileMode = true;
    $('fsChoose').textContent = 'Pick a file below';
    $('fsWrap').hidden = false;
    fsLoad(startPath || '');
    return new Promise(res => { fsResolve = res; });
  }
  function fsClose(result) {
    $('fsWrap').hidden = true;
    fsFileMode = false;
    const r = fsResolve; fsResolve = null;
    if (r) r(result);
  }

  // ---- New folder / rename, mini-file-explorer style. Both end by re-running fsLoad
  // (the same single listing path every navigation already uses — browser and desktop
  // share it) rather than patching the DOM by hand, so the row set, sort order and
  // disabled states never drift from the server's view of the folder.
  // `onCommit` fires with a non-empty trimmed value on Enter or on blur (blur commits,
  // matching Explorer); `onCancel` fires on Escape and never touches the server.
  function _fsEditRow(row, initial, { onCommit, onCancel }) {
    row.classList.add('fsedit');
    row.innerHTML = '📁 <input type="text" class="fseditinput" spellcheck="false" autocomplete="off">';
    const inp = row.querySelector('input');
    inp.value = initial;
    inp.focus();
    inp.select();
    let settled = false;
    const finish = commit => {
      if (settled) return;
      settled = true;
      const v = inp.value.trim();
      if (commit && v) onCommit(v); else onCancel();
    };
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    inp.addEventListener('blur', () => finish(true));
  }
  function fsNewFolder() {
    if (!fsCurrent) return;                 // no parent to create inside (top level / drives)
    const row = document.createElement('div');
    row.className = 'fsrow';
    $('fsList').prepend(row);
    _fsEditRow(row, 'New folder', {
      // Unlike rename, an UNCHANGED "New folder" is still a real name to create
      // (Explorer's own default-accept behaviour) — onCommit always gets a name here.
      onCommit: async name => {
        row.remove();
        try {
          await jpost('/api/fs/mkdir', { path: fsCurrent, name });
          fsLoad(fsCurrent);
        } catch (e) { $('fsMsg').className = 'msg err'; $('fsMsg').textContent = e.message; }
      },
      onCancel: () => row.remove(),
    });
  }
  function fsRenameRow(row) {
    const oldPath = row.dataset.rename;
    const oldName = row.dataset.name;
    _fsEditRow(row, oldName, {
      onCommit: async name => {
        if (name === oldName) { fsLoad(fsCurrent); return; }  // no real change -> just repaint
        try {
          await jpost('/api/fs/rename', { path: oldPath, name });
          fsLoad(fsCurrent);
        } catch (e) { $('fsMsg').className = 'msg err'; $('fsMsg').textContent = e.message; }
      },
      onCancel: () => fsLoad(fsCurrent),
    });
  }

  /* ---------------------------------------------------------------- tag editor */
  const TAGMAP = { tgTitle: 'title', tgArtist: 'artist', tgAlbum: 'album',
    tgAlbumArtist: 'albumartist', tgGenre: 'genre', tgDate: 'date',
    tgTrack: 'tracknumber', tgDisc: 'discnumber', tgComposer: 'composer',
    tgComment: 'comment' };

  async function openTagEditor(i) {
    tagI = i;
    $('tagWrap').hidden = false;
    $('tagMsg').textContent = '';
    for (const id of Object.keys(TAGMAP)) $(id).value = '';
    $('tagTech').textContent = 'Reading tags…';
    try {
      const j = await jget('/api/track/tags?i=' + i);
      for (const [id, tag] of Object.entries(TAGMAP)) $(id).value = j.tags[tag] || '';
      $('tagFile').textContent = j.file;
      const inf = j.info || {};
      $('tagTech').textContent =
        `${j.folder}\n${inf.codec || '?'} · ${Math.round((inf.bitrate || 0) / 1000)} kbps · ` +
        `${inf.sample_rate || '?'} Hz · ${inf.channels || '?'} ch · ` +
        `${(j.bytes / 1048576).toFixed(1)} MB`;
    } catch (e) {
      $('tagTech').textContent = '';
      $('tagMsg').className = 'msg err'; $('tagMsg').textContent = e.message;
    }
  }
  async function saveTags() {
    if (tagI == null) return;
    const tags = {};
    for (const [id, tag] of Object.entries(TAGMAP)) tags[tag] = $(id).value;
    $('tagMsg').className = 'msg'; $('tagMsg').textContent = 'Writing…';
    try {
      const j = await jpost('/api/track/tags', { i: tagI, tags });
      // refresh the row wherever it currently shows
      for (const r of S.rows) if (r.i === tagI) Object.assign(r, j.row);
      renderRows(S.rows, { seed: S.seed });
      if (Player.currentPool() === tagI) {
        $('lcdTrack').textContent = `${j.row.artist || '?'} - ${j.row.title}`;
        $('npArtistSmall').textContent = j.row.album || '';
      }
      $('tagMsg').className = 'msg ok'; $('tagMsg').textContent = 'Saved to file.';
      setTimeout(() => { $('tagWrap').hidden = true; }, 600);
    } catch (e) {
      $('tagMsg').className = 'msg err'; $('tagMsg').textContent = e.message;
    }
  }

  /* ---------------------------------------------------------------- mini mode */
  function toggleMini(force) {
    const on = force !== undefined ? force : !document.body.classList.contains('mini');
    document.body.classList.toggle('mini', on);
    $('miniBar').hidden = !on;
    store.set('mini', on);
  }

  /* ------------------------------------------------------------- right rail
     Collapse state persists like every other layout knob. With NO stored value the
     rail is on at >=1100px and collapsed below, so a narrow window does not open
     with three columns fighting over the width. */
  function applyRail(on) {
    document.body.classList.toggle('norail', !on);
    store.set('rail', on);
    const b = $('btnRail'); if (b) b.classList.toggle('on', on);
    if (on && typeof queueChanged === 'function') queueChanged();
  }
  function initRail() {
    const stored = store.get('rail', null);
    applyRail(stored === null ? innerWidth >= 1100 : !!stored);
  }

  /* ---------------------------------------------------------------- splitters */
  function bindSplitters() {
    const grid = $('grid');
    const leftW = store.get('leftw', 230);
    const rightW = store.get('rightw', 300);
    const facetH = store.get('faceth', 210);
    grid.style.setProperty('--leftw', leftW + 'px');
    grid.style.setProperty('--rightw', rightW + 'px');
    $('facets').style.flexBasis = facetH + 'px';
    document.documentElement.style.setProperty('--faceth', facetH + 'px');

    const drag = (el, onMove, onEnd) => {
      el.addEventListener('pointerdown', e => {
        e.preventDefault();
        el.classList.add('drag');
        el.setPointerCapture(e.pointerId);
        const move = ev => onMove(ev);
        const up = ev => {
          el.classList.remove('drag');
          el.removeEventListener('pointermove', move);
          el.removeEventListener('pointerup', up);
          onEnd && onEnd(ev);
        };
        el.addEventListener('pointermove', move);
        el.addEventListener('pointerup', up);
      });
    };
    drag($('splitLeft'), e => {
      const w = Math.min(Math.max(e.clientX - grid.getBoundingClientRect().left, 180), 480);
      grid.style.setProperty('--leftw', w + 'px');
    }, () => store.set('leftw', parseInt(grid.style.getPropertyValue('--leftw'))));
    drag($('splitFacets'), e => {
      const top = $('facets').getBoundingClientRect().top;
      const h = Math.min(Math.max(e.clientY - top, 80), 420);
      $('facets').style.flexBasis = h + 'px';
    }, () => store.set('faceth', parseInt($('facets').style.flexBasis)));
    // same pattern as splitLeft, measured from the grid's right edge instead
    drag($('splitRight'), e => {
      const w = Math.min(Math.max(grid.getBoundingClientRect().right - e.clientX, 200), 560);
      grid.style.setProperty('--rightw', w + 'px');
    }, () => store.set('rightw', parseInt(grid.style.getPropertyValue('--rightw'))));
  }

  /* ---------------------------------------------------------------- wiring */
  function bind() {
    $('btnPrefs').onclick = open;
    $('prefsSave').onclick = save;
    $('btnMini').onclick = () => toggleMini();
    $('btnRail').onclick = () => applyRail(document.body.classList.contains('norail'));
    $('miniExpand').onclick = () => toggleMini(false);
    $('tagSave').onclick = saveTags;
    $('btnRescan').onclick = rescan;
    $('scanShow').onclick = () => { open(); };
    // Add-row reads RAW values (readFolderInputs) so a blank row is actually added even
    // when the current fields are empty; Browse opens the server-side folder picker.
    $('addFolder').onclick = () => {
      const cur = readFolderInputs(); cur.push('');
      paintFolders(cur); focusLast('libFolders');
    };
    $('libBrowse').onclick = async () => {
      const p = await pickFolder(firstFolder('libFolders'));
      if (p) addFolderRow('libFolders', p);
    };
    $('libFolders').addEventListener('click', e => {
      const del = e.target.closest('button[data-del]'); if (!del) return;
      const cur = collectFolders(); cur.splice(+del.dataset.del, 1);
      paintFolders(cur);
    });
    // Folders to skip (exclude_folders) — the exact same folderlist widget and
    // Browse/Add/Remove functions as Music folders above, pointed at a second
    // container id. pickFolder() is the one server-side picker both go through.
    $('addExclude').onclick = () => {
      const cur = readFolderInputs('excludeFolders'); cur.push('');
      paintFolders(cur, 'excludeFolders'); focusLast('excludeFolders');
    };
    $('excludeBrowse').onclick = async () => {
      const p = await pickFolder(firstFolder('excludeFolders'));
      if (p) addFolderRow('excludeFolders', p);
    };
    $('excludeFolders').addEventListener('click', e => {
      const del = e.target.closest('button[data-del]'); if (!del) return;
      const cur = collectFolders('excludeFolders'); cur.splice(+del.dataset.del, 1);
      paintFolders(cur, 'excludeFolders');
    });
    $('prefPlaylistDirBrowse').onclick = async () => {
      const p = await pickFolder($('prefPlaylistDir').value.trim());
      if (p) $('prefPlaylistDir').value = p;
    };
    // Playlists and export
    $('prefNameTemplate').addEventListener('input', updateNamePreview);
    $('prefFlavor').addEventListener('change', updateFlavorEnabled);
    // Plex
    $('prefPlexUrl').addEventListener('input', updatePlexUrlValidity);
    $('prefPlexToken').addEventListener('input', () => { plexTokenDirty = true; });
    $('prefPlexTokenShow').onclick = () => {
      const show = $('prefPlexToken').type === 'password';
      $('prefPlexToken').type = show ? 'text' : 'password';
      $('prefPlexTokenShow').textContent = show ? 'Hide' : 'Show';
    };
    $('prefPlexForget').onclick = forgetPlex;
    // The server opens it, not the page: a plain external link inside the pywebview
    // shell can land in the embedded view or nowhere at all. If that fails, show the
    // address so the person can still get there by hand.
    $('prefPlexKeyHelp').onclick = async () => {
      const msg = $('prefPlexKeyHelpMsg');
      msg.className = 'hint'; msg.textContent = 'Opening Plex’s instructions in your browser...';
      try {
        const j = await jpost('/api/help/plex-key', {});
        msg.textContent = 'Opened in your browser: ' + j.url;
      } catch (e) {
        msg.className = 'hint err';
        msg.textContent = 'Could not open your browser. The page is at '
          + 'support.plex.tv, article "Finding an authentication token".';
      }
    };
    $('prefPlexTest').onclick = testPlex;
    $('prefPlexSection').addEventListener('change', updatePlexSyncEnabled);
    $('syncFolderBrowse').onclick = async () => {
      const p = await pickFolder($('prefSyncFolder').value.trim());
      if (p) $('prefSyncFolder').value = p;
    };
    // Advanced — the one existing read-only diagnostics route (web/applog.py), just
    // shown rather than opened: there is no route that opens a folder in Explorer.
    $('prefLogsOpen').onclick = async () => {
      $('prefLogsInfo').textContent = 'Reading...';
      try {
        const j = await jget('/api/diag/logs');
        const n = (j.files || []).length;
        $('prefLogsInfo').textContent = n
          ? `${n} log file${n === 1 ? '' : 's'} in ${j.dir}`
          : `No log files yet, in ${j.dir}`;
      } catch (e) { $('prefLogsInfo').textContent = e.message; }
    };
    // first-run wizard folder controls (its own list; Skip/✕ close via generic [data-close])
    $('wizAddFolder').onclick = () => {
      const cur = readFolderInputs('wizFolders'); cur.push('');
      paintFolders(cur, 'wizFolders'); focusLast('wizFolders');
    };
    $('wizBrowse').onclick = async () => {
      const p = await pickFolder(firstFolder('wizFolders'));
      if (p) addFolderRow('wizFolders', p);
    };
    $('wizFolders').addEventListener('click', e => {
      const del = e.target.closest('button[data-del]'); if (!del) return;
      const cur = collectFolders('wizFolders'); cur.splice(+del.dataset.del, 1);
      paintFolders(cur, 'wizFolders');
    });
    $('wizStart').onclick = wizStart;
    // "Start mixing" is a handover, not a skip: it only appears once the library is
    // loaded, so it must NOT set wizardSkipped the way the generic [data-close] buttons
    // do. Landing them on the Library list is the point -- that is where a seed is
    // picked, and an empty-handed "go on then" is how a first run quietly fails.
    // "Start mixing" makes the first mix and plays it (2026-09-22). It used to close the
    // window onto a list of every song with the one instruction ("pick any song, press
    // Create Mix") closing with it. Genius picks the song, so the first thing a new person
    // meets is music. With nothing mixable it falls back to the old refresh.
    $('wizFinish').onclick = async () => {
      $('wizWrap').hidden = true;
      try { if (typeof loadLibrary === 'function') await loadLibrary(true); }
      catch (e) { console.error('[wizard] library refresh', e); }
      try {
        if (typeof geniusMix === 'function' && S.stats && (S.stats.songs || 0) > 0) {
          await geniusMix();
        }
      } catch (e) { console.error('[wizard] first mix', e); }
    };
    // folder picker (its own close buttons resolve the pickFolder() promise)
    $('fsList').addEventListener('click', e => {
      const ren = e.target.closest('.fsren');
      if (ren) { e.stopPropagation(); fsRenameRow(ren.closest('.fsrow')); return; }
      const row = e.target.closest('.fsrow'); if (!row) return;
      if (row.classList.contains('fsfile')) { fsClose(row.dataset.file); return; }
      fsLoad(row.dataset.path);
    });
    $('fsUp').onclick = () => { if (fsUpTarget !== null) fsLoad(fsUpTarget); };
    $('fsChoose').onclick = () => { if (fsCurrent) fsClose(fsCurrent); };
    $('fsNewFolder').onclick = fsNewFolder;
    // fsPath doubles as an address bar: typing + Enter navigates there; a paste
    // navigates immediately (read the value on the next tick, after the paste lands)
    // without waiting for Enter, matching Explorer's address-bar paste behaviour.
    // Same fsLoad() as clicking a row -- one navigation path for the whole modal.
    $('fsPath').addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); fsLoad($('fsPath').value.trim()); $('fsPath').blur(); }
      else if (e.key === 'Escape') { e.preventDefault(); $('fsPath').value = fsCurrent; $('fsPath').blur(); }
    });
    $('fsPath').addEventListener('paste', () => {
      setTimeout(() => fsLoad($('fsPath').value.trim()), 0);
    });
    $('fsCancel').onclick = () => fsClose(null);
    $('fsX').onclick = () => fsClose(null);
    $('fsWrap').addEventListener('mousedown', e => { if (e.target === $('fsWrap')) fsClose(null); });
    // section nav (was a horizontal tab strip, #prefTabs; same click-to-switch
    // idiom, now a vertical list of eight, #prefNav — CONTRACT_CONNECT_2026-09-20 §I)
    $('prefNav').addEventListener('click', e => {
      const b = e.target.closest('button[data-tab]'); if (!b) return;
      $('prefNav').querySelectorAll('button').forEach(x =>
        x.classList.toggle('on', x === b));
      document.querySelectorAll('.tabpage').forEach(p =>
        p.hidden = p.dataset.page !== b.dataset.tab);
    });
    // display colour cards: live, no Save needed
    $('lcdGrid').addEventListener('click', e => {
      const c = e.target.closest('.lcdcard'); if (!c) return;
      applyLcd(c.dataset.lcd);
      paintLcdGrid();
    });
    // theme cards
    $('themeGrid').addEventListener('click', e => {
      const c = e.target.closest('.themecard'); if (!c) return;
      applyTheme(c.dataset.theme);
      store.set('themeChosen', true);
      // Picking a theme is included in the next Save's patch for free (save() reads
      // it back out of `store`, which applyTheme() above just updated) — this just
      // keeps THIS session's in-memory copy in step too, so a grid repaint before
      // the next server round trip still shows the right card as chosen.
      if (serverSettings) serverSettings.theme = c.dataset.theme;
      paintThemeGrid();
    });
    // playback tab mirrors -> player state (same localStorage keys player.js reads)
    $('prefXfade').addEventListener('input', () => {
      const v = +$('prefXfade').value;
      $('prefXfadeV').textContent = v + 's';
      $('xfade').value = v; $('xfade').dispatchEvent(new Event('input'));
    });
    $('prefRg').addEventListener('change', () => {
      $('rgOn').checked = $('prefRg').checked;
      $('rgOn').dispatchEvent(new Event('change'));
    });
    // generic modal close
    document.querySelectorAll('[data-close]').forEach(b =>
      b.onclick = () => {
        // Skipping the first-run wizard is remembered, so a genuinely empty library
        // asks once instead of blocking the window on every single launch.
        if (b.dataset.close === 'wizWrap') store.set('wizardSkipped', true);
        $(b.dataset.close).hidden = true;
      });
    document.querySelectorAll('.modalwrap').forEach(w =>
      w.addEventListener('mousedown', e => {
        if (e.target !== w) return;
        if (w.id === 'wizWrap') store.set('wizardSkipped', true);
        w.hidden = true;
      }));
  }

  function init() {
    bind();
    bindSplitters();
    initRail();
    if (store.get('mini', false)) toggleMini(true);
    // if a scan is already running (started before this page load), surface it
    jget('/api/scan/status').then(st => { if (st.running) startScanPoll(); }).catch(() => {});
  }

  // pickFolder is exposed so studio.js's "Copy to folder/USB" export can reuse the
  // exact same server-side directory browser (GET /api/fs/dirs) the wizard/Library use.
  // pickFile is the same modal in file-picking mode, backing "Locate file…" (relink).
  return { init, open, applyThemeEarly, openTagEditor, toggleMini, checkFirstRun,
    pickFolder, pickFile };
})();
