# Changelog

All notable changes to Attune are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] — unreleased (initial public cut)

### Added
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
