# Architecture

Attune is deliberately small: a feature extractor, a SQLite store, a ranking engine, and a
validation harness. No services, no cloud, no vector database.

```
  music files ──► features.py ──► db.py (SQLite) ──► mixer.py ──► playlist
                     (librosa)         │                 ▲
                                       └── validate.py ──┘  (optional: score vs MusicIP)
```

## Components

### `src/features.py` — acoustic descriptor
Loads audio at 22.05 kHz mono, analyzes a central window (default 90 s, to skip intros/outros
and bound cost), and produces a **79-dim float32 vector** plus scalar tempo/duration.

Layout (kept in sync with `FEATURE_GROUPS` so the mixer can re-weight groups):

| Offset | Dims | Feature |
|---|---|---|
| 0–39 | 40 | MFCC mean (20) + std (20) — timbre |
| 40–63 | 24 | Chroma CQT mean (12) + std (12) — harmony |
| 64–70 | 7 | Spectral contrast mean |
| 71–77 | 7 | centroid, bandwidth, rolloff, ZCR, RMS mean, RMS std, flatness |
| 78 | 1 | Tempo (BPM) |

To change the descriptor, bump `SCHEMA_VERSION` in `db.py` so stale vectors are recomputed.

### `src/db.py` — storage
Single SQLite file (`data/mixer.db`), WAL mode. Two tables: `tracks` (catalog) and
`features` (vector blob + tempo + error). Feature vectors are stored as raw little-endian
float32 bytes. `load_matrix()` returns `(paths, N×dim matrix, meta)` for the engine.

Errors are recorded (not just successes) so a file that fails to decode isn't retried on
every run.

### `src/scan.py` — cataloging + analysis
- `import-folder <dir>` — walk a directory, read tags via **ffprobe** (shelled out), populate
  `tracks`. This is the standalone path; MusicIP not required.
- `import-catalog <library.json>` — load a MusicIP metadata dump instead.
- `analyze` — extract features for all un-analyzed tracks. Thread pool (librosa/numba release
  the GIL); incremental and resumable; commits every 25 tracks. `--paths-file` restricts to a
  subset (used for fast validation before a full-library run).

### `src/mixer.py` — the engine
Z-scores features across the library, then for a seed computes weighted Euclidean distance to
every track.
- **style (0–100)** sets a per-group weight vector: low style emphasizes timbre (MFCC); high
  style flattens weights so harmony/rhythm/texture matter more.
- **variety (0–9)**: 0 = deterministic nearest-neighbour walk with artist spacing; >0 widens
  the candidate pool to `size×(1..8)` and samples with rank-decaying probability (temperature
  grows with variety).
- **artist_spacing** enforces a minimum gap between same-artist tracks.

Brute-force distance over the whole matrix is fine at library scale (tens of thousands of
rows × 79 dims is a few ms in NumPy). If you ever need 10⁶+ tracks, swap the linear scan for
an ANN index (FAISS/hnswlib) behind the same `rank()` interface.

### `src/validate.py` — fidelity scoring (optional)
Compares Attune's ranking to captured MusicIP mixes. See [VALIDATION.md](VALIDATION.md).

## Design principles

1. **Local-first, always.** Nothing leaves the machine.
2. **Transparent over clever.** Every knob maps to an explainable operation on explainable
   features. No opaque embedding you can't reason about.
3. **One file to back up.** The whole state is `data/mixer.db`.
4. **Right-sized.** No microservices for a single-user tool. Scale the storage/search only
   if a real workload demands it.
