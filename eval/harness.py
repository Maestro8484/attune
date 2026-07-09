"""Recommendation-quality eval harness for Attune's mixing engines.

Purpose: let engine changes (weights, new terms, a swapped embedding, etc.) be checked
for regressions WITHOUT a human listening session. It bootstraps labelled pairs
straight from the library's own structure -- no manual annotation:

  * same-(artist,album) mates        -> WEAK positive  (could be a boxset/comp, not a
                                          sonic match -- hence "weak")
  * same-artist, different album     -> WEAK positive  (same artist's records can sound
                                          very different across eras)
  * CLAP cosine >= 0.999             -> GUARANTEED positive (a near-duplicate -- e.g. a
                                          remaster/intro/alt-version of literally the same
                                          recording -- MUST be the #1 recommendation, or
                                          the ears are broken)
  * random cross-genre track         -> STRONG negative (disjoint genre tag sets)

It then imports HybridEngine from attune/src/hybrid.py (read-only, unmodified) and scores
it against these labels, reporting:
  * precision@k       -- of the engine's top-k recs, what fraction are known positives?
  * MRR               -- mean reciprocal rank of the best-ranked known positive
  * dupe-rank check   -- for seeds with a >=0.999 CLAP twin, is that twin engine-rank #1?
  * negative sanity   -- do the strong (cross-genre) negatives land in the BOTTOM of the
                          ranking, and how often do they leak into the top-k?

This script only READS mixer.db (never writes) and never modifies hybrid.py -- it just
imports HybridEngine. Everything it writes lives under attune/eval/.

Usage:
    python attune/eval/harness.py --db path/to/mixer.db
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hybrid import HybridEngine  # noqa: E402  (path shimmed above)

# Album names that mean "no real album" (untagged/loose singles bucket, live comp
# grab-bags, etc.) -- grouping by these would create FALSE same-album positives.
_ALBUM_PLACEHOLDER_SUBSTRINGS = (
    "singles & loose tracks", "loose tracks", "b-sides", "b sides",
    "bootleg", "unknown album", "various artists", "compilation",
)


def _is_placeholder_album(album: str) -> bool:
    a = album.lower()
    return any(sub in a for sub in _ALBUM_PLACEHOLDER_SUBSTRINGS)


# ---------------------------------------------------------------------------
# Label bootstrap
# ---------------------------------------------------------------------------

def bootstrap_album_artist_positives(eng, album_min=2, album_max=25, artist_pos_cap=20, rng=None):
    """Group the engine's own pool (paths it can actually score) by (artist,album)
    and by artist. Returns (album_pos, artist_pos): path -> set(other paths)."""
    rng = rng or random.Random(0)
    by_album = defaultdict(list)
    by_artist = defaultdict(list)
    for p in eng.paths:
        m = eng.meta.get(p, {})
        artist = (m.get("artist") or "").strip()
        album = (m.get("album") or "").strip()
        if artist:
            by_artist[artist].append(p)
        if artist and album and not _is_placeholder_album(album):
            by_album[(artist, album)].append(p)

    album_pos = {}
    n_album_groups = 0
    for group in by_album.values():
        if not (album_min <= len(group) <= album_max):
            continue
        n_album_groups += 1
        gs = set(group)
        for p in group:
            album_pos[p] = gs - {p}

    artist_pos = {}
    for group in by_artist.values():
        if len(group) < 2:
            continue
        for p in group:
            mates = [q for q in group if q != p]
            if len(mates) > artist_pos_cap:
                mates = rng.sample(mates, artist_pos_cap)
            artist_pos[p] = set(mates)

    return album_pos, artist_pos, n_album_groups


def find_clap_dupes(eng, threshold=0.999, scan_sample=8000, chunk=1500, rng=None):
    """Chunked cosine search over the engine's already-L2-normalized CLAP matrix.
    Returns dupe_pos: path -> set(near-duplicate paths), and the actual scan size used."""
    rng = rng or random.Random(0)
    n = len(eng.paths)
    scan_n = min(scan_sample, n)
    idxs = rng.sample(range(n), scan_n) if scan_n < n else list(range(n))
    dupes = defaultdict(set)
    for start in range(0, len(idxs), chunk):
        batch = idxs[start:start + chunk]
        sims = eng.X[batch] @ eng.X.T  # (len(batch), n_pool)
        for bi, gi in enumerate(batch):
            row = sims[bi].copy()
            row[gi] = -1.0  # exclude self
            hits = np.where(row >= threshold)[0]
            if hits.size:
                p = eng.paths[gi]
                for h in hits:
                    dupes[p].add(eng.paths[int(h)])
    return dict(dupes), scan_n


def sample_negatives(eng, seed_idx, k, rng, max_tries=300):
    """Sample up to k cross-genre STRONG negatives for a seed: pool tracks whose genre
    tag set is non-empty and disjoint from the seed's."""
    seed_tags = eng.genre_tags[seed_idx]
    if not seed_tags:
        return []
    n = len(eng.paths)
    out, seen, tries = [], set(), 0
    while len(out) < k and tries < max_tries:
        tries += 1
        j = rng.randrange(n)
        if j == seed_idx or j in seen:
            continue
        seen.add(j)
        tg = eng.genre_tags[j]
        if tg and not (tg & seed_tags):
            out.append(j)
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def rank_array(eng, si):
    """Full descending rank (1 = best) over the pool for seed index si, excluding si."""
    scores = eng._score(si)
    order = np.argsort(-scores)
    order = order[order != si]
    ranks = np.empty(len(eng.paths), dtype=np.int64)
    ranks[order] = np.arange(1, len(order) + 1)
    return order, ranks


