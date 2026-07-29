# Changelog

All notable changes to Attune are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] — unreleased (initial public cut)

### Added
- **Export roots move to settings.json + Preferences, unconfigured flavors degrade
  gracefully** (`src/config.py`, `src/export.py`, `web/app.py`, `web/static/prefs.js`,
  `web/static/studio.html`), per `PROPOSAL_EXPORT_ROOTS_2026-07-28.md`'s ruling and its
  same-day amendment. Three new settings keys (`local_library_root`, `unc_library_root`,
  `plex_library_root`) are the new home for the paths that used to live only in `.env`;
  `.env` stays a dev override plus Plex's real connection secrets. `local_library_root`
  additionally derives from the DB's own track-path common prefix when nothing configures
  it (`export.derive_local_root`); UNC and Plex are never guessed. A one-time migration
  copies existing `.env` values into settings.json on first run after this build. The
  actual bug fix: `GET /api/export/m3u` for an unconfigured UNC/Plex root used to 400 with
  an empty file — it now falls back to local paths and reports the fallback via
  `X-Attune-Export-*` response headers (the route's body is the playlist file itself, not
  JSON). Playlist export's default flavor stays `unc`, unchanged, per the amendment. The
  path-style dropdown (`#flavor` in Preferences -> Export) now remembers your last choice
  across sessions via `localStorage`. **Observed** over HTTP against a scratch copy of
  `mixer.db`: a fresh, fully-unconfigured install returns HTTP 200 (not 400) for
  `flavor=unc` and `flavor=plex`, with `X-Attune-Export-Fallback: 1` and a body
  byte-identical to the same request with `flavor=local`; with roots configured (via a
  scratch `.env`), `flavor=unc`/`plex` return `X-Attune-Export-Fallback: 0` and correctly
  remapped paths; the migration writes settings.json once and is idempotent on a second
  run; `POST /api/settings` accepts and round-trips the three new keys with
  `needs_restart` correctly listing all three. Sticky-flavor restore-on-load and
  persist-on-change were each confirmed via a real browser navigation/reload against the
  live page, not a simulated event.
- **"Send to folder", from the right-click menu, in two scopes** (`web/static/studio.js`
  context menu, `web/static/studio.html`): **Send selection** copies exactly the rows you
  have selected; **Send whole mix** copies the mix. Both route through the **existing**
  S8 export-copy job, which already handles destination containment, FAT-safe names,
  collisions and the relative `.m3u8`. `web/exportjob.py` is byte-identical to before
  this change: the two scopes are just which `ids` array the client sends, so there is
  no second copy pipeline. **Observed** over HTTP against a scratch DB: selection scope
  into an empty folder produced exactly the three selected tracks plus the playlist and
  nothing else; whole-mix produced 20 tracks plus the playlist. Note both scopes write
  an `.m3u8`, because the underlying job always does.
- **Folder picker behaves like Explorer** (`web/static/prefs.js`, `web/static/studio.html`,
  `web/static/studio.css`, `web/app.py`), in every context the one shared picker opens
  (first-run wizard, Preferences -> Library, export destinations). The path is now a real
  `<input>`, so it is selectable and copyable, and typing or pasting a path navigates
  there (Enter commits, Escape reverts). New **"+ New folder"** with inline rename, backed
  by two new loopback-guarded endpoints `POST /api/fs/mkdir` and `POST /api/fs/rename`.
  Both reduce the supplied name to a single path component and then prove containment on
  the resolved real path, the same pattern `exportjob.py` already used. **Observed:**
  a name of `../../escaped` and a name of `C:/Windows/evil` each landed inside the parent
  folder as a plain leaf, `C:\Windows\evil` was never created, and renaming a drive root
  is refused with a 400.
- **The desktop window reopens where you left it** (`desktop/app_desktop.py`,
  `src/config.py` new `window_geometry` key). Position and size persist to the existing
  settings.json (the one store the desktop app, server and analyzer already share) via
  pywebview's own `resized` / `moved` events, throttled, with a synchronous save on
  `closing`. Geometry that is off every attached monitor, malformed, or too small falls
  back to the previous hardcoded default instead of opening invisibly. **Observed** by a
  real launch, move, close, relaunch cycle: the window reopened at exactly the moved
  rect, and a hand-written off-screen `(-32000,-32000)` reopened at the default.
- **Seek position survives a restart**, alongside the queue that already did
  (`web/static/player.js`). Throttled so a playing track is not writing constantly, and
  the restore only applies when the saved track still matches the one being restored.
  Still paused on boot: Attune never starts making noise by itself.
- **A real installer icon** (`desktop/installer/attune.ico`, wired at
  `desktop/installer/attune.iss`). Rasterized from the app's own inline favicon artwork,
  multi-resolution 256/64/48/32/16 at 32bpp. **This is a placeholder; final art is the
  operator's call.** Verified by parsing the committed `.ico` back with an independent
  reader: 5 entries, exactly those sizes.

### Fixed
- **Saving a new auto-playlist under an existing name destroyed the old one silently**
  (`web/smartlists.py`). `sl_save` used `INSERT OR REPLACE` on a UNIQUE name, so a
  name collision quietly overwrote curated rules with no undo. It now mirrors
  `recipes.py`: a plain `INSERT`, `sqlite3.IntegrityError` caught, and a clear 400.
  Editing an existing list by id is unaffected. **Observed:** second save of the same
  name returned 400 and the original rules were still intact afterwards.
- **Now Playing went stale after queue edits** (`web/static/studio.js`). Adding to the
  queue repainted only the right-rail Up Next, never the open Now Playing table, so the
  view you were looking at silently disagreed with the queue. All three call sites
  (context menu Play Next, context menu Add to Queue, and the `q` shortcut) now repaint
  it. **Observed with real clicks:** with Now Playing open, Add to Queue took the table
  from one row to two in place, with no view switch.
- **A cancelled scan reported itself as failed** (`web/scanjob.py`). Cancelling
  terminates the child process, which returns a nonzero code, and all three stage guards
  tested that code alone, so the app told users who had just pressed Cancel that the scan
  had failed. Each guard now checks `self.cancelled` first. **Observed** by starting a
  real scan against a throwaway folder, cancelling mid-run, and reading status: cancelled
  true, error empty.
