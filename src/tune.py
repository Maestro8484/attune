"""Tuning search: find the feature weighting + metric that best reproduces MusicIP's
deep rankings. Loads the analyzed matrix once, then scores many configs in-memory.

Reports mean overlap@25 and Spearman per config so we can pick weights for mixer.py.
"""
import os, json, glob, itertools, numpy as np
import db as dbm
from features import FEATURE_GROUPS, FEATURE_DIM

HERE = os.path.dirname(os.path.abspath(__file__))
GT = os.environ.get("ATTUNE_GT", os.path.join(HERE, "..", "..", "extracted", "groundtruth"))
DB = os.path.join(HERE, "..", "..", "mixer-ng", "data", "mixer.db")
K = 25


def load():
    conn = dbm.connect(DB)
    paths, mat, meta = dbm.load_matrix(conn)
    idx = {p: i for i, p in enumerate(paths)}
    return paths, np.asarray(mat, np.float64), meta, idx


def group_weight_vec(weights):
    w = np.ones(FEATURE_DIM)
    for g, (a, b) in FEATURE_GROUPS.items():
        w[a:b] = weights.get(g, 1.0)
    return w


def spearman(a, b):
    if len(a) < 3:
        return float("nan")
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    ra -= ra.mean(); rb -= rb.mean()
    d = np.sqrt((ra**2).sum()*(rb**2).sum())
    return float((ra*rb).sum()/d) if d else float("nan")


def evaluate(Z, idx, paths, gt_files, wvec, metric):
    Zw = Z * wvec
    if metric == "cosine":
        norm = np.linalg.norm(Zw, axis=1, keepdims=True)
        norm[norm < 1e-9] = 1e-9
        Zn = Zw / norm
    overlaps, rhos = [], []
    for gt in gt_files:
        seed = gt["seed"]
        si = idx.get(seed)
        if si is None:
            continue
        mip = [t for t in gt["tracks"] if t != seed and t in idx]
        if len(mip) < 5:
            continue
        if metric == "cosine":
            d = 1 - Zn @ Zn[si]
        else:
            d = np.sqrt(((Zw - Zw[si])**2).sum(axis=1))
        order = np.argsort(d)
        our = [paths[i] for i in order if i != si]
        our_pos = {p: r for r, p in enumerate(our)}
        overlaps.append(len(set(mip[:K]) & set(our[:K]))/K)
        common = [t for t in mip if t in our_pos]
        rhos.append(spearman(np.arange(len(common)), np.array([our_pos[t] for t in common])))
    mo = np.nanmean(overlaps) if overlaps else float("nan")
    mr = np.nanmean(rhos) if rhos else float("nan")
    return mo, mr, len(overlaps)


def main():
    paths, Z0, meta, idx = load()
    # standardize
    mu, sd = Z0.mean(0), Z0.std(0); sd[sd < 1e-9] = 1e-9
    Z = (Z0 - mu)/sd
    gt_files = [json.load(open(f, encoding="utf-8")) for f in sorted(glob.glob(os.path.join(GT, "*_deep_s40_v0.json")))]
    print(f"{len(paths)} analyzed tracks, {len(gt_files)} ground-truth seeds, K={K}\n")

    configs = []
    # timbre emphasis sweep
    for tw in (1, 2, 3, 5, 8):
        configs.append((f"timbre={tw} rest=1 notempo", {"timbre": tw, "tempo": 0.0}, "euclid"))
    # balanced variants
    configs += [
        ("all=1 euclid",            {}, "euclid"),
        ("all=1 cosine",            {}, "cosine"),
        ("timbre=3 harm=1.5 cos",   {"timbre": 3, "harmony": 1.5, "tempo": 0.2}, "cosine"),
        ("timbre=4 tex=1.5 notempo",{"timbre": 4, "texture": 1.5, "tempo": 0.0}, "euclid"),
        ("timbre=3 cosine notempo", {"timbre": 3, "tempo": 0.0}, "cosine"),
        ("timbre=5 harm=2 cos",     {"timbre": 5, "harmony": 2, "contrast": 1.5, "texture": 1.5, "tempo": 0.3}, "cosine"),
        ("timbre only",             {"timbre": 1, "harmony": 0, "contrast": 0, "texture": 0, "tempo": 0}, "euclid"),
        ("timbre+contrast only cos",{"timbre": 3, "harmony": 0.5, "contrast": 2, "texture": 1, "tempo": 0}, "cosine"),
    ]
    results = []
    for name, w, metric in configs:
        wvec = group_weight_vec(w)
        mo, mr, n = evaluate(Z, idx, paths, gt_files, wvec, metric)
        results.append((mo, mr, name, metric))
        print(f"  overlap@{K}={mo:.3f}  spearman={mr:.3f}  [{metric:7s}] {name}")
    results.sort(reverse=True)
    print(f"\nBEST: overlap@{K}={results[0][0]:.3f} spearman={results[0][1]:.3f} "
          f"[{results[0][3]}] {results[0][2]}")


if __name__ == "__main__":
    main()