def _disp(path):
    """Basename only -- reports/logs must never contain real library filesystem paths
    (drive letters / NAS shares), only enough of the filename to identify the track."""
    return os.path.basename(path) if path else path


def evaluate(eng, seeds, album_pos, artist_pos, dupe_pos, k, neg_per_seed, rng):
    rows = []
    for seed in seeds:
        si = eng.idx[seed]
        order, ranks = rank_array(eng, si)
        top_k_idx = set(order[:k].tolist())

        a_paths = album_pos.get(seed, set())
        r_paths = artist_pos.get(seed, set())
        d_paths = dupe_pos.get(seed, set())
        rel_paths = a_paths | r_paths | d_paths
        rel_idx = {eng.idx[p] for p in rel_paths if p in eng.idx}
        if not rel_idx:
            continue

        hits = rel_idx & top_k_idx
        precision_at_k = len(hits) / k
        best_rank = min(int(ranks[i]) for i in rel_idx)
        rr = 1.0 / best_rank

        row = {
            "seed": _disp(seed),
            "n_relevant": len(rel_idx),
            "precision_at_k": precision_at_k,
            "rr": rr,
            "best_rank": best_rank,
            "has_album": bool(a_paths),
            "has_artist": bool(r_paths),
            "has_dupe": bool(d_paths),
        }

        # Per-signal breakdowns. NOTE: album-mates are BY CONSTRUCTION always also
        # artist-mates (both come from the same (artist,album) grouping key), so these
        # two are not mutually exclusive categories -- they answer different questions:
        # "does the engine find same-album mates specifically" vs "same-artist at all".
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

        negs = sample_negatives(eng, si, neg_per_seed, rng)
        if negs:
            neg_ranks = [int(ranks[j]) for j in negs]
            row["neg_mean_percentile"] = float(np.mean(neg_ranks)) / len(order)
            row["neg_in_topk"] = sum(1 for j in negs if j in top_k_idx)
            row["n_neg"] = len(negs)

        rows.append(row)
    return rows


def summarize(rows, k):
    def mean(key, pred=lambda r: True):
        vals = [r[key] for r in rows if pred(r) and key in r]
        return (sum(vals) / len(vals), len(vals)) if vals else (float("nan"), 0)

    out = {}
    out["n_seeds_evaluated"] = len(rows)
    out["precision_at_k"], out["precision_at_k_n"] = mean("precision_at_k")
    out["mrr"], out["mrr_n"] = mean("rr")

    # Per-signal precision/MRR (NOT mutually exclusive -- see comment in evaluate()):
    # "album" = does the engine surface OTHER TRACKS FROM THE SAME (artist,album) near
    # the top; "artist" = same, but for same-artist (any album) mates.
    out["album_precision_at_k"], out["album_n"] = mean("album_precision_at_k")
    out["album_mrr"], _ = mean("album_rr")
    out["artist_precision_at_k"], out["artist_n"] = mean("artist_precision_at_k")
    out["artist_mrr"], _ = mean("artist_rr")

    dupe_rows = [r for r in rows if "dupe_pass" in r]
    out["dupe_seeds_tested"] = len(dupe_rows)
    out["dupe_pass_rate"] = (sum(1 for r in dupe_rows if r["dupe_pass"]) / len(dupe_rows)
                              if dupe_rows else float("nan"))
    # note: r["seed"] is already a basename (see _disp) -- no real filesystem paths here.
    out["dupe_failures"] = [{"seed": r["seed"], "rank": r["dupe_rank"]}
                             for r in dupe_rows if not r["dupe_pass"]][:10]

    neg_rows = [r for r in rows if "neg_mean_percentile" in r]
    out["neg_mean_percentile"], out["neg_n"] = mean("neg_mean_percentile")
    total_neg = sum(r["n_neg"] for r in neg_rows) or 1
    out["neg_topk_leak_rate"] = sum(r["neg_in_topk"] for r in neg_rows) / total_neg
    return out