- **Smart views re-sorted themselves when you scrolled past row 200**
  (`web/static/studio.js`). `loadLibrary` respected a smart view's natural sort until the
  user clicked a header, but `maybeLoadMore` set the sort unconditionally, so paging
  crossed into a different ordering mid-scroll. The guard is now factored into one helper
  used by both. **Observed** against Recently Added: the two pages are contiguous in one
  ordering across the boundary, where forcing the old behaviour jumped 28 hours and
  reshuffled alphabetically.
- **A duplicate track in the queue dragged playback onto the wrong copy**
  (`web/static/player.js`). Removing or reordering re-derived the play position with
  `indexOf`, which always finds the *first* occurrence, so editing a queue containing
  duplicates could jump playback to a different slot. Position is now adjusted
  positionally on both the remove and the reorder path. Per-slot queue identity remains
  deliberately deferred. **Observed** across nine cases including the duplicate-before-
  position repro, run through the real `player.js`.
- **The Preferences engine dropdown was missing the learned engine**
  (`web/static/studio.html`). `config.py`, `app.py` and `libreload.py` all wire four
  engines while the dropdown offered three, so selecting the learned metric needed
  hand-edited JSON. It is now selectable. It is still **not** the default: per the
  project's first law that remains gated on a blind ear test.

### Added (earlier in this release)
- **"Mix from this"** — a button on the right rail's Track Info tab that starts a
  new mix seeded on the track you are hearing (`web/static/studio.{html,css,js}`).
  Same `doMix()` path as Create Mix with a different seed integer, so exports and
  the engine are untouched by construction; with nothing playing it toasts instead.
  Verified by real clicks + elementFromPoint hit-testing on a scratch-DB server:
  the rendered mix matched the pinned `/api/mix` capture for the same seed
  track-for-track; regression gate 16/16 before and after.
- **MusicBee-style layout, a global player bar, and a new default skin**
  (`web/static/{studio.html,studio.css,studio.js,player.js,prefs.js}`; no server code, no
  new files, no build changes). Rebuilds the Studio frame around MusicBee's documented
  anatomy (`_scratch-uiresearch/MUSICBEE_SPEC.md`) while preserving every existing element
  ID, so every `getElementById` contract in the JS keeps resolving.
  - **`#grid` is now five columns**: `--leftw` | splitter | main panel | splitter |
    `--rightw`. The left rail is the **Navigator only**. The art, LCD, visualizer and
    transport that used to be stacked underneath it have moved out.
  - **New `#rightRail`**, MusicBee's signature right sidebar: **Up Next** and **Track
    Info** as two tabs. Resizable via a new `#splitRight` (`--rightw`, localStorage
    persisted, same drag helper as `#splitLeft`) and collapsible from a new toolbar
    button. On by default at >=1100px, collapsed below, and the choice persists.
    Track Info carries `#art` at ~298px (up from 268px) plus title/artist/album/genre/year.
  - **New `#bottomBar`** (72px), the global player bar in MusicBee's documented
    left-to-right order: transport, then a centre display panel, then rating, spectrum,
    volume and EQ. The display panel is the Winamp throwback zone: `#lcd` moved intact
    (marquee, kbps/kHz, SHUF/REP/DJ/RADIO/EQ flags) with `#wave` directly beneath it.
  - **Up Next is a view of the queue, not a copy of it.** `Player.q` remains the single
    owner. Every queue mutation already funnels through `paintTransport()`, which now
    fires a `queueChanged()` hook that repaints the rail from `Player.q`/`Player.pos`;
    `moveInQueue()` and `shuffleQueue()` gained the same hook. `showNowPlaying()` (the
    main-view queue) is untouched and still works.
  - **Radio has a transport button** (`#tRadio`) on the same `toggleRadio()` path the `J`
    key uses. It was previously 2 clicks deep and the 8th interactive control down inside
    the Mix Options popover.
  - **The LCD, waveform and LED spectrum are now theme-shared**, driven by new `--lcd-bg`
    / `--lcd` / `--lcd-dim` / `--led-*` tokens that default on `:root` to the historical
    green-on-black. They used to live inside the attune-only block and vanished under the
    other themes.
  - **New `bee` theme, now the default**: three desaturated dark background tiers, two
    text tiers, one disciplined blue accent (`#3d84c6`) for selection/active/progress, and
    warm gold (`#d0a032`) reserved for exactly one control, the Genius button. Segoe UI at
    12px, 23px rows, flat panels, slim scrollbars. The `attune.themeChosen` guard is
    unchanged: an install that ever picked a theme keeps it, and only the un-chosen
    fallback moves from `attune` to `bee`. Attune's structural block is re-pointed at the
    regions the transport now occupies, so its bevels, ridged captions and gold survive on
    the new frame.

  **Bundled fixes** (all from `_scratch-uiaudit/GUI_AUDIT.md` §5):
  - Context menu, column chooser and "Why this pick?" no longer clamp against a hardcoded
    height budget. `#ctx` assumed 430px while the menu had grown to 683px, so right-clicking
    low in an 800px window hung it 253px off-screen with no scroll and no edge flip. A new
    `placeFloating()` measures the real box before placing it, and the CSS adds
    `max-height`/`overflow` so a floater taller than the window scrolls instead of clipping.
  - `.popover` gets `max-height` + `overflow`. Mix options is ~655px tall and simply clipped
    off the bottom of a short window with no way to reach the rest.
  - Favicon: an inline `data:` SVG `<link>`. `GET /favicon.ico` used to 404, and no new file
    ships.
  - Removed the dead `--h: 34px` token (declared, never referenced).

  **Found during verification, both by `elementFromPoint`:** `#eqPanel`'s `bottom:44px` was
  measured against the old layout and sat on top of the new player bar, covering its own EQ
  button; it derives from a `--barh` token now. And the toolbar overflowed its row by 188px
  at 1024px wide, pushing the search box and all three icon buttons off-screen (pre-existing;
  the new rail button made it 31px worse). Every toolbar control is pinned now except the
  search, which shrinks to a 96px floor, backed by a `<=1180px` media query.

  **Observed** (scratch copy of `mixer.db`, 115,511,296 bytes byte-verified, 21,236 tracks,
  port 8785; fresh browser profile, `%APPDATA%\Attune\settings.json` never touched):
  65/65 controls hit-test to themselves via `document.elementFromPoint` at 1280x800 and
  42/42 at 1024x700, with zero console output. The audit's exact context-menu repro
  (right-click at `clientY=498` in an 800px window) now places the menu at top 111 / bottom
  794, fully on screen; at 1024x560 it caps at 544px, scrolls, and both the first and last
  items hit-test. Mix options caps at 630px on a 700px window and its Radio checkbox is
  reachable. Real clicks (Browser pane) confirmed the Radio button lights the button, the
  LCD `RADIO` flag and the Options checkbox together; removing a track from the rail took
  the queue 8 to 7 and every surface followed (rail rows, tab badge, tree count,
  localStorage). Seed to mix is unchanged at 2 clicks (101 tracks, same seed line as the
  audit). Waveform seek at 75% across landed at 202s of a 268s track. All six themes swap
  live with `grid-template-columns` identical in every one. Mini mode, the EQ panel, and
  album/list click-through all verified. A stale pre-redesign `localStorage`
  (`theme=attune` + `themeChosen`, `leftw=270`, none of the new keys) boots attune on the
  new layout with sane defaults for everything new. Regression gate **16/16 byte-identical**
  against `REGRESSION_BASELINE_20260719`.

  **Deliberately out of scope**, listed here as future options: A-Z jump bar, thumbnail
  browser, library-explorer tree, lyrics panel, per-tab layout configuration, and
  drag-from-table-to-queue.
