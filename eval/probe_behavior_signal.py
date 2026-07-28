r"""Phase 1b -- is the mined behavioural signal actually TASTE?

build_behavior_pairs.py counts the signal. This script asks whether the signal means what
we want it to mean, because the pair count alone cannot tell the difference between
"playlists the operator built" and "playlists a similarity engine built for the operator".
Four controls, each falsifiable, each cheap:

  COHERENCE  Is a list more acoustically coherent than chance, and by how much compared to
             a list we KNOW an engine produced (our own ABTest sets)? Raw CLAP cosine has a
             ~0.95 floor between random tracks, so the statistic is a z-score of the list's
             mean pairwise cosine against equal-size random samples.

  C1 SEED-BLIND   Rank by "was this track in any training list", ignoring the seed entirely.
             If that reproduces the trained head's gain, the head learned membership, not
             similarity.

  C2 INDUCTIVE    Re-measure the trained head with the candidate pool stripped of every
             track that appeared in a training list. The honest generalisation number.

  C3 SHUFFLED     Train on the same lists with membership randomly permuted. Whatever gain
             appears here is what the protocol manufactures from NO signal, and must be
             subtracted from any gain claimed above.

  TEACHER    src/models/metric_head.onnx is the SHIPPED head: a distillation of MusicIP's
             similarity function, trained years of sessions ago on thousands of MusicIP
             rank lists, and it has never seen any of these playlists. If the playlists are
             personal taste it should do no better than CLAP on them. If it predicts them
             well for free, the playlists ARE MusicIP output and there is no personal signal
             in them to learn.

Every number is recall@k over the FULL pool, reported with pool size (LAW 3), and is a
diagnostic only -- nothing here may select what ships (LAW 1).

Runs under the ML venv (needs torch for the trained-head controls + onnxruntime for TEACHER).

usage:
  python eval/probe_behavior_signal.py --db ..\mixer-ng\data\mixer.db \
      --pairs eval/behavior_pairs.json --playlists "M:\_LAN-Playlists"
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import random
import sys

import numpy as np

if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


th = _load("train_behavior_head", os.path.join(HERE, "train_behavior_head.py"))
bp = _load("build_behavior_pairs", os.path.join(HERE, "build_behavior_pairs.py"))


class Cfg:
    """Mirror train_behavior_head's probe defaults exactly so the controls are comparable."""
    dim = 512; hidden = 4096; lr = 3e-4; tau = 0.05
    pos_k = 8; negs = 2048; batch = 256; epochs = 20; k = 25; max_seeds = 600


# --------------------------------------------------------------------------- coherence
def coherence(args, X, paths, by_relkey, idx):
    rng = np.random.default_rng(42)
    N = len(paths)

    def meanpair(rows):
        S = X[rows] @ X[rows].T
        n = len(rows)
        return float((S.sum() - np.trace(S)) / (n * (n - 1)))

    null = {}

    def z_of(rows):
        n = len(rows)
        if n not in null:
            vals = [meanpair(rng.choice(N, n, replace=False)) for _ in range(60)]
            null[n] = (float(np.mean(vals)), float(np.std(vals)) or 1e-9)
        mu, sd = null[n]
        return (meanpair(rows) - mu) / sd, mu, meanpair(rows)

    def resolve(f):
        entries, _, _ = bp.parse_playlist(f)
        out = []
        for e in entries:
            dbp = by_relkey.get(bp.relkey(e))
            if dbp and dbp in idx:
                out.append(idx[dbp])
        return out

    cand, excl = bp.find_playlists(args.playlists, include_bak=True)
    print("\n" + "=" * 76)
    print("COHERENCE -- is a list tighter than chance, vs a list we KNOW an engine made?")
    print("=" * 76)
    res = {}
    for tag, files in (("candidate (non-ABTest)", cand),
                       ("CONTROL: ABTest (known engine output)", excl)):
        zs, deltas, seen = [], [], set()
        for f in sorted(files):
            r = resolve(f)
            if len(r) < 12 or tuple(r) in seen:
                continue
            seen.add(tuple(r))
            z, mu, mp = z_of(r)
            zs.append(z)
            deltas.append(mp - mu)
        if zs:
            print(f"  {tag:42} n={len(zs):3}  mean z={np.mean(zs):5.1f}  "
                  f"mean (list - chance) cosine = {np.mean(deltas):+.4f}")
            res[tag] = {"n": len(zs), "mean_z": float(np.mean(zs)),
                        "mean_excess_cosine": float(np.mean(deltas))}
    print("  -> if the two rows show the same excess cosine, coherence does NOT distinguish\n"
          "     an operator-made list from an engine-made one.")
    return res