def main():
    ap = argparse.ArgumentParser(description="Attune recommendation-quality eval harness")
    ap.add_argument("--db", required=True, help="path to mixer.db (read-only)")
    ap.add_argument("--k", type=int, default=10, help="top-k for precision@k")
    ap.add_argument("--n-seeds", type=int, default=900,
                     help="max eval seeds to sample from the labelled candidate pool "
                          "(at most 1/3 of this budget goes to dupe-guaranteed seeds, so "
                          "album/artist-only breakdowns still get their own seeds)")
    ap.add_argument("--neg-per-seed", type=int, default=8,
                     help="strong (cross-genre) negatives sampled per seed")
    ap.add_argument("--dupe-scan-sample", type=int, default=8000,
                     help="rows scanned for CLAP near-duplicates (>= this many pool tracks "
                          "get randomly sampled as query rows for speed)")
    ap.add_argument("--rng-seed", type=int, default=42)
    ap.add_argument("--out", default=None,
                     help="optional JSON report path (default: attune/eval/report_<ts>.json)")
    args = ap.parse_args()

    rng = random.Random(args.rng_seed)
    t0 = time.time()

    print(f"[harness] loading HybridEngine from {args.db} ...")
    eng = HybridEngine(args.db)
    n_pool = len(eng.paths)
    print(f"[harness] engine pool (clap{'+librosa' if eng.use_lib else ''}): {n_pool} tracks, "
          f"weights={eng.w}")

    print("[harness] bootstrapping same-album / same-artist weak positives ...")
    album_pos, artist_pos, n_album_groups = bootstrap_album_artist_positives(eng, rng=rng)
    print(f"[harness]   album-mate labels: {len(album_pos)} tracks "
          f"(from {n_album_groups} (artist,album) groups)")
    print(f"[harness]   artist-mate labels: {len(artist_pos)} tracks")

    print(f"[harness] scanning for CLAP near-duplicates (cosine >= 0.999) ...")
    dupe_pos, dupe_scan_n = find_clap_dupes(eng, scan_sample=args.dupe_scan_sample, rng=rng)
    print(f"[harness]   scanned {dupe_scan_n}/{n_pool} pool tracks as query rows "
          f"(sampled for speed); found {len(dupe_pos)} tracks with a >=0.999 twin")
    # Metadata sanity check on the dupe LABEL itself: a genuine same-recording duplicate
    # (remaster / compilation reappearance) should share the artist tag. A >=0.999-cosine
    # pair that does NOT share an artist tag is likely a CLAP embedding collision (two
    # acoustically-similar-but-different recordings), not a true duplicate -- so the
    # dupe-rank-check below is partly measuring label noise, not just engine quality.
    agree, total = 0, 0
    for seed, mates in dupe_pos.items():
        sa = (eng.meta.get(seed, {}).get("artist") or "").strip().lower()
        for m in mates:
            total += 1
            ma = (eng.meta.get(m, {}).get("artist") or "").strip().lower()
            if sa and sa == ma:
                agree += 1
    dupe_artist_agree_rate = (agree / total) if total else float("nan")
    print(f"[harness]   dupe-label sanity: {agree}/{total} ({dupe_artist_agree_rate:.1%}) "
          f"of >=0.999 pairs share an artist tag (rest may be embedding collisions, "
          f"not true duplicates)")

    candidates = sorted(set(album_pos) | set(artist_pos) | set(dupe_pos))
    print(f"[harness] labelled candidate seeds available: {len(candidates)}")

    # Balance the sample: dupe-guaranteed seeds are the rarer, higher-value check, but if
    # there happen to be a lot of them we don't want them to crowd out the album/artist-only
    # breakdown (which needs its OWN seeds, i.e. candidates that are NOT also a dupe-seed).
    # Reserve at most 1/3 of the budget for dupes; spend the rest on weak-positive-only seeds.
    all_dupe_seeds = list(dupe_pos.keys())
    dupe_take = min(len(all_dupe_seeds), max(1, args.n_seeds // 3))
    dupe_seeds = (rng.sample(all_dupe_seeds, dupe_take)
                  if dupe_take < len(all_dupe_seeds) else all_dupe_seeds)
    rest = [p for p in candidates if p not in dupe_pos]
    budget = max(args.n_seeds - len(dupe_seeds), 0)
    sampled_rest = rng.sample(rest, min(budget, len(rest)))
    seeds = dupe_seeds + sampled_rest
    print(f"[harness] SAMPLED {len(seeds)} eval seeds for speed "
          f"({len(dupe_seeds)} dupe-guaranteed [of {len(all_dupe_seeds)} found] "
          f"+ {len(sampled_rest)} random weak-positive-only) out of {len(candidates)} candidates")

    print(f"[harness] scoring HybridEngine (k={args.k}, {args.neg_per_seed} neg/seed) ...")
    rows = evaluate(eng, seeds, album_pos, artist_pos, dupe_pos, args.k, args.neg_per_seed, rng)
    summary = summarize(rows, args.k)
    summary["k"] = args.k
    summary["n_pool"] = n_pool
    summary["weights"] = eng.w
    summary["elapsed_sec"] = round(time.time() - t0, 1)
    summary["dupe_pairs_found"] = len(dupe_pos)
    summary["dupe_artist_agree_rate"] = dupe_artist_agree_rate
    summary["dupe_artist_agree_n"] = total

    print("\n" + "=" * 60)
    print("ATTUNE HYBRIDENGINE - EVAL RESULTS")
    print("=" * 60)
    print(f"pool size (scoreable tracks):     {n_pool}")
    print(f"engine weights:                   {eng.w}")
    print(f"eval seeds scored:                {summary['n_seeds_evaluated']} "
          f"(sampled from {len(candidates)} candidates)")
    print(f"dupe near-dup pairs found:        {len(dupe_pos)} "
          f"(scanned {dupe_scan_n}/{n_pool} rows)")
    print("-" * 60)
    print(f"precision@{args.k} (overall):        "
          f"{summary['precision_at_k']:.4f}   (n={summary['precision_at_k_n']})")
    print(f"MRR (overall):                    "
          f"{summary['mrr']:.4f}   (n={summary['mrr_n']})")
    print(f"  [signal] album-mate  precision@{args.k}: "
          f"{summary['album_precision_at_k']:.4f}  MRR: {summary['album_mrr']:.4f}"
          f"   (n={summary['album_n']})")
    print(f"  [signal] artist-mate precision@{args.k}: "
          f"{summary['artist_precision_at_k']:.4f}  MRR: {summary['artist_mrr']:.4f}"
          f"   (n={summary['artist_n']})")
    print("-" * 60)
    print(f"DUPE-RANK CHECK (must be #1):     "
          f"{summary['dupe_pass_rate']:.4f} pass rate  "
          f"({summary['dupe_seeds_tested']} seeds tested)")
    print(f"  caveat: only {summary['dupe_artist_agree_rate']:.1%} of the >=0.999-cosine "
          f"pairs found share an artist tag (n={summary['dupe_artist_agree_n']}) -- the rest "
          f"may be CLAP embedding collisions rather than true duplicates, so some of the "
          f"'failures' below are arguably correct (a false dupe SHOULDN'T rank #1).")
    if summary["dupe_failures"]:
        print(f"  failures (seed -> actual rank, up to 10 shown):")
        for f in summary["dupe_failures"]:
            print(f"    {f['seed']}  -> rank {f['rank']}")
    print("-" * 60)
    print(f"strong-negative mean rank percentile (want close to 1.0 = bottom): "
          f"{summary['neg_mean_percentile']:.4f}  (n_seeds={summary['neg_n']})")
    print(f"strong-negative top-{args.k} leak rate (want close to 0.0):        "
          f"{summary['neg_topk_leak_rate']:.4f}")
    print("-" * 60)
    print(f"elapsed: {summary['elapsed_sec']}s")
    print("=" * 60)

    out_path = args.out or os.path.join(_HERE, f"report_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(out_path, "w") as f:
        json.dump({"summary": summary, "rows": rows}, f, indent=2)
    print(f"\n[harness] full report written to {out_path}")


if __name__ == "__main__":
    main()