- **A real Windows installer** (`desktop/installer/attune.iss` NEW, `desktop/build_installer.py`
  NEW). Inno Setup 6, per-user (`PrivilegesRequired=lowest`, no UAC, installs under
  `{localappdata}\Programs\Attune`), packages the whole one-dir PyInstaller tree from
  `desktop/build.py` (`Attune.exe` + `_internal\` + the nested `analyzer\AttuneAnalyzer.exe`)
  under lzma2/max solid compression. Start Menu shortcut always; a desktop shortcut and
  "start Attune when Windows starts" (an HKCU `Run` value) are both opt-in, unchecked
  tasks. Uninstall removes the install dir, Start Menu group, desktop icon, and the `Run`
  key; it never touches `%APPDATA%\Attune\settings.json` (`src/config.py`) or a user's
  library DB — no `[UninstallDelete]` section reaches into `{userappdata}`, which is the
  entire preservation mechanism. Fixed `AppId` (a hardcoded GUID) so future versions
  upgrade in place rather than installing side-by-side. `build_installer.py` is a thin
  ISCC driver (locates `ISCC.exe` in the three standard install locations, takes
  `--dist`/`--out`, no new dependencies) — the actual packaging logic all lives in Inno
  Setup itself, not reinvented here. No icon file exists anywhere under `desktop/` yet, so
  `SetupIconFile` is omitted (falls back to Inno's default icon) rather than inventing one.
  **Observed:** compiled clean (169 s, `AttuneSetup-0.1.0.exe`, 496.8 MB from a 1.1 GB / 4,032-file
  source tree) against the real `dist/Attune` build. `/VERYSILENT /NOICONS` install into a
  scratch dir: exit 0, 4,034 files (source count + the two uninstaller artifacts),
  `Attune.exe` and `analyzer\AttuneAnalyzer.exe` both present. Smoke-launched the installed
  copy: window opened, `/api/boot` reported ready, `/api/lib/stats` returned 200 with real
  library stats (21,236 tracks), closed cleanly (`CloseMainWindow`), port released, no
  orphaned processes. Confirmed `%APPDATA%\Attune\settings.json` byte-identical
  (SHA-256 match) and same mtime before and after — the app's own `db_path` write-back only
  fires on a mismatch, and the installed copy resolved the same already-configured DB path,
  so nothing wrote. Silent uninstall: exit 0, install dir gone, `settings.json` hash still
  matching. Reinstall-then-uninstall repeated clean. Separately verified with
  `/TASKS="startupicon"`: the HKCU `Run` value is created pointing at the installed
  `Attune.exe` and is removed by uninstall. Port discovery required reading
  `desktop/app_desktop.py`: the dev-server doc's "likely fixed" port assumption doesn't
  hold for the desktop build — it binds `("127.0.0.1", 0)` and lets the OS assign an
  ephemeral port every launch, so the smoke test found it via
  `Get-NetTCPConnection -OwningProcess` rather than a fixed number.
- **Journey (Radio) mode: an infinite ambient-radio queue** (`src/hybrid.py`
  `HybridEngine.radio_next()` NEW, `src/engine.py` `V2Engine.radio_next()` NEW,
  `web/app.py` `GET /api/radio/next` NEW, `web/static/{studio.html,studio.js,player.js}`).
  Reimplements MusicIP Mixer's actual variety mechanism, reverse-engineered in
  `TEACHER_MECHANICS.md` B.1/B.7: i.i.d. Bernoulli thinning (`p = 1/(1+variety)`) of a
  single fixed similarity-rank scan — NOT a diversity/packing walk — plus the same
  artist-spacing drop-for-good rule `mix()` already uses. Adds an energy-arc corridor
  MusicIP never had: a second Bernoulli-style accept/reject on the stored librosa
  `rms_mean` (loudness) feature (`features.py` texture-group offset 75), biasing the walk
  toward a flat/rise/fall/wave target over a 20-track period. Off by default —
  `variety<=0` is exactly `mix()`'s own walk (`radio_next(seed, n, exclude=[], variety=0,
  arc=<any>) == mix(seed, size=n)`, verified live). Stateless server (`exclude`/`pos` are
  client-owned); the client refills the queue from `/api/radio/next` when Now Playing has
  fewer than 5 upcoming tracks, seeded from Now Playing (falling back to the last mix
  seed), excluding the last 100 played/queued pool indices. New Studio controls: a Radio
  toggle, arc picker and variety slider in the Mix Options popover, and a `RADIO` LCD flag
  alongside `SHUF`/`REP`/`DJ`/`EQ`. V2-engine only (`radio` capability, gated the same way
  as `refine`/`explain`). Regression gate 16/16 byte-identical against
  `REGRESSION_BASELINE_20260719` — the default `/api/mix` path, including the existing
  boolean `variety=1&flow=1` journey export, is untouched.
- **The library is now alive** (`web/libreload.py` NEW, `web/libverify.py` NEW,
  `web/{app.py,studio.py,userdata.py,smartlists.py,recipes.py,exportjob.py}`,
  `web/static/{studio.html,studio.css,studio.js,prefs.js}`). Three pieces. (1) *Hot pool
  reload*: `POST /api/lib/reload` rebuilds the engine from the DB in a background thread
  and installs it into the running app, so freshly scanned tracks become searchable and
  mixable WITHOUT restarting — the scan toast's "Restart Attune to load them" is now a
  "Load now" button. The expensive build runs off-lock against throwaway instances; the
  install is an in-place swap under a new `engine_lock` that every state-reading route
  shares (`@_locked`), so a request runs entirely pre- or entirely post-reload, never
  torn (`/audio` locks only its path lookup, so streaming can't stall a reload). (2)
  *Missing-file detection*: `POST /api/lib/verify` walks every track path in a background
  job into a new additive `filestate` table (outside the schema guard, usermeta pattern),
  surfacing a `missing` count in `/api/lib/stats`, a "Missing Files" smart view, a
  hide-missing filter (default off — mixes are NOT changed by a missing flag), and a
  context-menu "Locate file…" relink that rekeys `tracks/features/clap/filestate` to the
  new path. (3) *A real "Remove from library…"*: `POST /api/track/delete` transactionally
  removes the track's rows across every per-track table and never touches the audio file
  on disk; the in-RAM pool keeps the track until the next reload, and the UI says so.
  **Observed:** scan of 3 scratch copies → reload → mixable without restart
  (`old_count 21236, new_count 21239`, `/api/mix` real on all 3); 8 concurrent mixes
  during a reload all 200; regression gate 16/16 byte-identical before AND after a no-op
  reload; delete leaves 0 rows across 5 tables; the verify job found 22 genuinely missing
  files already in the production library.

### Fixed
- **Packaged-app-only boot crash, caught before shipping** (`desktop/build.py`): the
  living-library modules `web/libreload.py` and `web/libverify.py` were not in the GUI
  bundle's data list, and `app.py` loads both by file path unconditionally in
  `create_app` — the frozen `Attune.exe` would have died at startup while dev stayed
  green (the same failure class as the S10 unbundled-`embed.py` scan bug). Found by
  reading the PyInstaller command line in the build log against the merged `app.py`;
  both files are now bundled. **Observed after rebuild:** the packaged exe boots,
  `/api/lib/stats` 200, and the new `/api/radio/next` + `/api/lib/reload/status`
  endpoints answer from the shipped bits.
- **Three dead controls found by the S10 GUI audit** (`web/static/{studio.js,studio.css}`):
  (1) *More/Less Like This Artist ignored a multi-row selection* — only the right-clicked
  row's artist voted, while Remove/Block honored the selection; the menu items now act on
  every selected artist with one re-mix. (2) *The main nav tree could be crushed to a
  clipped ~14 px sliver* whenever the playlist folder is large (`.tree.pl` is flex-grow and
  `#tree` was default-shrinkable): with 232 playlist entries, "Now Playing" was invisible
  and a playlist row ate its clicks — invisible to synthetic-event tests, caught by
  elementFromPoint. `#tree` is now `flex:0 0 auto`. (3) *"Save queue as playlist" was dead
  by mouse*: the click that opened the export panel bubbled to the document-level
  outside-click handler, which closed it in the same frame (`#queueTools` buttons were not
  exempt like `#toolbar` buttons). All three re-observed fixed via hit-testing on a live
  server; regression gate 16/16 byte-identical (pool 21,236, k=25).
- **weights_lock bypass** (`web/app.py`): a mix request carrying no weight params ran
  outside the lock and could read `eng.w` mid-mutation while a slider-overridden request
  had its temporary weights applied. All mixes now serialize through the lock.
  Pre-existing at `6fd1072` (MAD MAJOR#1 from the recipes round); single-request behavior
  unchanged, gate 16/16.

### Changed
- Default `theme` settings key is now `bee`, matching the client default since the
  MusicBee-style UI; the key is currently client-inert (prefs.js keeps the theme in
  localStorage) but must stay in DEFAULTS because the client POSTs it on every
  settings save (`src/config.py`). Stale day-1 "no GUI / no export" lines removed
  from Known limitations; `find_env` docstring no longer promises a repo-root
  fallback the code never had (`src/export.py`).
- **The program you launch is 8x smaller: 938 MB → 119 MB** (`desktop/build.py`,
  `desktop/worker_entry.py` NEW, `desktop/analyzer_main.py` NEW, `desktop/app_desktop.py`,
  `web/scanjob.py`). `Attune.exe` was carrying ~800 MB it never used — the 276 MB CLAP
  encoder, ffmpeg + ffprobe (283 MB) and librosa/numba/llvmlite/scipy/sklearn — all of
  it only for analyzing NEW audio, while mixing reads vectors already in `mixer.db`.
  The analyzer is now its own program, `analyzer\AttuneAnalyzer.exe`, inside the same
  install; `scanjob.py` launches it for the frozen + no-ML-venv case and refuses
  honestly, naming the expected path, when the folder is absent. **Nothing was removed:**
  onnxruntime + `metric_head.onnx` stay in the GUI because `engine: learned` is reachable
  from settings, and the analyzer scripts stay bundled because the ML-venv path runs them
  under the user's own Python. The whole install is 1,032 MB, ~94 MB MORE than before —
  the analyzer bundle duplicates the Python runtime — so this buys a small daily program
  and a smaller AV surface, not less disk. Startup is unchanged (1.42 s to a window):
  profiling refuted the earlier hypothesis that the analyzer stack cost import time in
  the GUI process — it was never imported there. **Observed:** routing equivalent on all
  four (ML venv × frozen) cases via `tools/routing_cases.py` (1-3 byte-identical, 4
  replaced with the same tool names and argv tail); scrubbed-PATH (`System32` + `Windows`
  only) analyzer run import/analyze/embed all rc=0 with real tags and durations from the
  bundled ffprobe, features dim 79 + tempo, clap dim 512, 0 errors; the lean GUI mixing
  under the same scrubbed PATH; a full scan driven from the real GUI into the analyzer;
  regression gate 16/16 byte-identical.

### Fixed
- **A packaged scan could not finish for anyone with an ML venv configured**
  (`desktop/build.py`). `embed.py` was never included in the bundle, so the packaged app
  ran import and analyze and then died at the embed stage with rc=2 "can't open file" —
  new tracks got librosa features but no CLAP vector, and so never joined the mixable
  pool. **Observed** rc=2 before, rc=0 ("21456 already embedded, 0 to go") after. Missed
  by earlier verification because a dev run has `embed.py` on disk.
- **Scan progress never reached the UI in the packaged app**
  (`desktop/analyzer_main.py`, `desktop/worker_entry.py`). The frozen worker was the
  *windowed* GUI exe, which has no usable stdout, so every progress line the scan job
  streams was written into nothing. The analyzer is a console build (launched with
  `CREATE_NO_WINDOW`, so no window appears) and reconfigures stdout to UTF-8, so lines
  now stream and accented track names survive the trip. **Observed:** "3/3 ok=3 err=0
  0.60/s", "done: 3 embedded, 0 errors".

### Added
- **Stop button** (`web/static/{studio.html,studio.js}`, `web/static/player.js`):
  transport Stop between Play and Next (V key) — halts playback and rewinds to the track
  start; the queue and position stay, Play resumes from 0:00.
- **Startup: 30.8 s → 3.0 s to a window, 4.7 s to a fully loaded library**
  (`src/hybrid.py`, `desktop/app_desktop.py`, `web/app.py`,
  `web/static/{boot.js,studio.js}`). Four separate causes, each measured before it was
  touched, none of them the music scan (the library was already analyzed):
  1. **Key estimation was 90% of engine load** (`src/hybrid.py`). `HybridEngine.__init__`
     re-derived the musical key of every pool track on EVERY launch by calling
     `np.corrcoef` 24× per track — ~510,000 calls, **23.19 s of a 25.80 s load** — for the
     `key` weight, which ships at 0.0 (it lost the ear test). Replaced with one
     precomputed (24,12) profile matrix and a single matmul + argmax
     (`_keys_from_chroma_batch`), using `corr(roll(c,-rot), prof) == corr(c, roll(prof, rot))`
     and a column order that reproduces the loop's first-max tie-break.
     **Parity-gated: identical `(tonic, is_major)`/None on all 21,236 pool tracks**;
     22.43 s → 0.02 s; whole engine load 25.80 s → 0.51 s.
  2. **A 4.13 s probe for MusicIP that isn't running** (`desktop/app_desktop.py`,
     `web/app.py`). Measured on this machine: with MusicIP down, port 10002 *drops* the
     SYN rather than refusing it, so the connect burns its full timeout — and urllib
     against `localhost` does it twice (`::1`, then `127.0.0.1`). Every launch paid it.
     Now a 0.25 s socket pre-check gates the HTTP call, and on the desktop path the probe
     moved off the window's critical path entirely.
  3. **The window only appeared after everything had loaded** (`desktop/app_desktop.py`).
     `create_app()` ran to completion *before* `create_window()`, so a launch showed
     nothing at all until the whole library was in memory. Added `_BootGate`, a WSGI gate
     (werkzeug `DispatcherMiddleware` shape + a readiness endpoint) that serves a splash
     and `/api/boot` immediately, then delegates every request to the real app once it is
     built on a worker thread started via pywebview's own `webview.start(func)` idiom.
     Window at 0.01 s from source, 3.0 s in the frozen build.
  4. **`/static/*.js` 404'd in the packaged app** (`web/app.py`) — the reason the shipped
     .exe opened with a dead UI. `Flask(__name__)` derives its static folder from the
     import name, but `desktop/app_desktop.py` execs `app.py` via importlib without
     registering it in `sys.modules`, so Flask fell back to the **current working
     directory**. In dev `__name__ == "__main__"` made this accidentally correct; in the
     frozen build `boot.js`, `player.js`, `prefs.js` and `smartlist.js` all 404'd — and
     with `boot.js` missing, nothing initialized. `static_folder` is now anchored to the
     module file, exactly as the existing `/studio`, `/studio.js`, `/studio.css` routes
     already were. Observed 404→200 for all four in `Attune.exe`, same commit.
- **A single failed request no longer kills the rest of the UI**
  (`web/static/boot.js`, `web/static/studio.js`). `boot()` did
  `try { await initCore() } catch { return }`, so one throw — most realistically
  `/api/playlists` when the playlist folder is an unreachable network drive — skipped
  `Player.init()`, `Prefs.init()`, `checkFirstRun()` and `Smart.init()`, leaving a window
  with no transport, no preferences, no auto-playlists and no wizard. Each subsystem now
  initializes independently; `initCore` never throws, retries `/api/lib/stats` with
  backoff (`jgetReady`) in case the server is still coming up, and degrades per-section.
  **Observed:** with `/api/playlists` forced to reject, `initCore` resolves
  `{ok:false, error:'playlist folder unavailable'}`, the library still renders its 200
  rows, and the UI reports the failure honestly instead of going dark.
- Regression gate **16/16 byte-identical** against `REGRESSION_BASELINE_20260719`
  (8 plain-server + 8 journey), run after the engine change and again on the final tree:
  the speedup changes nothing about which tracks a mix contains or their order.

### Added
- **"Take It With You" — copy a mix's actual audio files to a folder / USB**
  (`web/exportjob.py` NEW, `web/app.py`, `desktop/build.py`,
  `web/static/{studio.html,studio.js,studio.css,prefs.js}`): a third export destination,
  folded into the same Export popover beside the playlist-file and Plex paths. Pick a
  folder or USB drive (reusing the wizard's server-side folder picker) and Attune copies
  the ACTUAL audio files of the current on-screen mix — in mix order, with FAT32/exFAT-safe
  names — plus a self-contained relative-path `.m3u8`, so the folder plays in order on a
  dumb car head unit or phone that can't reach the library share. Flat layout
  (`NN Artist - Title.ext`, default) or an `Artist/Album/` tree. A background job (cloned
  from the scan-job pattern) with a progress bar + cancel; a `shutil.disk_usage` preflight
  refuses with numbers if it won't fit; filename collisions get a ` (2)` suffix and never
  overwrite; an unreadable source is skipped-and-reported, never aborting the copy. Sources
  are opened READ-ONLY (`shutil.copy2`, mtime preserved) and the destination is
  realpath-contained to the picked folder; the three copy endpoints are loopback-only like
  `/api/fs/dirs`. Consumes the same `_active_mix_tracks` list every other export uses, so
  it never changes what a mix is. **Observed:** journey mix (variety+flow) of 26 → 26 files
  + ordered relative-path m3u8 (seed first); re-copy → 26 ` (2)` files, zero overwrites;
  cancel mid-job → honest 1/26 partial + partial m3u8; no-space (size-lie) → clear refusal,
  nothing written, dest dir not even created; `Artist/Album/NN` tree layout; a full copy
  driven through the real Studio UI (progress → "Copied 6 tracks"); all 16
  journey-checkpoint exports byte-identical before/after (regression gate 16/16).
  A pre-merge MAD (Codex) round hardened it: write-time realpath containment on *every*
  output path (plus `lexists` so a dangling symlink at a destination name is never copied
  through), collision-safe `.m3u8` naming (a re-copy leaves `Mix.m3u8` and `Mix (2).m3u8`,
  never a silent overwrite), surfaced playlist-write failures, filename truncation that
  can't leave a trailing dot/space, and a lock around the status snapshot — all re-verified
  live (happy/collision/cancel/tree) with the gate still 16/16.
- **Mix recipes + the Genius button** (`web/recipes.py` NEW, `web/app.py`, `src/config.py`,
  `desktop/build.py`, `web/static/{studio.html,studio.js,studio.css}`): named, saved
  bundles of the existing mix parameters (five weight sliders, MMR variety, flow,
  dedup, size) — no engine-math change, no new embedding. Additive `recipes` table
  outside db.py's schema guard (smartlists pattern) with five seeded built-ins
  (Classic Journey, Sound-Alike, Same-Era Deep Cuts, Genre Purist, Wander Far — names
  provisional until the owner's ear pass), CRUD at `/api/recipe/{list,save,delete}`,
  closed param whitelist clamped to the same ranges `/api/mix` enforces. Studio gains a
  toolbar recipe select that writes the chosen bundle into the dial UI (`mixParams()`
  untouched — recipes set dials, they never build requests), Save-as/Update/Rename/
  Delete/Set-default in the Options popover, an active-recipe badge on the mix header,
  and localStorage persistence. `default_recipe` settings key (not restart-keyed).
  **✦ Genius (Ctrl+G)**: one click → `/api/recipe/genius_seed` picks a seed by tier
  (loved-and-rested → rating≥4 → any analyzed, random within tier) → default recipe
  (fallback Classic Journey) → normal `doMix` path → playback starts via the existing
  `Player.playList`. Auto-DJ's queue refill reads the same dials, so it carries the
  active recipe's params with no extra code. Hidden under the musicip engine (recipes
  are v2-param-shaped). **Observed live:** built-ins seeded, CRUD + validation
  round-trips over HTTP, recipe apply/persist across reloads, one real click →
  audio actually playing (seed picked by tier logic), Auto-DJ refill request carrying
  recipe params, and the 16 journey-checkpoint exports byte-identical at every step
  (pre-flight, after each chunk, and after the MAD fixes).
- **Inline More/Less Like This on mix rows** (`web/static/{studio.js,studio.css}`): every
  non-seed row of a mix shows hover-revealed +/− buttons in the title cell, wired to the
  same thumbs → `/api/refine` (Rocchio) path as the right-click menu. Votes toggle (a
  second click withdraws) and More/Less are mutually exclusive per track; the context-menu
  items share the same helper now, so both entry points dedupe. V2-engine-gated like the
  menu; voted tracks leave the refined list by design (`exclude=[seed]+liked+disliked`).
  **Observed:** render (100 buttons, seed excluded), vote → re-rank → undo round-trip,
  library-view isolation, zero console errors; all 16 journey-checkpoint exports
  byte-identical before/after the change.

### Fixed
- **Watcher ignored library roots set to a bare drive root** (`web/autoscan.py`
  `_under_any`, MAD 2026-07-19 review): `C:\` kept its trailing separator through
  `abspath`, so the child-prefix check demanded `c:\\` and never matched — events under
  such a root were silently dropped. Now the separator is normalized before the check.
  **Observed:** harness — drive-root child True, sibling prefix-collision still False,
  UNC child True.
- **Observer teardown never joined the dying thread** (`web/autoscan.py` `_stop_watch`):
  a recreated observer could briefly coexist with its stopping predecessor (duplicate
  events, leaked handles). Teardown now `join(timeout=5)`s and surfaces a wedged emitter
  in `last_error`. **Observed:** fake-observer harness — stop→join(5)→is_alive on the
  clean path; wedged path sets `last_error`.
- **`/api/fs/dirs` was reachable from the LAN when the server is bound to 0.0.0.0**
  (`web/app.py`): the wizard's directory browser now answers only loopback clients; other
  machines get 403 instead of a listing of this machine's folder names. **Observed:** real
  app via test client — 127.0.0.1→200, 192.168.1.50→403, ::1→200.
- **Browse re-click orphaned the first picker promise** (`web/static/prefs.js`
  `pickFolder`): opening the picker while it was already open overwrote the pending
  resolver, so the first `await pickFolder()` never settled. A re-click now resolves the
  earlier call as a cancel before installing the new resolver.
- **Folder picker in the first-run wizard and Preferences** (`web/app.py` `/api/fs/dirs`,
  `web/static/{studio.html,studio.css,prefs.js}`): a "Browse…" button opens a server-side
  directory browser (drive list → folders, with Up-navigation) so you can point Attune at
  your music by clicking instead of typing a path. One code path works identically in the
  browser and the desktop (pywebview) window — the listing comes from the server's own
  filesystem, returning absolute paths the analyzer can use, with no native-dialog bridge.
  Read-only, directories only. **Observed:** in the wizard, Browse → navigate `L:\` →
  `L:\_MUSIC` → Choose lands the absolute path in the folder field; the endpoint returns
  drives at the top level and correct parent links.
- **Attune analyzes new music by itself** (`web/autoscan.py`, NEW): two triggers wired onto
  the existing incremental scan pipeline — the `scan_on_launch` setting (present since the
  config spine but previously read by nothing) now fires an incremental scan ~10 s after
  boot, and a new `watch_folders` setting live-watches `library_folders` via `watchdog`
  6.0.0 so dropped-in audio is imported/analyzed/embedded without a Rescan click. Design:
  per-root observer choice (native ReadDirectoryChangesW for local drives, PollingObserver
  for SMB/UNC roots where the native API silently drops events — watchdog's own documented
  guidance), a supervisor thread that recreates silently-dead observers, a 20 s quiet-period
  debounce that leans on the pipeline's verified safe-to-re-run property instead of
  file-readiness heuristics, and changed-directories-only import (a one-track drop no longer
  ffprobes the whole library; analyze/embed stay DB-wide but incremental). `ScanJob.start`
  gained a module-level start lock so background triggers and the HTTP endpoint can't race.
  New `GET /api/watch/status`; "Live watch" checkbox in Preferences → Library; `watchdog>=6`
  added to the `bridge`/`all` extras (graceful degraded mode without it: launch-scan still
  works, watcher reports unavailable). **Observed end-to-end:** 3 files dropped into a
  watched folder auto-triggered import(rc0)/analyze(rc0)/embed-onnx(rc0) ~25 s later, all 3
  rows written with `features`(dim-79 + tempo) and `clap`(dim-512), 0 errors, UI responsive
  mid-scan (stats 147 ms, 20-track mix 16 ms); a follow-up event-noise trigger and the
  launch scan each completed as sub-second no-ops (incremental skip).
- **Frozen Windows app analyzes with no system Python or ffmpeg** (ROADMAP-standalone Phases
  B/C/E): the PyInstaller build (`desktop/build.py` → `dist/Attune/Attune.exe`) now bundles the
  torch-free analyzer (librosa/numba/scipy/soundfile/onnxruntime + `clap_music.onnx`) and
  ffmpeg/ffprobe binaries, so a first-run scan works on a machine with nothing installed. A
  `--attune-worker` self-reinvocation entry point (`desktop/app_desktop.py`) lets the frozen exe
  re-launch *itself* headless to run the analyze/embed stages — a frozen build has no system
  python to subprocess — and `web/scanjob.py`'s frozen routing drives it; the bundled ffmpeg is
  put ahead of any system copy on PATH for the worker only. **Observed:** frozen import→analyze→embed
  on 3 tracks under a scrubbed PATH wrote 3 `features`(dim-79) + 3 `clap`(dim-512) rows, 0 errors;
  the frozen GUI served a V2 mix over the 21k-track library. Bundled ffmpeg is redistributed as a
  separate program (GPLv3, see `NOTICE.md`), not linked. Measured one-folder install: ~975 MB.
- **Torch-free CLAP embedder** (`src/embed_onnx.py` + `src/models/clap_music.onnx` +
  `src/models/clap_norm.json`, ROADMAP-standalone Phase A): the `laion/larger_clap_music`
  audio tower exported to ONNX (opset 17, dynamic batch; per-clip L2 + 3-window mean-pool +
  final L2 inside the graph) with the log-mel front-end ported to pure numpy from
  transformers' exact numeric path. Same protocol, same `clap` table, same resume/err
  semantics as `embed.py` — but deps are numpy + librosa + onnxruntime only (covered by the
  existing `[learned]` extra; new `attune-embed-onnx` script). **Parity gate: min cosine
  0.999999932 over 500 production tracks** (raw audio → numpy mel → onnxruntime vs the
  stored torch-computed rows); the mel front-end is bit-exact vs `ClapFeatureExtractor`.
  `embed.py` stays as the reference/training path. `clap_music.onnx` is tracked via git-LFS
  (275.9 MB > GitHub's 100 MB blob limit).
- **Self-sufficient analyzer — no second Python environment** (ROADMAP-standalone Phase D):
  with no ML venv configured, the Rescan pipeline now analyzes audio torch-free using the
  built-in ONNX CLAP encoder (`embed_onnx.py`) under Attune's *own* interpreter, instead of
  importing metadata only. The ML-venv path is kept as an optional reference/retrain mode
  (`embed.py`, torch). Observed end-to-end: a no-ML scan under a torch-free interpreter
  imported 3 tracks and wrote both a `features` row (librosa-79 + tempo) and a `clap` row
  (dim-512 ONNX vector, 0 errors) for each, via the real `/api/scan/start` endpoint.
- **First-run "point at your music" wizard** (`web/static/studio.html`, `prefs.js`, `boot.js`):
  on a fresh profile (empty `library_folders`) a one-step modal prompts for music folder(s),
  saves them via `/api/settings`, kicks the scan, and hands off to the existing progress
  poller. Gated purely on server state — it never reappears once a library is configured.
- **Honest scan progress** (`prefs.js` `paintScan`): a live ETA (from stage progress + a
  clearly-labelled rough fallback rate), a "safe to close the lid — the scan resumes where it
  left off" reassurance while running (the pipeline is mtime-skip resumable), and the
  restart-to-load note on completion (the engine loads its pool once at startup).
- **Learned-metric engine** (`--engine learned`, `src/engine.py: LearnedEngine`): the
  distilled MusicIP metric head as a third selectable engine. ONNX inference only
  (`onnxruntime`, new `[learned]` extra) — the runtime stays torch-free. Ships with
  `src/models/metric_head.onnx` (591→4096 GELU→512, opset 17, L2-norm in-graph) and
  `src/models/learned_norm.json` (the training-pool z-score stats; verified against the
  training embeddings at min cosine ≥ 0.9999999 over 500 keys). **Selectable, NOT the
  default**: per LAW 1 it can only become the default after a blind ear test.
- **Blind A/B ear-test harness** (`eval/abtest.py`): the LAW 1 ship gate. N genre-diverse
  seeds × one anonymized `.m3u8` per engine (random letter blinding per seed), sealed
  key file under `eval/`, and an interactive `--score` mode that unseals and appends
  verdicts to `eval/abtest_results.jsonl` (pool size stamped per engine, LAW 3).
  Engine-agnostic via the common `src/engine.py` contract.
- **Attune Studio** (`web/studio.py`, `web/static/studio.*`): a full desktop-style UI at `/`,
  laid out like the original MusicIP Mixer — Filters/Playlists tree, cascading
  Genres | Artists | Albums panes with live counts, a sortable/paged track table
  (Track / Title / Length / Artist / Album / Year / Status), album art + a playback
  transport, a right-click context menu (Create Mix, More/Less Like This, Select
  Artist/Album/Genre, Why this pick?), and a status bar. Dark and light themes.
  The previous single-column page is still served at `/classic`.
- Library browsing API: `/api/lib/stats`, `/api/lib/facets` (cascading), `/api/lib/tracks`
  (sort + page), `/api/lib/rows`, and `/api/art` (folder art, then embedded APIC/FLAC picture).
- Playlist folder support (`--playlists DIR`): browse the `.m3u`/`.m3u8` files already on
  disk, open one as a view (entries resolved back to library tracks; unresolvable entries
  are shown, not silently dropped), and **save a mix straight into that folder** via
  `POST /api/export/m3u_dir`. It is the app's only write path and cannot escape the folder.
- Mix length in **minutes** as well as tracks.

### Changed
- **`web/scanjob.py` heavy-stage interpreter routing** now three-way: ML venv set →
  reference path (`embed.py` under that venv); no ML venv, dev → standalone path
  (`embed_onnx.py` under the app's own interpreter, torch-free); no ML venv, frozen →
  honest refusal (scripts not bundled yet, Phase B). The old "no ML venv = import metadata
  only, skip analyze/embed" behavior is gone. The frozen-never-spawn-the-GUI-exe guard
  (audit finding D) is preserved. Verified: all four routing cases asserted against the
  real module (argv per stage), plus the observed end-to-end no-ML scan above.
- `style` is no longer clamped to 0–100. Measured against the live engine, the eligible pool
  is flat to ~400 while the ordering keeps changing, and only collapses past ~845 (where the
  seed's own artist is all that survives). The dial now runs 0–845; the old cap hid ~88% of it.

### Fixed
- **First-run wizard "+ Add folder" did nothing on an empty list** (`web/static/prefs.js`):
  the add-row handler used `collectFolders()`, which filters out empty entries, so clicking
  it while the single starter row was blank dropped that row and re-added one — a net no-op
  that made the wizard feel broken (no way to enter a folder, no feedback). Add-row now reads
  raw input values with empties preserved (`readFolderInputs()`); the button (renamed
  "+ Add another") reliably appends a row. Same fix applied to Preferences → Library.
  **Observed:** one-empty-row → click → two rows (1→2); browse-choose fills the field.
- **Rescan under a frozen (PyInstaller) build** (`web/scanjob.py`, audit finding D): the
  import stage resolved its interpreter as `ml_venv_python or sys.executable`. In a frozen
  build `sys.executable` is `Attune.exe`, not a Python interpreter (and `scan.py` isn't
  bundled), so with no ML venv configured it would silently relaunch the GUI instead of
  importing. Now frozen-aware: when frozen it uses the configured ML venv and, if none is
  set, refuses with an honest message rather than spawning the app exe as if it were python.
  Non-frozen dev behavior is unchanged.
- **Removing a duplicated track from the queue removed every copy** (`web/static/player.js`,
  audit finding E): `removeFromQueue` dropped all queue positions whose pool id matched, so
  a track queued twice lost both when you deleted one. It now removes only one occurrence
  per requested id and never the currently-playing entry.
- Export from the classic page dropped the engine's style/variety (and V2 weight) controls, so
  the exported playlist could differ from the mix shown on screen.

- Acoustic feature extractor (`features.py`): 79-dim descriptor
  (MFCC / chroma / spectral contrast / texture / tempo) via librosa.
- SQLite store (`db.py`): single-file catalog + feature vectors, WAL mode,
  schema-versioned.
- Scanner (`scan.py`): standalone `import-folder` (ffprobe tag reading) and MusicIP
  `import-catalog`; incremental, parallel, resumable `analyze`.
- Mixing engine (`mixer.py`): seed → ranked playlist with **style** (0–100),
  **variety** (0–9), and artist-spacing knobs mirroring MusicIP; tolerant seed-path
  resolution (absolute/relative, separator- and case-insensitive).
- Validation harness (`validate.py`): overlap@K / recall@K / Spearman vs. captured
  MusicIP mixes (optional).
- MusicIP-interop tools (`tools/`): ground-truth capture + catalog parser via the
  `localhost:10002` API.
- Self-contained synthetic demo (`examples/make_demo_library.py`) — try the full
  pipeline with no copyrighted audio.
- Docs: architecture, validation method, MusicIP heritage; MIT license; legal notice.

### Known limitations
- Brute-force similarity (fine to ~10⁵ tracks; swap in an ANN index beyond that).
