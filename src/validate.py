"""Validation harness: does the modern engine approximate MusicIP?

For each ground-truth deep ranking (style=40, variety=0, 100 tracks), we:
  - restrict to the intersection of tracks that BOTH MusicIP ranked and we analyzed
  - compare our nearest-neighbour ordering of the seed against MusicIP's
Metrics:
  overlap@K  : |our top-K ∩ MusicIP top-K| / K   (set agreement)
  recall@K   : of MusicIP's top-K that we analyzed, how many are in our top-K
  spearman   : rank correlation on the common set (ordering agreement)

Because we can only rank tracks we've analyzed, coverage matters: we report how much
of each MusicIP ranking we actually had features for. Low coverage caveats a low score.
"""
from __future__ import annotations
import os, json, glob, numpy as np
from mixer import build_engine

HERE = os.path.dirname(os.path.abspath(__file__))
# Ground truth is OPTIONAL developer tooling: a folder of MusicIP mix captures used to
# score fidelity against the original engine. Public users won't have this. Override
# with --gt or the ATTUNE_GT env var. See docs/VALIDATION.md.
GT = os.environ.get("ATTUNE_GT", os.path.join(HERE, "..", "data", "groundtruth"))


def spearman(a, b):
    if len(a) < 3:
        return float("nan")
    ra = np.argsort(np.argsort(a))
    rb = np.argsort(np.argsort(b))
    ra = ra - ra.mean(); rb = rb - rb.mean()
    denom = np.sqrt((ra**2).sum() * (rb**2).sum())
    return float((ra*rb).sum()/denom) if denom else float("nan")


def evaluate(db_path, k=25, verbose=True):
    eng = build_engine(db_path)
    analyzed = set(eng.paths)
    files = sorted(glob.glob(os.path.join(GT, "*_deep_s40_v0.json")))
    rows = []
    for fp in files:
        gt = json.load(open(fp, encoding="utf-8"))
        seed = gt["seed"]
        if seed not in analyzed:
            continue
        mip = [t for t in gt["tracks"] if t != seed]          # MusicIP order
        mip_analyzed = [t for t in mip if t in analyzed]      # what we can score
        if len(mip_analyzed) < 5:
            continue
        our_ranked = [p for p, _ in eng.rank(seed, style=40.0)]
        our_pos = {p: i for i, p in enumerate(our_ranked)}

        mip_topk = set(mip[:k])
        our_topk = set(our_ranked[:k])
        overlap = len(mip_topk & our_topk) / k
        mip_topk_analyzed = [t for t in mip[:k] if t in analyzed]
        recall = (len(set(mip_topk_analyzed) & our_topk) / len(mip_topk_analyzed)
                  if mip_topk_analyzed else float("nan"))

        common = [t for t in mip_analyzed if t in our_pos]
        mip_rank = list(range(len(common)))
        our_rank = [our_pos[t] for t in common]
        rho = spearman(np.array(mip_rank), np.array(our_rank))

        coverage = len(mip_analyzed) / len(mip) if mip else 0
        rows.append({"seed": seed, "genre": gt.get("seed_meta", {}).get("genre"),
                     "overlap@k": overlap, "recall@k": recall, "spearman": rho,
                     "coverage": coverage, "n_common": len(common)})

    if not rows:
        print("No evaluable seeds — analyze more of the ground-truth tracks first.")
        print("Tip: run scan.py analyze --paths-file <gt track list>")
        return None

    def mean(key):
        vals = [r[key] for r in rows if r[key] == r[key]]  # drop nan
        return sum(vals)/len(vals) if vals else float("nan")

    summary = {
        "seeds_evaluated": len(rows),
        "mean_overlap@k": mean("overlap@k"),
        "mean_recall@k": mean("recall@k"),
        "mean_spearman": mean("spearman"),
        "mean_coverage": mean("coverage"),
        "k": k,
    }
    if verbose:
        print(f"=== Validation vs MusicIP (K={k}) ===")
        print(f"seeds evaluated : {summary['seeds_evaluated']}")
        print(f"mean overlap@{k}  : {summary['mean_overlap@k']:.3f}  "
              f"(fraction of MusicIP's top-{k} we also rank top-{k})")
        print(f"mean recall@{k}   : {summary['mean_recall@k']:.3f}  "
              f"(of MusicIP top-{k} we analyzed)")
        print(f"mean spearman    : {summary['mean_spearman']:.3f}  (rank corr on common set)")
        print(f"mean coverage    : {summary['mean_coverage']:.3f}  "
              f"(fraction of each MusicIP ranking we had features for)")
        print()
        rows.sort(key=lambda r: -r["overlap@k"])
        print("best / worst seeds by overlap:")
        for r in rows[:3] + rows[-3:]:
            print(f"  {r['overlap@k']:.2f} ov  {r['spearman']:.2f} rho  "
                  f"cov={r['coverage']:.2f}  [{r['genre']}] {os.path.basename(r['seed'])}")
    out = os.path.join(HERE, "..", "data", "validation_report.json")
    json.dump({"summary": summary, "rows": rows}, open(out, "w", encoding="utf-8"), indent=1)
    print(f"\nwrote {os.path.abspath(out)}")
    return summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, "..", "data", "mixer.db"))
    ap.add_argument("--k", type=int, default=25)
    ap.add_argument("--gt", help="ground-truth dir (default: $ATTUNE_GT or ../data/groundtruth)")
    a = ap.parse_args()
    if a.gt:
        GT = a.gt
    if not os.path.isdir(GT):
        raise SystemExit(
            f"ground-truth dir not found: {GT}\n"
            "Validation compares Attune against captured MusicIP mixes and is optional "
            "developer tooling. See docs/VALIDATION.md to generate your own.")
    evaluate(a.db, a.k)
