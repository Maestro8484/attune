# Changelog

All notable changes to Attune are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] — unreleased (initial public cut)

### Added
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
