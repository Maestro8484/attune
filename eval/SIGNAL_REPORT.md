# Behavioral head, Phase 1: signal report and NO-GO

**Verdict: NO-GO. Do not train the behavioral head on this machine's current data.**

The numeric gate in the brief passes. The data does not. The playlists that supply
essentially all of the mined signal are a similarity engine's output, not the operator's
taste, and that is a measured result rather than an inference from their filenames.

---

## 0. Ground-truth rule for this document

This file is a pointer, not standing truth. The local repo and a re-run of the scripts win.
If they disagree with anything below, this file is stale, and whoever notices should say so
out loud and correct it. Every number here carries the command that produced it.

Staleness check:

```
git log -1 --oneline                                  # where the branch is now
git log -1 --oneline -- eval/SIGNAL_REPORT.md         # when this doc was last true
```

Same hash means nothing has landed since this was written. Different means stale by exactly
that gap.

State as recorded: branch `feature/behavioral-head`, worktree `attune-personal-wt`, DB
`mixer-ng/data/mixer.db` opened read-only, playlists `M:\_LAN-Playlists` read-only.

Reproduce everything:

```
# Phase 1, counts (lean venv)
mixer-ng\.venv\Scripts\python.exe eval\build_behavior_pairs.py ^
    --db mixer-ng\data\mixer.db --playlists "M:\_LAN-Playlists" ^
    --out eval\behavior_pairs.json --emit-pairs

# Phase 1b, is it taste? (ML venv, needs CUDA + onnxruntime)
mixer-ng\.venv-ml\Scripts\python.exe eval\probe_behavior_signal.py ^
    --db mixer-ng\data\mixer.db --pairs eval\behavior_pairs.json ^
    --playlists "M:\_LAN-Playlists"
```

---

## 1. What was mined

Pool: **21,236 tracks** with both CLAP-512 and a finite librosa-79, which is exactly
`HybridEngine`'s clap-intersect-librosa pool. Every retrieval number in this document is
recall@25 over that pool, random baseline **0.00118** (LAW 3: the pool size is part of the
number). All of them are convergence diagnostics. None of them may select what ships
(LAW 1).

### Source A, playlist co-occurrence

