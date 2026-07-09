"""Bake-off: MusicIP vs our CLAP/V2 engine on the SAME eval harness.

Respect-the-art: the yardstick decides the pivot's default engine, not the plan. Reuses
harness.py's labels (album/artist mates, CLAP dupes) and summarize(); scores both engines on
the SAME seeds (only seeds MusicIP knows). MusicIP's ranking = its /api/mix output,
position-ranked with everything else tied one past the last returned track -- the same
"dropped tracks tie at worst_rank" semantics harness.mix_rank_array() uses for CLAP.

CAVEAT (logged, not hidden): CLAP scores over the full 21,236 pool; MusicIP only knows the
~17,447 Albums, so any album/artist-mate positive that happens to be a Single is unreachable
for MusicIP and counts against it. Most mates are albums, so the penalty is small -- but this
is a DIRECTIONAL read, not a perfectly fair A/B.

Run:  python attune/eval/bakeoff_musicip.py --db mixer-ng/data/mixer.db
"""
import argparse
import importlib.util
import os
import random

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


H = _load("harness", os.path.join(HERE, "harness.py"))
ME = _load("musicip_engine", os.path.join(SRC, "musicip_engine.py"))
HY = _load("hybrid", os.path.join(SRC, "hybrid.py"))


def musicip_rank_array(eng, si, mip, recon, mip_by_relkey, size):
    """Return (order, ranks) over our pool from MusicIP's mix for seed index si, or None if
    MusicIP doesn't know the seed / the call fails."""
    seed_unc = mip_by_relkey.get(ME.relkey(eng.paths[si]))
    if seed_unc is None:
        return None
    try:
        mix = mip.similar(seed_unc, size=size)
    except Exception:
        return None
    seen, kept = set(), []
    for p in mix:
        idx = recon.get(ME.relkey(p))
        if idx is not None and idx != si and idx not in seen:
            seen.add(idx)
            kept.append(idx)
    ranks = np.empty(len(eng.paths), dtype=np.int64)
    ranks[:] = len(kept) + 1                       # everything MusicIP didn't return ties last
    if kept:
        ranks[np.array(kept, dtype=np.int64)] = np.arange(1, len(kept) + 1)
    return np.array(kept, dtype=np.int64), ranks


def _row_from_ranks(eng, seed, order, ranks, album_pos, artist_pos, dupe_pos, k, negs):
    """Mirror of harness.evaluate()'s per-seed row logic, but fed a precomputed ranking so it
    works for either engine."""
    top_k_idx = set(order[:k].tolist())
    a_paths, r_paths, d_paths = album_pos.get(seed, set()), artist_pos.get(seed, set()), dupe_pos.get(seed, set())
    rel_idx = {eng.idx[p] for p in (a_paths | r_paths | d_paths) if p in eng.idx}
    if not rel_idx:
        return None
    hits = rel_idx & top_k_idx
    best_rank = min(int(ranks[i]) for i in rel_idx)
    row = {"seed": H._disp(seed), "precision_at_k": len(hits) / k, "rr": 1.0 / best_rank,
           "best_rank": best_rank}
    if a_paths:
        a_idx = {eng.idx[p] for p in a_paths if p in eng.idx}
        if a_idx:
            row["album_precision_at_k"] = len(a_idx & top_k_idx) / k
            row["album_rr"] = 1.0 / min(int(ranks[i]) for i in a_idx)
    if r_paths:
        r_idx = {eng.idx[p] for p in r_paths if p in eng.idx}
        if r_idx:
            row["artist_precision_at_k"] = len(r_idx & top_k_idx) / k
            row["artist_rr"] = 1.0 / min(int(ranks[i]) for i in r_idx)
    if d_paths:
        d_idx = {eng.idx[p] for p in d_paths if p in eng.idx}
        if d_idx:
            d_rank = min(int(ranks[i]) for i in d_idx)
            row["dupe_rank"] = d_rank
            row["dupe_pass"] = (d_rank == 1)
    if negs:
        row["n_neg"] = len(negs)
        row["neg_in_topk"] = sum(1 for j in negs if j in top_k_idx)
        row["neg_mean_percentile"] = float(np.mean([int(ranks[j]) for j in negs])) / max(1, len(order))
    return row


def evaluate_engine(kind, eng, seeds, labels, k, neg_per_seed, rng, mip=None, recon=None,
                    mip_by_relkey=None, size=100):
    album_pos, artist_pos, dupe_pos = labels
    rows = []
    for seed in seeds:
        si = eng.idx[seed]
        if kind == "clap":
            order, ranks = H.mix_rank_array(eng, si)
        else:
            r = musicip_rank_array(eng, si, mip, recon, mip_by_relkey, size)
            if r is None:
                continue
            order, ranks = r
        negs = H.sample_negatives(eng, si, neg_per_seed, rng)
        row = _row_from_ranks(eng, seed, order, ranks, album_pos, artist_pos, dupe_pos, k, negs)
        if row:
            rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser(description="MusicIP vs CLAP bake-off on the eval harness")
    ap.add_argument("--db", required=True)
    ap.add_argument("--url", default="http://localhost:10002")
    ap.add_argument("--seeds", type=int, default=300)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--size", type=int, default=100, help="MusicIP mix depth to rank over")
    args = ap.parse_args()
    rng = random.Random(42)

    print("loading our engine (CLAP/V2) ...")
    eng = HY.HybridEngine(args.db)
    print("building labels from our pool ...")
    album_pos, artist_pos, n_alb = H.bootstrap_album_artist_positives(eng, rng=rng)
    dupe_pos, scan_n = H.find_clap_dupes(eng, rng=rng)
    labels = (album_pos, artist_pos, dupe_pos)

    print("connecting to MusicIP ...")
    mip = ME.MusicIPEngine(args.url)
    if not mip.alive():
        raise SystemExit("MusicIP not responding on " + args.url)
    mip_songs = mip.songs()
    mip_by_relkey = {ME.relkey(p): p for p in mip_songs}
    recon = ME.build_reconciler(eng.paths)

    # seeds MusicIP actually knows (fair set), that also have at least one label
    labelled = set(album_pos) | set(artist_pos) | set(dupe_pos)
    candidates = [p for p in labelled if ME.relkey(p) in mip_by_relkey]
    rng.shuffle(candidates)
    seeds = candidates[:args.seeds]
    print(f"MusicIP knows {len(mip_songs):,} tracks; {len(candidates):,} labelled seeds are shared; "
          f"scoring {len(seeds)} seeds (k={args.k}, MusicIP mix depth={args.size}).\n")

    clap_rows = evaluate_engine("clap", eng, seeds, labels, args.k, 5, random.Random(42))
    mip_rows = evaluate_engine("musicip", eng, seeds, labels, args.k, 5, random.Random(42),
                               mip=mip, recon=recon, mip_by_relkey=mip_by_relkey, size=args.size)
    clap = H.summarize(clap_rows, args.k)
    mus = H.summarize(mip_rows, args.k)

    def g(d, key):
        v = d.get(key)
        return f"{v:.4f}" if isinstance(v, float) else str(v)

    print(f"{'metric':<26}{'CLAP / V2':>14}{'MusicIP':>14}")
    for key in ("n_seeds_evaluated", "precision_at_k", "mrr",
                "album_precision_at_k", "artist_precision_at_k", "dupe_pass_rate"):
        print(f"{key:<26}{g(clap, key):>14}{g(mus, key):>14}")
    print("\nNote: MusicIP is limited to the ~17.4k Albums; Single positives it cannot reach "
          "count against it. Directional read — confirm the winner by ear before defaulting.")


if __name__ == "__main__":
    main()
