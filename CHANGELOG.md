# Changelog

All notable changes to Attune are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] — unreleased (initial public cut)

### Fixed
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
- No playlist export or player integration yet (roadmap: `.m3u`, Plex, Jellyfin).
- No GUI yet — CLI only; the engine is UI-agnostic.
- Brute-force similarity (fine to ~10⁵ tracks; swap in an ANN index beyond that).