| | count |
|---|---|
| playlist files found (non-ABTest) | 78 |
| playlist files excluded (`ABTest\`, `ABTest_v3\`) | 120 |
| local path lines | 7,841 |
| resolved to a pool track | 6,059 (77.3%) |
| unresolved | 1,782 |
| stream/URL lines skipped | 1 |
| lists with >= 5 resolved tracks | 72 |
| **distinct lists after content-dedupe** | **25** |
| exact duplicate copies dropped | 47 |
| positive pairs | 85,866 |
| distinct tracks covered | 1,629 (7.7% of pool) |

The 47 dropped copies are the same lists saved four times over: root as `.m3u.bak`,
`MiniPC\`, `MiniPC\m3u non edited\`, and `MiniPC\*_2.m3u`. Verified identical by content
fingerprint on the resolved sequence, so the `m3u non edited` folder name notwithstanding,
nobody edited anything. There is no operator-edit delta to mine.

Path resolution uses `src/musicip_engine.relkey`, the mirror-invariant already used by
`MusicIPAdapter` and `eval/bakeoff_musicip.py`. The 1,782 unresolved lines are mostly
`\\DiskStation\...\Songs\...` singles that are not in the analyzed pool.

### Source B, usermeta

This is the in-app behavioral signal, the thing a personal taste engine is actually
supposed to be built from.

| field | count |
|---|---|
| usermeta rows | 21,454 (in pool: 21,236) |
| `loved = 1` | **1** |
| `rating >= 4` | **4** |
| `play_count >= 1` | **7** |
| `play_count >= 2` | 1 |
| `skip_count >= 2` (hard negatives) | **0** |
| tracks with any signal at all | **9** |
| positive pairs from B | 6 |

Nine tracks. Source B is empty for training purposes. The `date_added` column is populated
for all 21,454 rows, but that is the one-time backfill from `features.analyzed_at` that
`web/userdata.py::_ensure_schema` performs, not listening behaviour.

### Gate as written

| gate | value | threshold | result |
|---|---|---|---|
| combined positive pairs | 85,872 | >= 20,000 | PASS |
| distinct tracks covered | 1,633 | >= 1,500 | PASS |

**Numeric verdict: GO. Actual verdict: NO-GO.** Sections 2 and 3 are why.

---

## 2. The pair count is an artifact, not a sample size

85,872 pairs come from 25 lists. Pairs inside one playlist are not independent
observations. A 130-track list contributes C(130,2) = 8,385 pairs and exactly **one**
observation of taste.

```
sum C(n,2) over the 25 lists = 90,314
independent observations     = 25
oversampling factor          = 3,613x
```

List sizes: 130, 120, 117, 112, 100, 100, 99, 99, 95, 88, 87, 87, 82, 81, 79, 77, 72, 71,
70, 69, 64, 50, 37, 15, 8.

Per-track it is thinner still: **1,410 of the 1,633 covered tracks appear in essentially
one list** (pair-degree <= 130, median degree 98). For 86% of covered tracks the entire
behavioural record is a single co-occurrence event.

The 20,000-pair threshold was a proxy for "enough listening data". Against C(n,2) inflation
it is satisfied by 25 playlists, so it does not measure what it was meant to measure. The
gate that would have caught this is a count of independent *lists*, and 25 is far too few
to fit a 4.7M-parameter head.

For the record, the artist confound is *not* the problem here: only 1.7% of pairs share an
artist, across 421 distinct artists, top-20 artists accounting for 27.6% of covered tracks.

---

## 3. The playlists are the teacher's output, not the operator's taste

This is the finding that settles it, and it is measured, not inferred.

`src/models/metric_head.onnx` is the shipped learned head: a distillation of MusicIP's
similarity function, trained on thousands of MusicIP rank lists in the MusicIP workspace.
**It has never seen any of these playlists.** If they were personal taste, it should do no
better on them than plain CLAP.

Leave-lists-out, 5 folds, pool 21,236, recall@25:

| engine | recall@25 | vs CLAP |
|---|---|---|
| CLAP raw (untrained baseline) | 0.0110 | - |
| **shipped MusicIP head, zero training on these lists** | **0.0749** | **+0.0639 (6.8x)** |
| behavior head trained *on* these lists (held-out folds) | 0.0375 | +0.0265 (3.4x) |

A model of MusicIP's similarity function reconstructs these playlists **twice as well as a
head trained directly on them**, for free. The effect holds on every one of the 24 usable
lists individually, from 0.014 to 0.144, all far above their CLAP baselines of 0.000 to
0.024.

That is what it looks like when the "taste data" is the teacher's output wearing a
playlist's clothes. Training on it would launder MusicIP's opinion back to the operator as
"your personal taste", and would do it *worse* than the head already shipped, because it is
fit on 24 seeds instead of thousands.

The corroborating file evidence, which on its own would only have been suggestive:
filenames are `like-<seed>` / `Like-<seed>`, sizes are round (150/100/50, plus one 101),
`(MB)` marks MusicBee output whose Similar-To feature is MusicIP-backed, and there is a
folder literally named `_Similar To\`.

### Controls that rule out the obvious alternative explanations

Same folds, same config, pool 21,236:

| control | result | reading |
|---|---|---|
| C1, seed-blind membership prior alone | 0.0120 vs CLAP 0.0110 | a global "was in a playlist" prior explains almost none of the gain |
| C2, true inductive (pool strips every training-list track, ~19,900 left) | CLAP 0.0092 -> head 0.0255 | the gain survives removing train/test track overlap |
| **C3, shuffled-label null** (same lists, membership randomly permuted) | **0.0176, i.e. +0.0066 over CLAP from no signal at all** | the protocol manufactures 25% of the apparent gain out of nothing |

Signal-attributable gain is therefore trained minus shuffled = **+0.0199**, not the
headline +0.0265. And per section 3, what little is left is MusicIP structure that the
shipped head already models better.

### Coherence does not distinguish, and it is worth recording that it does not

Mean pairwise CLAP cosine in excess of an equal-size random sample:

| set | n lists | mean z | excess cosine |
|---|---|---|---|
| candidate lists (non-ABTest) | 24 | 2.5 | +0.0185 |
| control: `ABTest\` lists, known engine output | 45 | 1.3 | +0.0254 |

The known-machine control is *more* coherent than the candidates. Acoustic coherence cannot
tell an operator-made list from an engine-made one, so it should not be cited either way. A
playlist-position monotonicity test was also run and came back null (mean Spearman 0.023,
median -0.003), meaning these lists are not sorted by CLAP distance from their first track.
Neither test was able to settle provenance. The teacher test in section 3 did.

---

## 4. Recommendation

**Do not train.** The blocker is not tuning, architecture, or the loss. It is that no
record of this operator's own choices exists on this machine yet, in usable quantity.

What would unblock it, in order of leverage:

1. **Do the listening inside Attune.** Source B is the right signal and it is at 9 tracks.
   The plumbing is not the problem: `web/static/player.js` already reports a play at
   `reportPlayIfDue` (line 420, fires at 50% of duration or 240s, whichever is less) and a
   skip at `reportSkipIfAbandoned` (line 431, fires between 3s and 33%), both into
   `usermeta` via `web/userdata.py`. The counters are near zero because the app has been
   driven as a dev and evaluation tool rather than as the daily player, so almost nothing
   has ever been played through it end to end. This costs no engineering, only use. Rough
   target for a retry: **2,000+ tracks with a play or skip event, and 300+ loved/rated**,
   which at normal listening is months, not weeks.
2. **Import an existing listening history.** A Last.fm scrobble export is the single
   highest-leverage unlock, since it is years of already-recorded real behaviour rather
   than months of waiting. Scrobbles resolve to tracks by artist/title and give both
   play counts and, through session adjacency, genuine co-listening pairs. This is the
   recommended path.
3. **Hand-curate playlists deliberately, and label them.** If the operator builds lists
   himself, keep them out of `_LAN-Playlists` or mark them, so provenance never has to be
   reverse-engineered again. 25 machine lists is what the current folder amounts to.

Retry gate for the next attempt, replacing the pair-count gate that failed to bind:

- **>= 300 independent lists or listening sessions** (not pairs), and
- **>= 3,000 distinct tracks covered**, and
- the teacher test in `eval/probe_behavior_signal.py` shows the shipped MusicIP head
  performing **no better than CLAP** on the new data, which is what confirms the data is
  taste rather than engine output.

Until then the honest position is that Attune has one learned head, it is a MusicIP
distillation, and there is no second signal on this machine to build a personal one from.

---

## 5. What was built and left in place

| file | purpose | state |
|---|---|---|
| `eval/build_behavior_pairs.py` | Phase 1 miner, sources A + B, provenance classification, gate | works, run above |
| `eval/train_behavior_head.py` | Phase 2 trainer, 591 -> 4096 GELU -> 512 InfoNCE, track holdout and list holdout | works, run in probe mode only |
| `eval/probe_behavior_signal.py` | Phase 1b, coherence + C1/C2/C3 + teacher test | works, produced section 3 |
| `eval/behavior_pairs.json` | mined pairs, per-list membership, counts | generated, gitignored if large |
| `eval/behavior_signal_probe.json` | probe results | generated |

The trainer is complete and deliberately left in the tree. When real signal arrives, Phase 2
is `--mode train` and nothing else needs writing. It defaults to `--mode probe` so that the
generalisation question is asked before anything is believed.

**Nothing outside `eval/` was touched.** No `PersonalEngine` was registered, `src/engine.py`
and `web/app.py` are unmodified, no ONNX was exported, and the default V2 path is
untouched by construction. Phases 2 and 3 were not run, per the brief's instruction to stop
at a NO-GO rather than make the deliverable look complete.
