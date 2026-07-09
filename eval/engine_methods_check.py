"""Numeric proof-of-property check for the HybridEngine methods added for relevance
feedback / diversity / flow / explainability (src/hybrid.py): mix_from_vector(), refine(),
mmr(), order_by_flow(), explain(). Read-only: only SELECTs via HybridEngine's loader.

Machine-checkable properties asserted here (no taste/quality judgments):
  1. refine() with thumbs-up on same-artist/near-neighbour tracks raises the mean CLAP
     cosine similarity of the refined query to that track's own neighbourhood, vs the
     unrefined seed vector.
  2. mmr() lowers the mean pairwise CLAP cosine similarity among the chosen set vs a
     plain top-k (by relevance) drawn from the same candidate pool.
  3. order_by_flow() lowers the summed consecutive-pair CLAP distance (1 - cosine) of a
     track list vs its original (scrambled) order.
  4. explain() component contributions sum (within float tolerance) to the same score
     _score() computes for that (seed, candidate) pair.

  python engine_methods_check.py --db ../../mixer-ng/data/mixer.db
"""
from __future__ import annotations
import os, sys, argparse, random
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from hybrid import HybridEngine


def check_refine(eng, rng, n_trials=15):
    """Three Rocchio invariants, checked in isolation so the (expected, and confirmed
    below) high baseline cosine similarity of this CLAP space -- random track pairs
    average ~0.95, see the note in main() -- can't mask a real regression:

      A. thumbs-up ALONE on a track's near-neighbourhood raises the refined query's
         mean similarity to that neighbourhood vs the plain (unrefined) seed vector
         ("thumbs-up raises that track-neighbourhood score").
      B. thumbs-down ALONE on a distant cluster lowers the refined query's mean
         similarity to that cluster vs the plain seed vector.
      C. combined feedback ranks the liked cluster above the disliked cluster (the
         property that actually matters for re-ranking candidates).
    """
    n_a = n_b = n_c = 0
    rows = []
    for _ in range(n_trials):
        seed_i = rng.randrange(len(eng.paths))
        seed_p = eng.paths[seed_i]
        sims_to_seed = eng.X @ eng.X[seed_i]
        order = np.argsort(-sims_to_seed)
        liked = [int(j) for j in order[1:11]]     # closest 10 (excluding the seed itself)
        disliked = [int(j) for j in order[-10:]]  # farthest 10 in the whole pool
        q0 = eng.X[seed_i]

        q_liked_only = eng.refine(seed_p, liked_idx=liked, disliked_idx=[], alpha=1.0, beta=0.5)
        a_before = float(np.mean(eng.X[liked] @ q0))
        a_after = float(np.mean(eng.X[liked] @ q_liked_only))
        a_ok = a_after > a_before
        n_a += a_ok

        q_disliked_only = eng.refine(seed_p, liked_idx=[], disliked_idx=disliked, alpha=1.0, beta=0.5)
        b_before = float(np.mean(eng.X[disliked] @ q0))
        b_after = float(np.mean(eng.X[disliked] @ q_disliked_only))
        b_ok = b_after < b_before
        n_b += b_ok

        q_both = eng.refine(seed_p, liked_idx=liked, disliked_idx=disliked, alpha=1.0, beta=0.5)
        c_liked = float(np.mean(eng.X[liked] @ q_both))
        c_disliked = float(np.mean(eng.X[disliked] @ q_both))
        c_ok = c_liked > c_disliked
        n_c += c_ok

        rows.append({"seed": os.path.basename(seed_p),
                      "A_liked_before": a_before, "A_liked_after": a_after, "A_ok": a_ok,
                      "B_disliked_before": b_before, "B_disliked_after": b_after, "B_ok": b_ok,
                      "C_liked_sim": c_liked, "C_disliked_sim": c_disliked, "C_ok": c_ok})
    return {"rows": rows, "n_trials": n_trials, "n_a": n_a, "n_b": n_b, "n_c": n_c,
             "pass": n_a == n_trials and n_b == n_trials and n_c == n_trials}


def _greedy_spacing_walk(order, artist_of, skip, size, artist_spacing):
    """Mirrors mix()/mix_from_vector()'s own selection loop (rank order -> artist-spaced
    top-k), so a hand-rolled 'expected' ranking applies the identical spacing rule as the
    method under test -- an apples-to-apples comparison instead of a naive contiguous slice."""
    out, recent = [], []
    for j in order:
        j = int(j)
        if j == skip:
            continue
        art = artist_of[j]
        if art and art in recent[-artist_spacing:]:
            continue
        out.append(j); recent.append(art)
        if len(out) >= size:
            break
    return out


