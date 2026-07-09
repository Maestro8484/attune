"""CLAP-alone vs current weighted blend, on the SAME labelled pairs.

Read-only experiment. Writes ONLY under attune/eval/. Does NOT edit hybrid.py --
it imports HybridEngine (unmodified) exactly like harness.py does, and additionally
imports harness.py's own label-bootstrap / eval / summarize functions so the two
conditions are scored on IDENTICAL seeds, labels, and pool.

Method for the "same pool" requirement: HybridEngine's pool selection depends on
whether the 'lib' weight is truthy at construction time (use_lib flag) -- so we
construct the engine ONCE with the current blend weights (pool = clap ∩ librosa,
matching production), bootstrap labels/seeds ONCE against that pool, score the
blend, then MUTATE eng.w in place to a clap-only dict and score again on the exact
same seeds/labels/pool. HybridEngine._score() reads self.w at call time (see
src/hybrid.py), so this in-place mutation is safe and doesn't require touching the
file or rebuilding the engine.

Usage:
    python attune/eval/compare_clap_alone.py --db path/to/mixer.db
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from hybrid import HybridEngine, DEFAULT_WEIGHTS  # noqa: E402
from harness import (  # noqa: E402
    bootstrap_album_artist_positives,
    find_clap_dupes,
    evaluate,
    summarize,
)

CLAP_ONLY_WEIGHTS = {"clap": 1.0, "lib": 0.0, "genre": 0.0, "bpm": 0.0, "era": 0.0, "key": 0.0}


def run():
    ap = argparse.ArgumentParser(description="CLAP-alone vs weighted-blend eval, same labelled pairs")
    ap.add_argument("--db", required=True)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--n-seeds", type=int, default=900)
    ap.add_argument("--neg-per-seed", type=int, default=8)
    ap.add_argument("--dupe-scan-sample", type=int, default=8000)
    ap.add_argument("--rng-seed", type=int, default=42)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    t0 = time.time()
    rng = random.Random(args.rng_seed)

    print(f"[compare] loading HybridEngine (blend weights) from {args.db} ...")
    eng = HybridEngine(args.db)  # default (blend) weights -> establishes the pool
    blend_weights = dict(eng.w)
    n_pool = len(eng.paths)
    print(f"[compare] pool (clap{'+librosa' if eng.use_lib else ''}): {n_pool} tracks")
    print(f"[compare] blend weights: {blend_weights}")

    print("[compare] bootstrapping same-album / same-artist weak positives ...")
    album_pos, artist_pos, n_album_groups = bootstrap_album_artist_positives(eng, rng=rng)
    print(f"[compare]   album-mate labels: {len(album_pos)} tracks (from {n_album_groups} groups)")
    print(f"[compare]   artist-mate labels: {len(artist_pos)} tracks")

    print("[compare] scanning for CLAP near-duplicates (cosine >= 0.999) ...")
    dupe_pos, dupe_scan_n = find_clap_dupes(eng, scan_sample=args.dupe_scan_sample, rng=rng)
    print(f"[compare]   scanned {dupe_scan_n}/{n_pool} rows; found {len(dupe_pos)} tracks with a twin")

    candidates = sorted(set(album_pos) | set(artist_pos) | set(dupe_pos))
    print(f"[compare] labelled candidate seeds available: {len(candidates)}")

    all_dupe_seeds = list(dupe_pos.keys())
    dupe_take = min(len(all_dupe_seeds), max(1, args.n_seeds // 3))
    dupe_seeds = (rng.sample(all_dupe_seeds, dupe_take)
                  if dupe_take < len(all_dupe_seeds) else all_dupe_seeds)
    rest = [p for p in candidates if p not in dupe_pos]
    budget = max(args.n_seeds - len(dupe_seeds), 0)
    sampled_rest = rng.sample(rest, min(budget, len(rest)))
    seeds = dupe_seeds + sampled_rest
    print(f"[compare] SAMPLED {len(seeds)} eval seeds "
          f"({len(dupe_seeds)} dupe-guaranteed + {len(sampled_rest)} weak-positive-only) "
          f"-- IDENTICAL seed set used for BOTH conditions below")

    # --- Condition A: current weighted blend (engine as constructed) ---
    print(f"\n[compare] scoring CONDITION A: weighted blend {blend_weights} ...")
    # Same negative-sampling rng seed reused for both conditions below so the sampled
    # cross-genre negatives per seed are IDENTICAL across A and B, not just the seed list.
    rows_blend = evaluate(eng, seeds, album_pos, artist_pos, dupe_pos, args.k, args.neg_per_seed,
                           random.Random(args.rng_seed + 1))
    summary_blend = summarize(rows_blend, args.k)

    # --- Condition B: CLAP-alone. Mutate eng.w IN PLACE -- pool/labels/seeds untouched. ---
    print(f"[compare] scoring CONDITION B: CLAP-alone {CLAP_ONLY_WEIGHTS} ...")
    eng.w = dict(CLAP_ONLY_WEIGHTS)
    rows_clap = evaluate(eng, seeds, album_pos, artist_pos, dupe_pos, args.k, args.neg_per_seed,
                          random.Random(args.rng_seed + 1))
    summary_clap = summarize(rows_clap, args.k)

    elapsed = round(time.time() - t0, 1)

    def fmt(v):
        return f"{v:.4f}" if isinstance(v, float) else str(v)

    print("\n" + "=" * 78)
    print("CLAP-ALONE  vs  WEIGHTED BLEND -- SAME labelled pairs / SAME pool / SAME seeds")
    print("=" * 78)
    print(f"pool size (scoreable tracks): {n_pool}")
    print(f"eval seeds scored: {len(seeds)}  (of {len(candidates)} labelled candidates)")
    print(f"blend weights: {blend_weights}")
    print(f"clap-alone weights: {CLAP_ONLY_WEIGHTS}")
    print("-" * 78)
    rows = [
        ("precision@{}".format(args.k), "precision_at_k"),
        ("MRR", "mrr"),
        ("album-mate precision@k", "album_precision_at_k"),
        ("album-mate MRR", "album_mrr"),
        ("artist-mate precision@k", "artist_precision_at_k"),
        ("artist-mate MRR", "artist_mrr"),
        ("dupe pass-rate (rank==1)", "dupe_pass_rate"),
        ("neg mean percentile (want ~1.0)", "neg_mean_percentile"),
        ("neg top-k leak rate (want ~0.0)", "neg_topk_leak_rate"),
    ]
    header = f"{'metric':38s} {'BLEND':>12s} {'CLAP-ALONE':>12s} {'delta(clap-blend)':>18s}"
    print(header)
    print("-" * len(header))
    verdict_rows = {}
    for label, key in rows:
        b = summary_blend.get(key, float("nan"))
        c = summary_clap.get(key, float("nan"))
        delta = c - b if (isinstance(b, float) and isinstance(c, float)) else float("nan")
        verdict_rows[key] = {"blend": b, "clap_alone": c, "delta": delta}
        print(f"{label:38s} {fmt(b):>12s} {fmt(c):>12s} {delta:>+18.4f}")
    print("-" * 78)
    print(f"n dupe seeds tested: blend={summary_blend['dupe_seeds_tested']}  "
          f"clap-alone={summary_clap['dupe_seeds_tested']}  (must match -- same seed set)")
    print(f"elapsed: {elapsed}s")
    print("=" * 78)

    # Plain verdict text (data-backed, not a taste judgment -- machine-checkable deltas only).
    pk_delta = verdict_rows["precision_at_k"]["delta"]
    mrr_delta = verdict_rows["mrr"]["delta"]
    dupe_delta = verdict_rows["dupe_pass_rate"]["delta"]
    verdict_lines = []
    verdict_lines.append(
        f"precision@{args.k}: clap-alone {'beats' if pk_delta > 0 else ('matches' if abs(pk_delta) < 1e-6 else 'loses to')} "
        f"blend by {pk_delta:+.4f} ({summary_clap['precision_at_k']:.4f} vs {summary_blend['precision_at_k']:.4f})"
    )
    verdict_lines.append(
        f"MRR: clap-alone {'beats' if mrr_delta > 0 else ('matches' if abs(mrr_delta) < 1e-6 else 'loses to')} "
        f"blend by {mrr_delta:+.4f} ({summary_clap['mrr']:.4f} vs {summary_blend['mrr']:.4f})"
    )
    verdict_lines.append(
        f"dupe pass-rate: clap-alone {'beats' if dupe_delta > 0 else ('matches' if abs(dupe_delta) < 1e-6 else 'loses to')} "
        f"blend by {dupe_delta:+.4f} ({summary_clap['dupe_pass_rate']:.4f} vs {summary_blend['dupe_pass_rate']:.4f})"
    )
    print("\nVERDICT (data-backed, precision/MRR/dupe-rank only -- not a taste judgment):")
    for line in verdict_lines:
        print(f"  - {line}")

    out_path = args.out or os.path.join(_HERE, f"compare_clap_alone_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(out_path, "w") as f:
        json.dump({
            "n_pool": n_pool,
            "n_seeds": len(seeds),
            "n_candidates": len(candidates),
            "blend_weights": blend_weights,
            "clap_only_weights": CLAP_ONLY_WEIGHTS,
            "summary_blend": summary_blend,
            "summary_clap_alone": summary_clap,
            "comparison": verdict_rows,
            "verdict_lines": verdict_lines,
            "elapsed_sec": elapsed,
        }, f, indent=2)
    print(f"\n[compare] full report written to {out_path}")


if __name__ == "__main__":
    run()
