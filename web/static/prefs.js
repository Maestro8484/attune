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

  /* ---------------------------------------------------------------- theme */
  function applyTheme(id) {
    document.documentElement.dataset.theme = id;
    store.set('theme', id);
  }
  function applyThemeEarly() {
    // The house default is now 'bee' (the MusicBee-style flat skin); 'attune' remains a
    // first-class choice, one click away. The GUARD is unchanged: a stored theme only
    // wins once the user has EXPLICITLY picked one (themeChosen) — earlier builds
    // persisted 'obsidian' on every boot, which would otherwise pin old installs to a
    // look nobody chose. An existing themeChosen install therefore keeps its theme.
    applyTheme(store.get('themeChosen', false) ? store.get('theme', 'bee') : 'bee');
  }
  function paintThemeGrid() {
    const cur = store.get('theme', 'bee');
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
      $('prefWatch').checked = !!serverSettings.watch_folders;
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
      scan_on_launch: $('prefScanLaunch').checked,
      watch_folders: $('prefWatch').checked,
      library_folders: collectFolders(),
      theme: store.get('theme', 'bee'),
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
      else if (st.new_tracks) {
        toast(`Scan done — ${st.new_tracks.toLocaleString()} new track${st.new_tracks === 1 ? '' : 's'} found.`);
        // studio.js owns the reload banner/button (it needs S/loadLibrary) -- this
        // module only reports the count, same cross-file global-function pattern
        // studio.js's own toast() already uses from here.
        if (typeof offerReload === 'function') offerReload(st.new_tracks);
      }
      else toast('Scan done — library is up to date.');
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

    // A configured SCAN FOLDER is not the same thing as HAVING A LIBRARY.
    // library_folders only tells the scanner where to look for NEW music; it is routinely
    // empty on a perfectly good install (e.g. cleared after a scan test). Gating the
    // wizard on it alone put a full-screen modal over a working 21,236-track library and
    // made the whole app unclickable. Only offer the wizard when we KNOW there is nothing
    // to play. Unknown (stats missing) is NOT a reason to block the UI.
    if (!S.stats || (S.stats.analyzed || 0) > 0) return;
    if (store.get('wizardSkipped', false)) return;   // don't re-nag every launch

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
