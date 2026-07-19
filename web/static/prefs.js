/* Attune preferences + appearance + tag editor + scan UI + mini mode + splitters.
   Server settings live in settings.json (/api/settings); presentation state
   (theme, splitter sizes, mini) also mirrors to localStorage for instant boot. */
'use strict';

const Prefs = (() => {
  const THEMES = [
    { id: 'attune',   name: 'Attune',   sw: ['#17191c', '#22252a', '#f0a92e', '#00e800'] },
    { id: 'obsidian', name: 'Obsidian', sw: ['#0e1014', '#151922', '#6d8cff', '#8b5cf6'] },
    { id: 'aurora',   name: 'Aurora',   sw: ['#060a08', '#0b120e', '#00e07f', '#37cdbe'] },
    { id: 'crimson',  name: 'Crimson',  sw: ['#120d0d', '#1a1212', '#ff5d47', '#e0a030'] },
    { id: 'daylight', name: 'Daylight', sw: ['#eef0f4', '#ffffff', '#4463d6', '#7c4dd6'] },
  ];
  let serverSettings = null;
  let scanTimer = 0;
  let tagI = null;

  /* ---------------------------------------------------------------- theme */
  function applyTheme(id) {
    document.documentElement.dataset.theme = id;
    store.set('theme', id);
  }
  function applyThemeEarly() {
    // The house identity is the default. A stored theme only wins once the user has
    // EXPLICITLY picked one (themeChosen) — earlier builds persisted 'obsidian' on
    // every boot, which would otherwise pin old installs to the pre-identity look.
    applyTheme(store.get('themeChosen', false) ? store.get('theme', 'attune') : 'attune');
  }
  function paintThemeGrid() {
    const cur = store.get('theme', 'attune');
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
    paintThemeGrid();
    jget('/api/settings').then(j => {
      serverSettings = j.settings;
      $('prefsPath').textContent = j.path;
      $('prefDb').value = serverSettings.db_path || '';
      $('prefPlaylistDir').value = serverSettings.playlist_dir || '';
      $('prefEngine').value = serverSettings.engine || 'auto';
      $('prefMipUrl').value = serverSettings.musicip_url || '';
      $('prefMlPython').value = serverSettings.ml_venv_python || '';
      $('prefScanLaunch').checked = !!serverSettings.scan_on_launch;
      paintFolders(serverSettings.library_folders || []);
    }).catch(e => { $('prefsMsg').className = 'msg err'; $('prefsMsg').textContent = e.message; });
    // playback tab mirrors the player's persisted knobs
    $('prefXfade').value = store.get('xfade', 0);
    $('prefXfadeV').textContent = store.get('xfade', 0) + 's';
    $('prefRg').checked = store.get('rg', false);
  }
  function close() { $('prefsWrap').hidden = true; }

  function paintFolders(folders, containerId = 'libFolders') {
    $(containerId).innerHTML = folders.map((f, k) => `
      <div class="fr"><input type="text" value="${esc(f)}" data-k="${k}">
        <button data-del="${k}" title="Remove">✕</button></div>`).join('')
      || '<span class="hint">No folders yet — add your music folder(s).</span>';
  }
  function collectFolders(containerId = 'libFolders') {
    return [...$(containerId).querySelectorAll('input')]
      .map(x => x.value.trim()).filter(Boolean);
  }

  async function save() {
    const patch = {
      db_path: $('prefDb').value.trim(),
      playlist_dir: $('prefPlaylistDir').value.trim(),
      engine: $('prefEngine').value,
      musicip_url: $('prefMipUrl').value.trim() || 'http://localhost:10002',
      ml_venv_python: $('prefMlPython').value.trim(),
      scan_on_launch: $('prefScanLaunch').checked,
      library_folders: collectFolders(),
      theme: store.get('theme', 'attune'),
    };
    try {
      const j = await jpost('/api/settings', patch);
      serverSettings = j.settings;
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
    if (!running && scanTimer && st.finished) {
      stopScanPoll();
      if (st.error) toast('Scan: ' + st.error, true);
      else if (st.cancelled) toast('Scan cancelled');
      else toast(st.new_tracks
        ? `Scan done — ${st.new_tracks.toLocaleString()} new track${st.new_tracks === 1 ? '' : 's'}. Restart Attune to load them.`
        : 'Scan done — library is up to date.');
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

  /* ---------------------------------------------------------------- first-run wizard */
  async function checkFirstRun() {
    let j;
    try { j = await jget('/api/settings'); } catch { return; }  // unreachable — don't nag on a fluke
    serverSettings = j.settings;
    if ((serverSettings.library_folders || []).length) return;  // already configured
    paintFolders([''], 'wizFolders');
    $('wizMsg').textContent = '';
    $('wizWrap').hidden = false;
  }
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
      startScanPoll();
      $('wizWrap').hidden = true;
    } catch (e) {
      $('wizMsg').className = 'msg err'; $('wizMsg').textContent = e.message;
    }
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

  /* ---------------------------------------------------------------- splitters */
  function bindSplitters() {
    const grid = $('grid');
    const leftW = store.get('leftw', 270);
    const facetH = store.get('faceth', 210);
    grid.style.setProperty('--leftw', leftW + 'px');
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
  }

  /* ---------------------------------------------------------------- wiring */
  function bind() {
    $('btnPrefs').onclick = open;
    $('prefsSave').onclick = save;
    $('btnMini').onclick = () => toggleMini();
    $('miniExpand').onclick = () => toggleMini(false);
    $('tagSave').onclick = saveTags;
    $('btnRescan').onclick = rescan;
    $('scanShow').onclick = () => { open(); };
    $('addFolder').onclick = () => {
      const cur = collectFolders(); cur.push('');
      paintFolders(cur);
      const inputs = $('libFolders').querySelectorAll('input');
      inputs[inputs.length - 1].focus();
    };
    $('libFolders').addEventListener('click', e => {
      const del = e.target.closest('button[data-del]'); if (!del) return;
      const cur = collectFolders(); cur.splice(+del.dataset.del, 1);
      paintFolders(cur);
    });
    // first-run wizard folder controls (its own list; Skip/✕ close via generic [data-close])
    $('wizAddFolder').onclick = () => {
      const cur = collectFolders('wizFolders'); cur.push('');
      paintFolders(cur, 'wizFolders');
      const inputs = $('wizFolders').querySelectorAll('input');
      inputs[inputs.length - 1].focus();
    };
    $('wizFolders').addEventListener('click', e => {
      const del = e.target.closest('button[data-del]'); if (!del) return;
      const cur = collectFolders('wizFolders'); cur.splice(+del.dataset.del, 1);
      paintFolders(cur, 'wizFolders');
    });
    $('wizStart').onclick = wizStart;
    // tabs
    $('prefTabs').addEventListener('click', e => {
      const b = e.target.closest('button[data-tab]'); if (!b) return;
      $('prefTabs').querySelectorAll('button').forEach(x =>
        x.classList.toggle('on', x === b));
      document.querySelectorAll('.tabpage').forEach(p =>
        p.hidden = p.dataset.page !== b.dataset.tab);
    });
    // theme cards
    $('themeGrid').addEventListener('click', e => {
      const c = e.target.closest('.themecard'); if (!c) return;
      applyTheme(c.dataset.theme);
      store.set('themeChosen', true);
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
      b.onclick = () => $(b.dataset.close).hidden = true);
    document.querySelectorAll('.modalwrap').forEach(w =>
      w.addEventListener('mousedown', e => { if (e.target === w) w.hidden = true; }));
  }

  function init() {
    bind();
    bindSplitters();
    if (store.get('mini', false)) toggleMini(true);
    // if a scan is already running (started before this page load), surface it
    jget('/api/scan/status').then(st => { if (st.running) startScanPoll(); }).catch(() => {});
  }

  return { init, open, applyThemeEarly, openTagEditor, toggleMini, checkFirstRun };
})();