# --------------------------------------------------------------------------- controls
def controls(args, d, X, L_raw, dev):
    members = [lm["rows"] for lm in d["list_members"] if len(lm["rows"]) >= Cfg.pos_k + 1]
    names = [lm["file"] for lm in d["list_members"] if len(lm["rows"]) >= Cfg.pos_k + 1]
    N = X.shape[0]
    Lz, _, _ = th.zscore(L_raw)
    F_in = np.concatenate([X, Lz], axis=1).astype(np.float32)
    order = list(range(len(members)))
    random.Random(th.SEED).shuffle(order)
    folds = [order[i::args.folds] for i in range(args.folds)]

    print("\n" + "=" * 76)
    print(f"CONTROLS -- pool {N:,} tracks, random baseline recall@{Cfg.k} = {Cfg.k/N:.5f}")
    print("=" * 76)
    print(f"{'fold':>4} {'CLAP':>7} {'trained':>8} | {'C1 blind':>9} | "
          f"{'C2 pool':>9} {'C2 CLAP':>8} {'C2 head':>8} | {'C3 shuf':>8}")
    rows = []
    for fi, te in enumerate(folds):
        tr = [i for i in order if i not in te]
        tr_groups = [members[i] for i in tr]
        te_groups = [members[i] for i in te]
        seen = set()
        for g in tr_groups:
            seen.update(g)
        _, E, _ = th.train_head(F_in, tr_groups, [], Cfg, dev, log=lambda s: None)
        rb, _, _ = th.recall_at_k(X, te_groups, k=Cfg.k, max_seeds=Cfg.max_seeds)
        rl, _, _ = th.recall_at_k(E, te_groups, k=Cfg.k, max_seeds=Cfg.max_seeds)

        rng = np.random.default_rng(0)
        prior = rng.random(N) * 1e-6
        prior[sorted(seen)] += 1.0
        recs = []
        for g in te_groups:
            for s in g:
                tgt = [x for x in g if x != s]
                sc = prior.copy()
                sc[s] = -np.inf
                keff = min(Cfg.k, len(tgt))
                top = set(np.argpartition(-sc, keff - 1)[:keff].tolist())
                recs.append(len(top & set(tgt)) / keff)
        c1 = float(np.mean(recs)) if recs else 0.0

        mask = np.ones(N, bool)
        mask[sorted(seen)] = False
        c2b, _, pool2 = th.recall_at_k(X, te_groups, k=Cfg.k, pool_mask=mask,
                                       max_seeds=Cfg.max_seeds)
        c2l, _, _ = th.recall_at_k(E, te_groups, k=Cfg.k, pool_mask=mask, max_seeds=Cfg.max_seeds)

        rs_ = random.Random(100 + fi)
        universe = sorted(set().union(*[set(g) for g in tr_groups]))
        sh = [rs_.sample(universe, len(g)) for g in tr_groups]
        _, Esh, _ = th.train_head(F_in, sh, [], Cfg, dev, log=lambda s: None)
        c3, _, _ = th.recall_at_k(Esh, te_groups, k=Cfg.k, max_seeds=Cfg.max_seeds)

        print(f"{fi:>4} {rb:7.4f} {rl:8.4f} | {c1:9.4f} | {pool2:>9,} {c2b:8.4f} "
              f"{c2l:8.4f} | {c3:8.4f}")
        rows.append((rb, rl, c1, c2b, c2l, c3))

    m = np.mean(rows, axis=0)
    print(f"\n  MEAN  CLAP {m[0]:.4f}   trained {m[1]:.4f}   (naive delta {m[1]-m[0]:+.4f})")
    print(f"    C1 seed-blind membership prior alone      : {m[2]:.4f}")
    print(f"    C2 TRUE INDUCTIVE (training tracks removed): CLAP {m[3]:.4f} -> "
          f"head {m[4]:.4f}  ({m[4]-m[3]:+.4f})")
    print(f"    C3 SHUFFLED-LABEL null                    : {m[5]:.4f}  "
          f"({m[5]-m[0]:+.4f} over CLAP from NO signal)")
    print(f"    => signal-attributable gain = trained - shuffled = {m[1]-m[5]:+.4f}")
    return {"clap": m[0], "trained": m[1], "c1_seedblind": m[2], "c2_clap": m[3],
            "c2_head": m[4], "c3_shuffled": m[5],
            "signal_attributable": float(m[1] - m[5])}, members, names, order, folds, F_in


