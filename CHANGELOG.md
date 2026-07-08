# Changelog

All notable changes to Attune are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] — unreleased (initial public cut)

### Added
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
