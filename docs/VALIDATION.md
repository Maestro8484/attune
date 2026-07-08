# Validating Attune against MusicIP

Attune began as a clean-room modernization of MusicIP Mixer, and one of its goals is to
*measurably* approximate MusicIP's acoustic similarity — not just "feel similar." This is
optional developer tooling; you don't need it to use Attune.

## The idea

If you still have a working MusicIP install, you can capture its playlist output as
**ground truth**, then score how closely Attune reproduces it.

## 1. Capture ground truth from MusicIP

MusicIP exposes a local HTTP API on `localhost:10002` (enable it in
**File → Preferences → Services → API → Start**).

```bash
# Dump MusicIP's catalog to library.json (paths + metadata)
python tools/parse_musicip_library.py     # after saving /api/songs?extended=1 output

# Capture mixes for many seeds at many settings -> data/groundtruth/*.json
python tools/capture_groundtruth.py
```

Each capture records, for a seed track and a `(style, variety, size)` setting, the ordered
playlist MusicIP produced. The "deep" captures (style 40, variety 0, 100 tracks) are the key
oracle: MusicIP's full nearest-neighbour ranking for that seed.

> Keep `data/groundtruth/` **private** — it's derived from your personal library.

## 2. Point Attune at the same tracks

```bash
python src/make_validation_set.py --topn 40     # tracks referenced by the ground truth
python src/scan.py analyze --paths-file data/validation_paths.txt --workers 6
```

## 3. Score

```bash
python src/validate.py --k 25
# or point at a custom ground-truth dir:
ATTUNE_GT=/path/to/groundtruth python src/validate.py --k 25
```

## Metrics

| Metric | Meaning |
|---|---|
| **overlap@K** | fraction of MusicIP's top-K that Attune also ranks in its top-K |
| **recall@K** | of MusicIP's top-K that Attune analyzed, how many it recovered |
| **Spearman** | rank correlation between the two orderings on the common set |
| **coverage** | fraction of each MusicIP ranking Attune had features for (a caveat on low scores) |

Perfect agreement is neither achievable nor the goal — MusicIP used a different (proprietary)
feature set. The aim is *credible* similarity: high overlap on genre/timbre-coherent seeds
and sensible orderings. Use the per-seed breakdown to see where Attune diverges, then tune
the feature-group weights in `mixer.py` (`_group_weights`).

## Tuning loop

1. Run `validate.py`, note `mean_overlap@k` and the worst seeds.
2. Adjust group weights or add/remove features in `features.py`.
3. Re-analyze (bump `SCHEMA_VERSION` if the descriptor changed) and re-score.
4. Repeat until overlap plateaus.