# --------------------------------------------------------------------------- teacher
def teacher(args, d, X, L_raw, members, names, folds, order, F_in, dev):
    import onnxruntime as ort
    N = X.shape[0]
    mdir = os.path.join(ROOT, "src", "models")
    nj = json.load(open(os.path.join(mdir, "learned_norm.json"), encoding="utf-8"))
    mu = np.asarray(nj["mu"], np.float64)
    sd = np.asarray(nj["sd"], np.float64)
    clipv = float(nj["clip"])
    Lz = np.clip((L_raw - mu) / sd, -clipv, clipv).astype(np.float32)
    Fs = np.concatenate([X, Lz], axis=1).astype(np.float32)
    sess = ort.InferenceSession(os.path.join(mdir, "metric_head.onnx"),
                                providers=["CPUExecutionProvider"])
    iname = sess.get_inputs()[0].name
    oname = sess.get_outputs()[0].name
    E = np.empty((N, 512), np.float32)
    for b in range(0, N, 2048):
        E[b:b + 2048] = sess.run([oname], {iname: Fs[b:b + 2048]})[0]

    print("\n" + "=" * 76)
    print("TEACHER TEST -- the SHIPPED MusicIP distillation, never trained on these lists")
    print("=" * 76)
    print(f"{'fold':>4} {'CLAP':>8} {'SHIPPED musicip head':>22} {'behavior head':>16}")
    rows = []
    for fi, te in enumerate(folds):
        tr = [i for i in order if i not in te]
        te_groups = [members[i] for i in te]
        rb, _, _ = th.recall_at_k(X, te_groups, k=Cfg.k, max_seeds=Cfg.max_seeds)
        rs, _, _ = th.recall_at_k(E, te_groups, k=Cfg.k, max_seeds=Cfg.max_seeds)
        _, Eb, _ = th.train_head(F_in, [members[i] for i in tr], [], Cfg, dev, log=lambda s: None)
        rl, _, _ = th.recall_at_k(Eb, te_groups, k=Cfg.k, max_seeds=Cfg.max_seeds)
        print(f"{fi:>4} {rb:8.4f} {rs:22.4f} {rl:16.4f}")
        rows.append((rb, rs, rl))
    m = np.mean(rows, axis=0)
    print(f"\n  MEAN  CLAP {m[0]:.4f}   SHIPPED MusicIP head {m[1]:.4f}   "
          f"behavior head {m[2]:.4f}   (pool {N:,})")
    print(f"    shipped head's FREE gain over CLAP: {m[1]-m[0]:+.4f}  ({m[1]/max(m[0],1e-9):.1f}x)")
    print(f"    behavior head trained ON these lists is {m[1]-m[2]:+.4f} WORSE than it")
    print("\n  per-list (recall@25, pool %s):" % f"{N:,}")
    per = []
    for nm, g in sorted(zip(names, members), key=lambda t: -len(t[1])):
        r, _, _ = th.recall_at_k(E, [g], k=Cfg.k)
        r0, _, _ = th.recall_at_k(X, [g], k=Cfg.k)
        print(f"    CLAP {r0:.3f}   musicip-head {r:.3f}   n={len(g):4}  {nm}")
        per.append({"file": nm, "n": len(g), "clap": r0, "musicip_head": r})
    return {"clap": m[0], "shipped_musicip_head": m[1], "behavior_head": m[2],
            "per_list": per}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--pairs", default=os.path.join(HERE, "behavior_pairs.json"))
    ap.add_argument("--playlists", required=True)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--out", default=os.path.join(HERE, "behavior_signal_probe.json"))
    args = ap.parse_args()

    import torch
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    random.seed(th.SEED)
    np.random.seed(th.SEED)
    print(f"torch {torch.__version__}  device {dev}")

    d = th.load_pairs(args.pairs)
    paths = d["paths"]
    X, L_raw = th.load_features(args.db, paths)
    idx = {p: i for i, p in enumerate(paths)}
    _, by_relkey, _ = bp.load_pool(args.db)
    print(f"pool {len(paths):,} tracks (CLAP-512 + librosa-79)")

    out = {"pool_size": len(paths), "k": Cfg.k, "seed": th.SEED}
    out["coherence"] = coherence(args, X, paths, by_relkey, idx)
    ctl, members, names, order, folds, F_in = controls(args, d, X, L_raw, dev)
    out["controls"] = ctl
    out["teacher"] = teacher(args, d, X, L_raw, members, names, folds, order, F_in, dev)

    print("\n" + "=" * 76)
    print("VERDICT")
    print("=" * 76)
    t = out["teacher"]
    if t["shipped_musicip_head"] > t["behavior_head"]:
        print("  The shipped MusicIP distillation predicts these playlists BETTER than a head")
        print("  trained directly on them, having never seen them. The playlists are the")
        print("  teacher's output, not the operator's taste. NO personal signal to learn.")
    else:
        print("  The behavior head beats the shipped MusicIP head on held-out lists: there is")
        print("  structure here the MusicIP distillation does not already contain.")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
