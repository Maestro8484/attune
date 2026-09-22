/* Attune hover help: every control, knob, setting and readout says what it does when the
   pointer rests on it. Asked for on 2026-09-22 in his words: "ALL buttons or knobs or
   even settings - any item that displays 'something' needs to have a hover popup with
   info of what that item does".

   The standard answer is a tooltip library such as Tippy.js. It is not used because the
   app ships offline with no bundler, and what is needed here is forty lines: one popup
   that appears quickly, reads the text from the element, and follows the theme. The
   browser's own title tooltip is slow (about a second) and draws in the operating
   system's colours, which is why it is replaced rather than relied on.

   Where the text comes from, first match wins, walking up from the pointer:
     1. the element's own title (moved to data-tip on first hover, so the browser's
        tooltip does not show as well),
     2. the TIPS table below, by CSS selector, for things with no title in the page,
     3. a Preferences row or a mix slider: its own grey help line,
     4. a song row, a column header, a genre/artist/album entry: built from the data.
   Nothing here changes what anything does. */
'use strict';

(() => {
  const T = [
    // top bar
    ['.brand', 'Attune: seed one song, get a playlist that sounds like it.'],
    ['#q', 'Search the whole library by title, artist, album or genre. Press / to jump here.'],
    // Mix options panel
    ['#optionsPanel .poptitle', 'How a new mix is picked. These settings apply to the next Create Mix.'],
    ['#optionsPanel .engineRow, #engineName', 'The mixing engine in use. Change it in Preferences, Mixing.'],
    ['#clap, #v2Controls .slider:nth-of-type(1) .k', 'CLAP: how much the sound itself counts, as heard by the listening model. The main dial.'],
    ['#lib, #v2Controls .slider:nth-of-type(2) .k', 'Timbre: how much tone colour counts (bright, dark, warm, harsh).'],
    ['#genre, #v2Controls .slider:nth-of-type(3) .k', 'Genre: how much the genre tags count. Higher keeps the mix inside the seed song\'s genres.'],
    ['#bpm, #v2Controls .slider:nth-of-type(4) .k', 'Tempo: how much matching beats per minute counts.'],
    ['#era, #v2Controls .slider:nth-of-type(5) .k', 'Era: how much the release year counts. Higher keeps the mix near the seed\'s decade.'],
    ['#v2Controls .presets > .k', 'Ready-made dial settings. Click one to set all five dials at once.'],
    ['[data-preset="v2"]', 'Set the five dials to the default: the combination that won the listening test.'],
    ['[data-preset="nobpm"]', 'The default dials with Tempo at 0, so tempo is ignored.'],
    ['#v2Controls .expsep', 'Radio: a mix that never ends. Turn it on here or with the Radio button in the player.'],
    ['#radioOn', 'Radio on: when the queue runs out, keep adding songs that follow on from what is playing.'],
    ['#radioVariety', 'How far Radio wanders from what is playing. 0 takes the closest match every time.'],
    ['#radioArc', 'The energy shape Radio follows: hold steady, build up, wind down, or rise and fall in waves.'],
    ['#dedupOn', 'Leave out songs that are the same song twice, for example a single and its album version.'],
    ['#dedupField', 'What counts as the same song: same artist and title, same title, or same file name.'],
    ['#style, #mipControls .slider:nth-of-type(1) .k', 'Similarity (MusicIP engine): 0 follows the sound across genres; higher follows the artist\'s style.'],
    ['#variety, #mipControls .slider:nth-of-type(2) .k', 'Variety (MusicIP engine): higher reaches deeper for more artists and genres.'],
    ['#recipeControls .k', 'Recipes: named dial settings you can save and choose again from the list in the top bar.'],
    ['#btnRecipeSave', 'Save the dials as they are now under a name of your choosing.'],
    ['#btnRecipeUpdate', 'Overwrite the chosen recipe with the dials as they are now.'],
    ['#btnRecipeRename', 'Give the chosen recipe a new name.'],
    ['#btnRecipeSetDefault', 'Make the chosen recipe the one Genius and every new mix start from.'],
    ['#btnRecipeDelete', 'Delete the chosen recipe. The dials stay as they are.'],
    // Export panel
    ['#exportPanel .poptitle, #exportCount', 'Everything in this panel acts on the list on screen, or on the selected songs if you have selected some.'],
    ['#plName, #exportPanel .row:nth-of-type(1) .k', 'The name of the playlist file, or of the folder on the USB stick. Leave blank for the name pattern in Preferences.'],
    ['#flavor, #exportPanel .row:nth-of-type(2) .k', 'How song locations are written in the file: for this PC, for other players on your network, or for Plex.'],
    ['#btnSaveDir', 'Write the playlist file into your playlist folder. It then shows under Playlists on the left.'],
    ['#btnDownload', 'Save the playlist as an .m3u8 file into your Downloads folder, to open in another player.'],
    ['#btnPlex', 'Make this list a playlist on your Plex server. Set Plex up in Preferences first.'],
    ['#exportPanel .expsep', el => /mirror/i.test(el.textContent)
      ? 'Keep a folder of music files matched onto one Plex playlist, for example a car USB stick.'
      : 'Copy the actual music files, not just a list of them, so they play anywhere.'],
    ['#copyDest', 'The folder or USB drive the songs are copied to. Use Browse to pick it.'],
    ['#copyBrowse', 'Pick the folder or USB drive to copy to.'],
    ['#copyLayout', 'Flat: numbered files that play in this order in a car stereo. Folders: Artist and Album folders.'],
    ['#btnCopy', 'Copy the songs now.'],
    ['#btnCopyCancel', 'Stop the copy. Songs already copied stay.'],
    ['#psFolder', 'A folder of music files to keep matched onto one Plex playlist.'],
    ['#psBrowse', 'Pick the folder to mirror.'],
    ['#psTitle', 'The Plex playlist name. {date}, {ymd}, {month} and {year} are filled in for you.'],
    ['#psOrder', 'The order the songs go into the Plex playlist.'],
    ['#psPrune', 'On update: make Plex match the folder exactly, or only ever add.'],
    ['#btnPsCheck', 'Read the folder and show what would change in Plex. Changes nothing.'],
    ['#btnPsApply', 'Write the changes the check just showed into Plex.'],
    ['#btnPsCancel', 'Stop the check or the update.'],
    ['#btnPsRescan', 'Ask Plex to look for new files, after adding music to your library.'],
    ['#btnPsOpen', 'Open this playlist in Plex.'],
    // side list
    ['#left .paneHead', 'Where you are. Click a place to show it in the list on the right.'],
    ['[data-view="library"]', 'Every song in your library.'],
    ['[data-view="nowplaying"]', 'The queue: what has played, what is playing, and what is next. Drag to re-order.'],
    ['[data-view="mix"]', 'The last mix you made. Drag to re-order, then save it.'],
    ['[data-smart="loved"]', 'Songs you have marked with the heart.'],
    ['[data-smart="toprated"]', 'Songs you have rated highest.'],
    ['[data-smart="recent"]', 'Songs added to the library most recently.'],
    ['[data-smart="mostplayed"]', 'Songs you have played the most.'],
    ['[data-smart="neverplayed"]', 'Songs you have never played.'],
    ['[data-smart="missing"]', 'Songs whose file cannot be found on disk. Check files now looks for them.'],
    ['#foldersHead', 'Your music folders, as a tree. Click to show or hide it.'],
    ['#treeMid .grouphead', 'Auto-Playlists: playlists made from rules (rating, genre, year...) that keep themselves up to date. + makes a new one.'],
    ['#treeTail .grouphead', 'Playlist files in your playlist folder. Subfolders are folded; click one to open it.'],
    ['#facets .paneHead', 'Click an entry to show only its songs. Ctrl-click to pick more than one. Click it again to clear.'],
    // the bar above the list
    ['#viewLabel', 'What the list is showing.'],
    ['#viewSub', 'How many songs, and how long they run.'],
    ['#mixRecipeTag', 'The recipe this mix was made with.'],
    ['#btnVerifyCancel', 'Stop looking for missing files.'],
    ['#saveNewName', 'The name for the new playlist. Enter saves, Escape cancels.'],
    ['#saveNewGo', 'Save the new playlist and open it.'],
    ['#saveNewCancel', 'Close without saving.'],
    // player bar
    ['.vol, #tVol', 'Volume.'],
    ['#eqPanel .poptitle, .eqHead', 'Equalizer: raise or lower bass, middle and treble.'],
    ['#eqOn', 'Turn the equalizer on or off.'],
    ['#eqFlat', 'Put every band back to 0.'],
    ['#eqPreset', 'Ready-made equalizer settings.'],
    ['#sSel', 'How many songs are selected, and how long they run.'],
    ['#scanShow', 'Show the scan\'s progress in Preferences, Library.'],
    ['#reloadNow', 'Load the newly analyzed songs so they can be mixed.'],
    // Preferences
    ['.modal .x', 'Close.'],
    ['#prefNav [data-tab="library"]', 'Your music folders, folders to skip, and scanning.'],
    ['#prefNav [data-tab="playlists"]', 'Where playlists are saved, how they are named, and how song locations are written.'],
    ['#prefNav [data-tab="plex"]', 'Connect to your Plex server.'],
    ['#prefNav [data-tab="mixing"]', 'The mixing engine and the default recipe.'],
    ['#prefNav [data-tab="playback"]', 'Crossfade, volume levelling and play counting.'],
    ['#prefNav [data-tab="analysis"]', 'The tools that listen to your music.'],
    ['#prefNav [data-tab="appearance"]', 'Colour theme and display colour.'],
    ['#prefNav [data-tab="advanced"]', 'Database, logs and Diagnostics.'],
    ['#libBrowse, #excludeBrowse, #prefPlaylistDirBrowse, #syncFolderBrowse', 'Pick a folder.'],
    ['#addFolder, #addExclude', 'Add another folder.'],
    ['#prefPlexTokenShow', 'Show or hide the key as you type it.'],
    ['#prefPlexForget', 'Remove the saved Plex address and key from this computer.'],
    ['#prefPlexKeyHelp', 'Open Plex\'s own page on finding the key.'],
    ['#prefPlexTest', 'Check that Attune can reach your Plex server with this address and key.'],
    ['#prefsSave', 'Save these settings.'],
    ['.modalFoot .chip[data-close]', 'Close without saving.'],
    ['#prefScanLaunch', 'Look through your music folders for new or changed songs every time Attune starts.'],
    ['#prefLogsOpen', 'Open the folder that holds Attune\'s log files, for when something goes wrong.'],
    ['#prefSyncOrder', 'The order the songs go into the mirrored Plex playlist.'],
    ['[data-page="appearance"] .prefrow:first-child > .k', 'Colour theme for the whole window. Saved with the Save button.'],
    ['.themecard', 'Colour theme for the whole window. Saved with the Save button.'],
    ['.lcdcard', 'Colour of the time and song readout and the spectrum. Changes at once.'],
    // the A-Z letters beside the Library list
    ['#azBar button', b => `Jump to the first song under "${b.textContent}" in the column the list is sorted by.`],
    ['.eqBand input', b => {
      const f = b.parentElement.querySelector('.f');
      const v = f ? f.textContent.trim() : '';
      const hz = !v ? 'this band' : /k$/i.test(v) ? `${v.slice(0, -1)} kHz` : /hz$/i.test(v) ? v : `${v} Hz`;
      return `Raise or lower the sound around ${hz}. Drag up to boost, down to cut.`;
    }],
  ];

  // A row's own help line, for Preferences and the mix sliders
  function fromHint(el) {
    const row = el.closest('.prefrow, .slider');
    if (!row) return '';
    const hints = [...row.querySelectorAll('.hint')]
      .filter(h => !h.hidden && !h.classList.contains('err') && h.textContent.trim())
      .map(h => h.textContent.trim().replace(/\s+/g, ' '));
    return hints.join(' ');
  }

  function fromData(el) {
    const th = el.closest('thead th');
    if (th) {
      if (th.dataset.sort === 'pos') return 'The song\'s place in this list: the order it plays and saves in. Drag rows to change it.';
      return `Click to sort by ${th.textContent.trim().toLowerCase()}; click again to reverse. Right-click to choose which columns show.`;
    }
    const st = el.closest('.stars');
    if (st) return 'Click a star to rate this song, 1 to 5. Click the same star again to clear.';
    if (el.closest('#npHeart, .heart')) return 'Love this song. Loved songs collect under Smart Views, Loved.';
    const tune = el.closest('.tune .tb');
    if (tune) return tune.classList.contains('more')
      ? 'More like this: steer the mix toward this song.' : 'Less like this: steer the mix away from this song.';
    const tr = el.closest('#tbody tr');
    if (tr) {
      // S is a top-level const in studio.js: shared by name, not a property of window
      const rows = (typeof S !== 'undefined' && S.rows) || [];
      const r = rows.find(x => String(x.i) === tr.dataset.i);
      const who = r ? `${r.title} by ${r.artist}. ` : '';
      const drag = tr.getAttribute('draggable') === 'true' ? ' Drag to move it.' : '';
      return `${who}Double-click to play. Right-click for Create Mix, rating, tags and more.${drag}`;
    }
    const fl = el.closest('.flist li');
    if (fl) return `Show only songs under "${fl.dataset.v}". Ctrl-click to add another. Click again to clear.`;
    const fr = el.closest('#folderTree li');
    if (fr) return 'Show the songs in this folder. The small arrow opens the folders inside it.';
    const sl = el.closest('#slList li');
    if (sl) return 'Open this auto-playlist. Its pencil edits the rules.';
    const ac = el.closest('.acard');
    if (ac) return 'Click to list this album\'s songs. The round button plays the whole album.';
    return '';
  }

  function tipFor(start) {
    for (let el = start; el && el !== document.body && el.nodeType === 1; el = el.parentElement) {
      const t = el.getAttribute('title');
      if (t) { el.dataset.tip = t; el.removeAttribute('title'); }
      if (el.dataset && el.dataset.tip) return { el, text: el.dataset.tip };
      for (const [sel, text] of T) {
        if (!el.matches(sel)) continue;
        const s = typeof text === 'function' ? text(el) : text;
        if (s) return { el, text: s };
      }
      const h = fromHint(el); if (h && el.matches('.k, input, select, button, label, textarea')) return { el, text: h };
      // A label, or the name at the left of a settings row, says what its control says.
      if (el.matches('label.chk, .row > .k, .prefrow > .k')) {
        const box = el.matches('label') ? el : el.parentElement;
        const c = box.querySelector('input:not([type=hidden]), select, textarea, button');
        const ct = c && c !== el ? tipFor(c) : null;
        if (ct && ct.text) return { el, text: ct.text };
      }
      const d = fromData(el); if (d) return { el, text: d };
    }
    return null;
  }

  const pop = document.createElement('div');
  pop.id = 'tip'; pop.setAttribute('role', 'tooltip'); pop.hidden = true;
  document.body.appendChild(pop);
  let timer = 0, cur = null;

  function hide() { clearTimeout(timer); pop.hidden = true; cur = null; }
  function place(el) {
    const r = el.getBoundingClientRect();
    pop.style.left = '0px'; pop.style.top = '0px'; pop.hidden = false;
    const w = pop.offsetWidth, h = pop.offsetHeight;
    let x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8);
    let y = r.bottom + 8;
    if (y + h > innerHeight - 8) y = Math.max(8, r.top - h - 8);
    pop.style.left = x + 'px'; pop.style.top = y + 'px';
  }

  document.addEventListener('mouseover', e => {
    const hit = tipFor(e.target);
    if (!hit || !hit.text) { if (cur) hide(); return; }
    if (cur === hit.el) return;
    hide(); cur = hit.el;
    timer = setTimeout(() => { pop.textContent = hit.text; place(hit.el); }, 350);
  });
  document.addEventListener('mouseout', e => {
    if (cur && !cur.contains(e.relatedTarget)) hide();
  });
  for (const ev of ['mousedown', 'wheel', 'keydown']) document.addEventListener(ev, hide, true);
  window.addEventListener('blur', hide);

  // for checking coverage from the console: which visible things would show no help
  window.AttuneTips = { tipFor, uncovered(root = document) {
    const sel = 'button,select,input,textarea,label.chk,#tree li,.paneHead,.grouphead,.poptitle,'
      + '.expsep,.row>.k,.prefrow>.k,#status>span:not(.spacer):not(.sep),.lcd,canvas,.stars,.heart,.pill,.vol,'
      + '#viewLabel,#viewSub,.brand,thead th,#tbody tr,.flist li,#plList li,.acard';
    return [...root.querySelectorAll(sel)].filter(e => e.getClientRects().length && e.type !== 'hidden'
      && !(tipFor(e) || {}).text);
  } };
})();