def check_mix_from_vector_consistency(eng, rng, n_trials=10, k=20, artist_spacing=3, overlap_thresh=0.85):
    """mix_from_vector() ranking by a track's own CLAP row should reproduce a pure-CLAP
    top-k ranking under the SAME artist-spacing rule (built by hand here via the identical
    greedy walk, so this isolates ranking correctness from spacing choices). Compared as
    set overlap rather than strict order equality: refine()/mmr()'s float64 promotion of
    the query vector vs _score()'s float32 path can swap the order of near-ties (this
    space's mean pairwise similarity is ~0.95, so near-ties are common) -- that is
    re-summation noise, not a ranking bug."""
    rows = []
    n_ok = 0
    for _ in range(n_trials):
        seed_i = rng.randrange(len(eng.paths))
        seed_p = eng.paths[seed_i]
        sims = eng.X @ eng.X[seed_i]
        expected_order = np.argsort(-sims)
        expected_idx = _greedy_spacing_walk(expected_order, eng.artist, seed_i, k, artist_spacing)
        expected = {eng.paths[j] for j in expected_idx}
        got = set(eng.mix_from_vector(eng.X[seed_i], size=k, artist_spacing=artist_spacing,
                                       exclude=[seed_p]))
        overlap = len(expected & got) / k
        ok = overlap >= overlap_thresh
        n_ok += ok
        rows.append({"seed": os.path.basename(seed_p), "top_k_overlap": overlap, "ok": ok})
    return {"rows": rows, "n_trials": n_trials, "n_ok": n_ok, "pass": n_ok == n_trials}


def mean_pairwise_sim(eng, paths):
    idxs = [eng.idx[p] for p in paths]
    if len(idxs) < 2:
        return 0.0
    Xc = eng.X[idxs]
    sim = Xc @ Xc.T
    n = len(idxs)
    iu = np.triu_indices(n, k=1)
    return float(np.mean(sim[iu]))


def check_mmr(eng, rng, n_trials=8, pool_size=60, k=15):
    results = []
    for _ in range(n_trials):
        seed_i = rng.randrange(len(eng.paths))
        seed_p = eng.paths[seed_i]
        sims = eng.X @ eng.X[seed_i]
        order = np.argsort(-sims)
        candidates = [eng.paths[j] for j in order if j != seed_i][:pool_size]

        plain_topk = candidates[:k]
        diverse = eng.mmr(candidates, k=k, lambda_=0.3, query=eng.X[seed_i])

        plain_sim = mean_pairwise_sim(eng, plain_topk)
        diverse_sim = mean_pairwise_sim(eng, diverse)
        results.append({
            "seed": os.path.basename(seed_p),
            "plain_topk_mean_pairwise_sim": plain_sim,
            "mmr_mean_pairwise_sim": diverse_sim,
            "improved": diverse_sim < plain_sim,
        })
    return results


def check_order_by_flow(eng, rng, n_trials=8, n_tracks=25):
    def consecutive_dist_sum(paths):
        idxs = [eng.idx[p] for p in paths]
        Xc = eng.X[idxs]
        total = 0.0
        for a, b in zip(idxs[:-1], idxs[1:]):
            total += 1.0 - float(eng.X[a] @ eng.X[b])
        return total

    results = []
    for _ in range(n_trials):
        sample_idx = rng.sample(range(len(eng.paths)), n_tracks)
        paths = [eng.paths[i] for i in sample_idx]
        scrambled = list(paths)
        rng.shuffle(scrambled)

        before = consecutive_dist_sum(scrambled)
        flowed = eng.order_by_flow(scrambled)
        after = consecutive_dist_sum(flowed)
        results.append({
            "before_sum_consecutive_dist": before,
            "after_sum_consecutive_dist": after,
            "improved": after < before,
            "same_set": sorted(flowed) == sorted(scrambled),
        })
    return results


def check_explain(eng, rng, n_trials=20, tol=1e-5):
    # tol=1e-5: the CLAP matrix is float32; _score() sums a vectorized (X @ X[si]) over
    # the whole pool while explain() computes the single pair directly, so the two take
    # a different float32 summation path to the same value -- differences run ~1e-7,
    # comfortably inside this tolerance. This is float rounding, not a formula mismatch.
    results = []
    for _ in range(n_trials):
        si = rng.randrange(len(eng.paths))
        ci = rng.randrange(len(eng.paths))
        expected = float(eng._score(si)[ci])
        comp = eng.explain(si, ci)
        diff = abs(comp["total"] - expected)
        results.append({"si": si, "ci": ci, "expected": expected,
                         "explain_total": comp["total"], "abs_diff": diff,
                         "ok": diff < tol})
    return results


