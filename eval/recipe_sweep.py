"""MusicIP knob CHARACTERIZATION sweep — descriptive, NOT optimization.

MusicIP's native /api/mix exposes two user-facing knobs: "style" (0-100) and "variety"
(0-9, per MIX_DEFAULTS/bridge.py's proven usage). This script sweeps a grid of those two
knobs across a sample of seeds MusicIP knows, and MEASURES what each cell's resulting mix
looks like on properties WE can compute from our own DB (mixer.db, read-only) after
reconciling MusicIP's UNC paths back to our pool via musicip_engine.build_reconciler():

  * distinct-artist ratio   = unique artists / resolved tracks in the mix
  * distinct-genre count    = size of the union of genre tags across the mix
  * mean |tempo - seed bpm| = mean absolute tempo gap vs the seed (only pairs where both
                              our features.tempo is known; MusicIP's own bpm is not used
                              here, ours is, so this is directly comparable across seeds)
  * year range              = max(year) - min(year) among resolved tracks with a known year
  * distinct decades        = number of distinct (year // 10 * 10) buckets, same tracks

This is NOT a quality/optimization eval (see harness.py / bakeoff_musicip.py for that) --
it does not rank cells as "better", it only reports how each property MOVES as style/variety
change, aggregated over seeds. No thresholds are tuned toward any target here.

Reuses: musicip_engine.MusicIPEngine (the live /api/mix wrapper), musicip_engine.relkey /
build_reconciler (the same UNC<->L: path reconciliation musicip_engine.py's own self-test
and bakeoff_musicip.py use), and hybrid.HybridEngine purely as a read-only metadata source
(artist/genre/year/tempo) for our DB -- no scoring, no engine changes.

Run:
    python attune/eval/recipe_sweep.py --db mixer-ng/data/mixer.db
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")

STYLES = (0, 20, 40, 60, 80, 100)
VARIETIES = (0, 3, 6, 9)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


HY = _load("hybrid", os.path.join(SRC, "hybrid.py"))
ME = _load("musicip_engine", os.path.join(SRC, "musicip_engine.py"))


def _disp(path):
    """Basename only -- never print/store real library filesystem paths (see harness.py's
    _disp for the same rule)."""
    return os.path.basename(path) if path else path


def _validate_out_path(out_arg, eval_dir):
    eval_real = os.path.realpath(eval_dir)
    out_real = os.path.realpath(out_arg)
    try:
        common = os.path.commonpath([eval_real, out_real])
    except ValueError:
        common = None
    if common != eval_real:
        raise SystemExit(
            f"[recipe_sweep] --out must be inside {eval_real} "
            f"(got: {out_arg!r} -> resolves to {out_real!r})")
    return out_real


def pick_seeds(eng, mip_by_relkey, n_seeds, rng):
    """Seeds MusicIP knows AND our DB has a known bpm + year for (so the tempo-gap and
    era metrics are actually computable for every seed, not sparsely NaN)."""
    candidates = []
    for i, p in enumerate(eng.paths):
        if ME.relkey(p) not in mip_by_relkey:
            continue
        if eng.bpm[i] <= 0:
            continue
        if eng.year[i] <= 0:
            continue
        candidates.append(p)
    rng.shuffle(candidates)
    return candidates[:n_seeds], len(candidates)


def measure_mix(eng, recon, seed_idx, mix_paths):
    """Reconcile MusicIP's UNC result list to our pool and compute the descriptive
    properties for one (seed, style, variety) cell. Returns a dict; some keys are None
    if the metric had no computable data (e.g. no resolved track had a known year)."""
    seen, kept = set(), []
    for p in mix_paths:
        idx = recon.get(ME.relkey(p))
        if idx is None or idx == seed_idx or idx in seen:
            continue
        seen.add(idx)
        kept.append(idx)

    n_resolved = len(kept)
    out = {"n_returned": len(mix_paths), "n_resolved": n_resolved}
    if n_resolved == 0:
        out.update(distinct_artist_ratio=None, distinct_genre_count=None,
                    mean_abs_tempo_gap=None, year_range=None, n_distinct_decades=None)
        return out

    artists = [eng.artist[i] for i in kept]
    named_artists = [a for a in artists if a]
    out["distinct_artist_ratio"] = (len(set(named_artists)) / n_resolved) if named_artists else None

    genre_union = set()
    for i in kept:
        genre_union |= eng.genre_tags[i]
    out["distinct_genre_count"] = len(genre_union)

    seed_bpm = eng.bpm[seed_idx]
    gaps = [abs(eng.bpm[i] - seed_bpm) for i in kept if eng.bpm[i] > 0]
    out["mean_abs_tempo_gap"] = (sum(gaps) / len(gaps)) if (gaps and seed_bpm > 0) else None

    years = [eng.year[i] for i in kept if eng.year[i] > 0]
    if years:
        out["year_range"] = max(years) - min(years)
        out["n_distinct_decades"] = len({int(y // 10 * 10) for y in years})
    else:
        out["year_range"] = None
        out["n_distinct_decades"] = None

    return out


def main():
    ap = argparse.ArgumentParser(
        description="Characterize what MusicIP's style/variety knobs DO (descriptive, not an optimization)")
    ap.add_argument("--db", required=True, help="our mixer DB, read-only")
    ap.add_argument("--url", default="http://localhost:10002")
    ap.add_argument("--n-seeds", type=int, default=25)
    ap.add_argument("--size", type=int, default=20, help="MusicIP mix size per cell")
    ap.add_argument("--rng-seed", type=int, default=42)
    ap.add_argument("--out", default=None,
                     help="optional JSON report path (default: attune/eval/recipe_sweep_<ts>.json)")
    args = ap.parse_args()

    rng = random.Random(args.rng_seed)
    t0 = time.time()

    print(f"[recipe_sweep] loading our DB metadata (artist/genre/year/tempo) from {args.db} ...")
    eng = HY.HybridEngine(args.db)
    print(f"[recipe_sweep] our pool: {len(eng.paths):,} tracks")

    print(f"[recipe_sweep] connecting to MusicIP at {args.url} ...")
    mip = ME.MusicIPEngine(args.url)
    if not mip.alive():
        raise SystemExit(f"[recipe_sweep] MusicIP not responding on {args.url}")
    mip_songs = mip.songs()
    mip_by_relkey = {ME.relkey(p): p for p in mip_songs}
    recon = ME.build_reconciler(eng.paths)
    resolved_lib = sum(1 for p in mip_songs if ME.relkey(p) in recon)
    print(f"[recipe_sweep] MusicIP knows {len(mip_songs):,} tracks; "
          f"{resolved_lib:,} reconcile into our pool ({100 * resolved_lib // max(1, len(mip_songs))}%)")

    seeds, n_candidates = pick_seeds(eng, mip_by_relkey, args.n_seeds, rng)
    print(f"[recipe_sweep] SAMPLED {len(seeds)} seeds (known to MusicIP + known bpm + known "
          f"year in our DB) out of {n_candidates} eligible candidates "
          f"(rng-seed={args.rng_seed}, no further cell subsampling: the full "
          f"{len(STYLES)}x{len(VARIETIES)}={len(STYLES) * len(VARIETIES)}-cell grid is run "
          f"for every seed since a live /api/mix call measured ~15ms locally)")
    if not seeds:
        raise SystemExit(
            "[recipe_sweep] ERROR: no eligible seeds (need tracks MusicIP knows AND our DB "
            "has both a known tempo and a known year for). Refusing to report on an empty sample.")

    total_calls = len(seeds) * len(STYLES) * len(VARIETIES)
    print(f"[recipe_sweep] sweeping {len(STYLES)} styles x {len(VARIETIES)} varieties x "
          f"{len(seeds)} seeds = {total_calls} /api/mix calls (size={args.size}) ...")

    # cell key -> list of per-seed metric dicts
    cells = defaultdict(list)
    n_calls, n_errors = 0, 0
    for seed in seeds:
        si = eng.idx[seed]
        seed_unc = mip_by_relkey[ME.relkey(seed)]
        for style in STYLES:
            for variety in VARIETIES:
                n_calls += 1
                try:
                    mix = mip.similar(seed_unc, size=args.size, style=style, variety=variety)
                except Exception as e:
                    n_errors += 1
                    print(f"[recipe_sweep]   WARN: mix call failed for seed={_disp(seed)} "
                          f"style={style} variety={variety}: {e}")
                    continue
                m = measure_mix(eng, recon, si, mix)
                m["seed"] = _disp(seed)
                cells[(style, variety)].append(m)

    print(f"[recipe_sweep] done: {n_calls} calls issued, {n_errors} errors "
          f"({time.time() - t0:.1f}s elapsed)")

    METRICS = ("distinct_artist_ratio", "distinct_genre_count", "mean_abs_tempo_gap",
               "year_range", "n_distinct_decades")

    def cell_means(rows):
        out = {"n_seeds": len(rows)}
        for m in METRICS:
            vals = [r[m] for r in rows if r.get(m) is not None]
            out[m] = (sum(vals) / len(vals)) if vals else None
            out[m + "_n"] = len(vals)
        return out

    agg = {f"style={s},variety={v}": cell_means(cells[(s, v)]) for s in STYLES for v in VARIETIES}

    # --- compact table: rows = style, cols = variety, one table per metric ---
    print("\n" + "=" * 78)
    print("MUSICIP KNOB CHARACTERIZATION (descriptive only -- not tuned for quality)")
    print("=" * 78)
    print(f"seeds: {len(seeds)}   mix size: {args.size}   grid: "
          f"{len(STYLES)} styles x {len(VARIETIES)} varieties, full (no cell sampling)")

    def fmt(v):
        return f"{v:6.3f}" if isinstance(v, float) else f"{'--':>6}"

    for metric in METRICS:
        print(f"\n-- {metric} --  (rows=style, cols=variety)")
        header = "style\\var" + "".join(f"{v:>8}" for v in VARIETIES)
        print(header)
        for s in STYLES:
            row = f"{s:>9}"
            for v in VARIETIES:
                cell = agg[f"style={s},variety={v}"]
                row += f"{fmt(cell[metric]):>8}"
            print(row)

    # --- marginal directional read: extremes of each knob, averaged over the OTHER knob ---
    print("\n" + "-" * 78)
    print("DIRECTIONAL READ (marginal means at knob extremes, averaged over the other knob)")
    print("-" * 78)

    def marginal(fix_key, fix_val, metric):
        vals = []
        for s in STYLES:
            for v in VARIETIES:
                if (fix_key == "style" and s != fix_val) or (fix_key == "variety" and v != fix_val):
                    continue
                cell = agg[f"style={s},variety={v}"]
                if cell[metric] is not None:
                    vals.append(cell[metric])
        return (sum(vals) / len(vals)) if vals else None

    for metric in METRICS:
        lo_v, hi_v = marginal("variety", VARIETIES[0], metric), marginal("variety", VARIETIES[-1], metric)
        lo_s, hi_s = marginal("style", STYLES[0], metric), marginal("style", STYLES[-1], metric)

        def arrow(lo, hi):
            if lo is None or hi is None:
                return "?"
            if hi > lo * 1.02:
                return "UP"
            if hi < lo * 0.98:
                return "DOWN"
            return "flat"

        print(f"{metric:<24} variety {VARIETIES[0]}->{VARIETIES[-1]}: "
              f"{fmt(lo_v)} -> {fmt(hi_v)}  ({arrow(lo_v, hi_v)})   |   "
              f"style {STYLES[0]}->{STYLES[-1]}: {fmt(lo_s)} -> {fmt(hi_s)}  ({arrow(lo_s, hi_s)})")

    print(f"\nelapsed: {time.time() - t0:.1f}s")
    print("=" * 78)

    report = {
        "n_seeds": len(seeds),
        "n_candidates_eligible": n_candidates,
        "mix_size": args.size,
        "styles": STYLES,
        "varieties": VARIETIES,
        "n_calls": n_calls,
        "n_errors": n_errors,
        "elapsed_sec": round(time.time() - t0, 1),
        "cells": agg,
    }
    out_path = (_validate_out_path(args.out, HERE) if args.out
                else os.path.join(HERE, f"recipe_sweep_{time.strftime('%Y%m%d_%H%M%S')}.json"))
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n[recipe_sweep] full report written to {out_path}")


if __name__ == "__main__":
    main()
