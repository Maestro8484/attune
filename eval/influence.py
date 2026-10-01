"""How much say each ingredient of the score has, per scoring version; and how badly a
version crowds the same few songs into every list ("hubs").

REPORTS ONLY. LAW 1: nothing here may pick what ships; the ear does that (eval/abtest.py).
LAW 3: every line carries the pool size it was measured over.

Two measurements, both over full top-K lists straight from _score() (no artist spacing,
so they describe the score and not the walk):

  influence  For each seed, take one ingredient out (weight 0) and count how many of the
             top K songs change. Median and range over the seeds. An ingredient that
             changes 9 of 100 is doing a tenth of the sorting; one that changes 60 is
             doing most of it.
  hubs       Over many random seeds, how many songs turn up in more than 2% of all the
             top-K lists, beside the count chance alone would give. Also the standard
             hubness figure, the skewness of how often each song is picked (Radovanovic
             et al. 2010): 0 is even, large and positive means a few songs soak up the
             lists.

The read is read-only (HybridEngine opens the database mode=ro).

    python eval/influence.py --db <mixer.db> [--key eval/abtest_key_*.json]
        [--versions v2,v2-fused,v2-feel,v2-noclap] [--grid] [--geometry] [--learned]
        [--feel-file path] [--out report.txt]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
for _p in (_SRC, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import variants as V   # noqa: E402

TERMS = ("clap", "lib", "genre", "bpm", "era", "feel")
K = 100
HUB_SEEDS = 200
HUB_SHARE = 0.02


def top_k(eng, si, k=K):
    s = eng._score(si).copy()
    s[si] = -1e9
    return np.argsort(-s)[:k]


def influence(eng, seed_idx, k=K):
    """{ingredient: [songs of k changed, per seed]} for every ingredient with a weight,
    plus 'fingerprint' = the fingerprint and everything derived from it (clap and feel)
    taken out together."""
    base = dict(eng.w)
    live = [t for t in TERMS if base.get(t)]
    groups = {t: (t,) for t in live}
    if base.get("feel") and base.get("clap"):
        groups["fingerprint"] = ("clap", "feel")
    out = {g: [] for g in groups}
    try:
        for si in seed_idx:
            eng.w = dict(base)
            ref = set(top_k(eng, si, k).tolist())
            for g, keys in groups.items():
                eng.w = dict(base)
                for key in keys:
                    eng.w[key] = 0.0
                out[g].append(k - len(ref & set(top_k(eng, si, k).tolist())))
    finally:
        eng.w = base
    return out


def hubs(top_fn, n, seed_idx, k=K):
    """top_fn(si) -> the k pool rows picked for seed si. Returns the hub figures."""
    occ = np.zeros(n, dtype=np.int64)
    for si in seed_idx:
        occ[np.asarray(top_fn(si))] += 1
    lists = len(seed_idx)
    line = HUB_SHARE * lists
    p = k / n
    by_chance = n * (1.0 - sum(math.comb(lists, j) * p ** j * (1 - p) ** (lists - j)
                               for j in range(int(math.floor(line)) + 1)))
    sd = occ.std()
    skew = float(((occ - occ.mean()) ** 3).mean() / sd ** 3) if sd > 0 else 0.0
    return {"hubs": int((occ > line).sum()), "by_chance": by_chance,
            "biggest": int(occ.max()), "lists": lists,
            "never": int((occ == 0).sum()), "never_by_chance": n * (1 - p) ** lists,
            "skew": skew}


def std_seeds(eng, key_path=None, n_random=12):
    """The 20 seeds every sonic measurement has used: the sealed key's seeds that are
    still in the pool, then 12 drawn with a fixed generator."""
    seeds = []
    if key_path and os.path.exists(key_path):
        key = json.load(open(key_path, encoding="utf-8"))
        seeds = [d["seed"] for d in key["tests"].values() if d["seed"] in eng.idx]
    rng = np.random.default_rng(20260930)
    seeds += [eng.paths[i] for i in rng.choice(len(eng.paths), n_random, replace=False)]
    return [eng.idx[p] for p in seeds]


def hub_seeds(n):
    return np.random.default_rng(20261001).choice(n, min(HUB_SEEDS, n), replace=False).tolist()


def _fmt_infl(res):
    return "  ".join(f"{t} {int(np.median(v))} ({min(v)}-{max(v)})" for t, v in res.items())


def _fmt_hubs(h):
    return (f"{h['hubs']} songs in more than {HUB_SHARE:.0%} of {h['lists']} lists "
            f"(chance alone: {h['by_chance']:.0f}); biggest hub in {h['biggest']} lists; "
            f"{h['never']} songs never picked (chance alone: {h['never_by_chance']:.0f}); "
            f"skew {h['skew']:.2f}")


def report_version(base, name, seeds, hseeds, out, feel_file=None, label=None, **override):
    eng = V.variant_engine(base, name, feel_file=feel_file, **override)
    n = len(eng.paths)
    lab = label or name
    w = "  ".join(f"{t} {eng.w[t]:g}" for t in TERMS if eng.w.get(t))
    out(f"\n== {lab}   fusion {eng.fusion}, fingerprint {eng.clap_space}, weights: {w}")
    res = influence(eng, seeds)
    out(f"   pool {n}, {len(seeds)} seeds, songs of {K} that change when one ingredient "
        f"is removed, median (range):")
    out(f"   {_fmt_infl(res)}")
    h = hubs(lambda si: top_k(eng, si), n, hseeds)
    out(f"   pool {n}, hubs: {_fmt_hubs(h)}")
    if eng.fusion == "z" and eng.w.get("genre"):
        zmax = []
        for si in seeds:
            for t, _wk, g in eng._terms(si):
                if t == "genre":
                    zmax.append(float(eng._fuse(g).max()))
        out(f"   genre ingredient under z: largest value per seed, median "
            f"{np.median(zmax):.1f}, max {max(zmax):.1f} (other ingredients stay within about 3)")
    return res, h


def geometry(base, seeds, hseeds, out):
    """Raw against centered against all-but-the-top, on the fingerprint alone."""
    n = len(base.paths)
    out(f"\n== the fingerprint alone, by space   pool {n}, {len(seeds)} seeds")
    out("   spread = how much fingerprint closeness varies among the 1,000 songs competing")
    out("   for a seed's mix under the shipped score (median over seeds), and across the pool;")
    out("   genre share = of the 100 closest by fingerprint alone, the share carrying one of")
    out("   the seed's genre tags (random songs for comparison); overlap = shared with raw's top 100")
    gen = base.genre_tags
    rng = np.random.default_rng(20261001)
    contenders = {}
    for si in seeds:
        s = base._score(si).copy(); s[si] = -1e9
        contenders[si] = np.argsort(-s)[:1000]
    raw_top = {}
    for sp in ("raw", "centered", "abtt1", "abtt2", "abtt3"):
        M = base._clap_matrix(sp)
        spread, spread_all, gshare, grand, ov = [], [], [], [], []
        for si in seeds:
            c = (M @ M[si]).astype(np.float64)
            spread.append(c[contenders[si]].std()); spread_all.append(np.delete(c, si).std())
            c[si] = -9
            top = np.argsort(-c)[:K]
            if sp == "raw":
                raw_top[si] = set(top.tolist())
            ov.append(len(raw_top[si] & set(top.tolist())))
            if gen[si]:
                gshare.append(np.mean([bool(gen[si] & gen[j]) for j in top]))
                rnd = rng.choice(n, K, replace=False)
                grand.append(np.mean([bool(gen[si] & gen[j]) for j in rnd]))

        def top_fn(si, M=M):
            c = M @ M[si]; c[si] = -9
            return np.argsort(-c)[:K]
        h = hubs(top_fn, n, hseeds)
        out(f"   {sp:9s} spread among contenders {np.median(spread):.4f} (whole pool "
            f"{np.median(spread_all):.4f}) | genre share {np.median(gshare):.2f} vs random "
            f"{np.median(grand):.2f} | overlap with raw {int(np.median(ov))} of {K}")
        out(f"   {'':9s} hubs: {_fmt_hubs(h)}")
        base._spaces.pop(sp, None)


def learned(base, db, seeds, hseeds, out):
    """The learned projection head, measured as it stands (it is not changed here)."""
    spec = importlib.util.spec_from_file_location("engine", os.path.join(_SRC, "engine.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["engine"] = mod
    spec.loader.exec_module(mod)
    le = mod.LearnedEngine(db, hybrid_engine=base)
    E, rows = le.E, le.rows
    row_of = le._row_of
    n = len(rows)
    out(f"\n== learned engine ({le.version})   pool {n}")
    s_in = [row_of[si] for si in seeds if si in row_of]
    spread, spread_all = [], []
    for r in s_in:
        c = (E @ E[r]).astype(np.float64)
        c2 = np.delete(c, r)
        spread_all.append(c2.std()); spread.append(np.sort(c2)[-1000:].std())
    out(f"   closeness spread among a seed's 1,000 nearest {np.median(spread):.4f}, across "
        f"the pool {np.median(spread_all):.4f} (median over {len(s_in)} seeds)")
    mu = E.astype(np.float64).mean(0)
    out(f"   length of the average projected fingerprint: {np.linalg.norm(mu):.3f} "
        f"(the stored fingerprints: {np.linalg.norm(base.X.astype(np.float64).mean(0)):.3f})")

    def top_fn(r):
        c = E @ E[r]; c[r] = -9
        return np.argsort(-c)[:K]
    hs = [row_of[si] for si in hseeds if si in row_of]
    out(f"   hubs: {_fmt_hubs(hubs(top_fn, n, hs))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--key", default=None, help="a sealed abtest key whose seeds lead the seed list")
    ap.add_argument("--versions", default="v2,v2-fused,v2-feel,v2-noclap")
    ap.add_argument("--grid", action="store_true",
                    help="every fusion mode against every fingerprint space, shipped weights")
    ap.add_argument("--geometry", action="store_true", help="the fingerprint alone, by space")
    ap.add_argument("--learned", action="store_true", help="the learned engine's hubs and spread")
    ap.add_argument("--feel-file", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    spec = importlib.util.spec_from_file_location("hybrid", os.path.join(_SRC, "hybrid.py"))
    hy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hy)
    base = hy.HybridEngine(a.db)
    lines = []

    def out(s):
        print(s); lines.append(s)

    n = len(base.paths)
    out(f"pool {n} (LAW 3). Reports only; the ear decides what ships (LAW 1).")
    seeds = std_seeds(base, a.key)
    hseeds = hub_seeds(n)
    for name in [v.strip() for v in a.versions.split(",") if v.strip()]:
        if name == "v2-feel" and base.feel is None:
            out(f"\n== {name}: skipped, no feel scores file")
            continue
        report_version(base, name, seeds, hseeds, out, feel_file=a.feel_file)
    if a.grid:
        for fu in hy.FUSIONS:
            for sp in hy.CLAP_SPACES:
                report_version(base, "v2", seeds, hseeds, out, label=f"grid {fu} x {sp}",
                               fusion=fu, clap_space=sp)
    if a.geometry:
        geometry(base, seeds, hseeds, out)
    if a.learned:
        learned(base, a.db, seeds, hseeds, out)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