def main():
    ap = argparse.ArgumentParser(description="Prove numeric properties of the new HybridEngine methods")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "..",
                                                   "mixer-ng", "data", "mixer.db"))
    ap.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    a = ap.parse_args()
    rng = random.Random(a.seed)

    print(f"loading HybridEngine from {a.db} ...")
    eng = HybridEngine(a.db)
    print(f"pool size: {len(eng.paths)} tracks (use_lib={eng.use_lib})\n")

    all_ok = True

    rng0 = np.random.default_rng(a.seed)
    idx_a = rng0.integers(0, len(eng.paths), 3000)
    idx_b = rng0.integers(0, len(eng.paths), 3000)
    baseline_sim = float(np.mean(np.sum(eng.X[idx_a] * eng.X[idx_b], axis=1)))
    print(f"(context: mean cosine similarity between {len(idx_a)} random track pairs in this "
          f"pool = {baseline_sim:.3f} -- CLAP space here is highly anisotropic, so refine()'s "
          f"thumbs-up/-down effects below are checked in isolation, not as one combined delta)\n")

    print("=== 1. refine() (Rocchio) -- thumbs-up/down move the query as expected ===")
    r = check_refine(eng, rng)
    for row in r["rows"]:
        print(f"  seed={row['seed']!r:40s} A(liked up)={row['A_ok']}"
              f"  B(disliked down)={row['B_ok']}  C(liked>disliked)={row['C_ok']}")
    print(f"  -> A: {r['n_a']}/{r['n_trials']} liked-only raised liked-sim")
    print(f"  -> B: {r['n_b']}/{r['n_trials']} disliked-only lowered disliked-sim")
    print(f"  -> C: {r['n_c']}/{r['n_trials']} combined feedback ranked liked above disliked")
    all_ok &= r["pass"]
    print(f"  -> {'PASS' if r['pass'] else 'FAIL'}\n")

    print("=== 1b. mix_from_vector() consistency check (own-CLAP-row ranking) ===")
    r2 = check_mix_from_vector_consistency(eng, rng)
    for row in r2["rows"]:
        print(f"  seed={row['seed']!r:40s} top-20 overlap with pure-CLAP ranking = {row['top_k_overlap']:.2f}")
    print(f"  -> {r2['n_ok']}/{r2['n_trials']} trials with >=85% top-20 overlap")
    all_ok &= r2["pass"]
    print(f"  -> {'PASS' if r2['pass'] else 'FAIL'}\n")

    print("=== 2. mmr() lowers mean pairwise CLAP similarity vs plain top-k ===")
    mmr_results = check_mmr(eng, rng)
    n_improved = sum(r["improved"] for r in mmr_results)
    for r in mmr_results:
        print(f"  seed={r['seed']!r:40s} plain={r['plain_topk_mean_pairwise_sim']:.4f}"
              f"  mmr={r['mmr_mean_pairwise_sim']:.4f}  improved={r['improved']}")
    print(f"  -> {n_improved}/{len(mmr_results)} trials improved (lower mmr similarity)")
    mmr_pass = n_improved == len(mmr_results)
    all_ok &= mmr_pass
    print(f"  -> {'PASS' if mmr_pass else 'FAIL'}\n")

    print("=== 3. order_by_flow() lowers summed consecutive-pair distance ===")
    flow_results = check_order_by_flow(eng, rng)
    n_improved = sum(r["improved"] for r in flow_results)
    same_set_ok = all(r["same_set"] for r in flow_results)
    for r in flow_results:
        print(f"  before={r['before_sum_consecutive_dist']:.4f}  after={r['after_sum_consecutive_dist']:.4f}"
              f"  improved={r['improved']}  same_set={r['same_set']}")
    print(f"  -> {n_improved}/{len(flow_results)} trials improved; same_set_preserved={same_set_ok}")
    flow_pass = n_improved == len(flow_results) and same_set_ok
    all_ok &= flow_pass
    print(f"  -> {'PASS' if flow_pass else 'FAIL'}\n")

    print("=== 4. explain() components sum to _score() ===")
    explain_results = check_explain(eng, rng)
    n_ok = sum(r["ok"] for r in explain_results)
    max_diff = max(r["abs_diff"] for r in explain_results)
    print(f"  -> {n_ok}/{len(explain_results)} pairs match within 1e-5 (max abs diff = {max_diff:.2e})")
    explain_pass = n_ok == len(explain_results)
    all_ok &= explain_pass
    print(f"  -> {'PASS' if explain_pass else 'FAIL'}\n")

    print("=" * 60)
    print(f"OVERALL: {'ALL PASS' if all_ok else 'SOME FAILED'}")
    raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
