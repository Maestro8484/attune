r"""Phase 2 of the behavioral head -- train a projection head on the operator's behavioural
pairs, and MEASURE whether it generalises before believing it.

Architecture and preprocessing follow the proven recipe that produced the shipped learned
head (the MusicIP workspace's extracted/distill.py): inputs are the CLAP-512 + librosa-79
features ALREADY in mixer.db (LAW 2 -- the base embedding is never swapped, only
re-projected), librosa z-scored on visible rows only, head = 591 -> 4096 GELU -> 512 with an
L2-normalized output, InfoNCE with in-batch + random negatives.

TWO HOLDOUTS, and the difference between them is the whole point:

  --track-holdout   a random slice of TRACKS never enters any loss term; the eval pool is
                    those tracks. This is distill.py's inductive test and it answers
                    "does the metric transfer to a track it has not seen".

  --holdout-lists   whole PLAYLISTS are withheld. This answers the question that actually
                    decides whether a personal taste head is real: "does what I learned from
                    these playlists predict a playlist I have never seen?"  Pairs inside one
                    playlist are NOT independent samples -- a 130-track list contributes 8,385
                    pairs but exactly ONE observation of taste.  A track holdout cannot catch
                    this, because held-out tracks still sit inside lists the model trained on.
                    With few lists, the track holdout will look healthy while the list holdout
                    shows the model learned the catalogue, not the taste.

Every retrieval number here is a CONVERGENCE DIAGNOSTIC reported with its pool size (LAW 3).
None of them may be used to declare an engine better -- that is an ear test (LAW 1).

Runs under the ML venv (torch + CUDA).

usage:
  # the honest generalisation probe -- run this BEFORE training anything for real
  python eval/train_behavior_head.py --db ..\mixer-ng\data\mixer.db \
      --pairs eval/behavior_pairs.json --mode probe --folds 5

  # full training run
  python eval/train_behavior_head.py --db ..\mixer-ng\data\mixer.db \
      --pairs eval/behavior_pairs.json --mode train --epochs 30 --track-holdout 0.10
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import sqlite3
import sys
import time

import numpy as np

if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
    # see build_behavior_pairs.py: never rewrap an already-wrapped stdout on import.
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODELS = os.path.join(ROOT, "src", "models")

SEED = 20260727            # deterministic; recorded in the provenance json


# --------------------------------------------------------------------------- data
def zscore(L_raw, rows=None, clip=6.0):
    """distill.py's standardization. `rows` restricts the statistics to visible tracks so a
    holdout cannot leak its distribution through mu/sd."""
    src = L_raw if rows is None else L_raw[rows]
    mu, sd = src.mean(0), src.std(0)
    sd[sd < 1e-9] = 1e-9
    return np.clip(((L_raw - mu) / sd).astype(np.float32), -clip, clip), mu, sd


def load_features(db_path, paths):
    """CLAP-512 (row-L2-normalized) and raw librosa-79 for exactly `paths`, in that order."""
    con = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True)
    clap, lib = {}, {}
    for p, dim, blob in con.execute("SELECT path,dim,vec FROM clap WHERE vec IS NOT NULL"):
        if dim == 512:
            v = np.frombuffer(blob, np.float32)
            if v.shape[0] == 512:
                clap[p] = v
    for p, blob, dim in con.execute(
            "SELECT path,vec,dim FROM features WHERE vec IS NOT NULL AND error IS NULL"):
        if dim != 79:
            continue
        v = np.frombuffer(blob, np.float32)
        if v.shape[0] == 79 and np.isfinite(v).all():
            lib[p] = v
    con.close()
    missing = [p for p in paths if p not in clap or p not in lib]
    if missing:
        raise SystemExit(f"{len(missing)} pool paths lack CLAP or librosa -- pairs file and DB "
                         f"disagree; re-run build_behavior_pairs.py")
    X = np.vstack([clap[p] for p in paths]).astype(np.float32)
    X /= np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-9)
    L_raw = np.vstack([lib[p] for p in paths]).astype(np.float64)
    return X, L_raw


def load_pairs(pairs_json):
    d = json.load(open(pairs_json, encoding="utf-8"))
    if "paths" not in d or "list_members" not in d:
        raise SystemExit("pairs file has no 'paths'/'list_members' -- re-run "
                         "build_behavior_pairs.py with --emit-pairs")
    return d


# --------------------------------------------------------------------------- metric
def recall_at_k(E, groups, k=25, pool_mask=None, max_seeds=None, rng_seed=7):
    """For each group (a playlist's member rows), use each member as a seed and measure how
    many OTHER members land in the top-k of the ranking induced by E.

    POOL SIZE IS PART OF THE NUMBER (LAW 3): with `pool_mask` None the pool is every row of
    E, and the random baseline is k / len(E). Never compare across pools."""
    n_pool = int(pool_mask.sum()) if pool_mask is not None else E.shape[0]
    recs = []
    seeds = []
    for g in groups:
        g = [r for r in g if pool_mask is None or pool_mask[r]]
        if len(g) < 5:
            continue
        for s in g:
            seeds.append((s, [x for x in g if x != s]))
    if max_seeds and len(seeds) > max_seeds:
        random.Random(rng_seed).shuffle(seeds)
        seeds = seeds[:max_seeds]
    for s, tgt in seeds:
        sc = E @ E[s]
        if pool_mask is not None:
            sc = np.where(pool_mask, sc, -np.inf)
        sc[s] = -np.inf
        keff = min(k, len(tgt))
        if keff < 1 or int(np.isfinite(sc).sum()) < keff:
            continue
        top = set(np.argpartition(-sc, keff - 1)[:keff].tolist())
        recs.append(len(top & set(tgt)) / keff)
    return (float(np.mean(recs)) if recs else 0.0), len(recs), n_pool


# --------------------------------------------------------------------------- model
def build_head(d_in, d_out, hidden, dev):
    import torch.nn as nn
    import torch.nn.functional as F

    class Head(nn.Module):
        def __init__(self):
            super().__init__()
            self.f = nn.Sequential(nn.Linear(d_in, hidden), nn.GELU(), nn.Linear(hidden, d_out))

        def forward(self, x):
            return F.normalize(self.f(x), dim=-1)

    return Head().to(dev)


def train_head(F_in, groups, hard_negs, args, dev, log=print):
    """InfoNCE over playlist co-membership: seed = one member, positives = K other members of
    the same list, negatives = in-batch positives of other seeds + random pool rows + the
    mined hard negatives."""
    import torch
    import torch.nn.functional as Fn

    torch.manual_seed(SEED)
    N, D = F_in.shape
    Ft = torch.from_numpy(F_in).to(dev)
    K = args.pos_k

    anchors, poss = [], []
    rng = random.Random(SEED)
    for g in groups:
        if len(g) < K + 1:
            continue
        for s in g:
            others = [x for x in g if x != s]
            rng.shuffle(others)
            anchors.append(s)
            poss.append(others[:K])
    if not anchors:
        raise SystemExit("no usable (anchor, positives) rows -- lists too small for --pos-k")
    A = torch.tensor(anchors, device=dev)
    P = torch.tensor(poss, device=dev)
    HN = torch.tensor(sorted(hard_negs), device=dev) if hard_negs else None
    rank_w = torch.ones(K, device=dev)          # co-membership is unordered: uniform weights

    model = build_head(D, args.dim, args.hidden, dev)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    batch = min(args.batch, len(anchors))
    steps = max(1, args.epochs * max(1, len(anchors) // batch))
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, args.lr, total_steps=steps, pct_start=0.2)
    log(f"    head {D}->{args.hidden}->{args.dim}  "
        f"{sum(p.numel() for p in model.parameters()):,} params   "
        f"anchors={len(anchors)}  steps={steps}  tau={args.tau}")

    step = 0
    hist = []
    for ep in range(args.epochs):
        model.train()
        perm = torch.randperm(len(anchors), device=dev)
        tot, nb = 0.0, 0
        for b0 in range(0, len(anchors) - batch + 1, batch):
            bi = perm[b0:b0 + batch]
            s_rows, p_rows = A[bi], P[bi]
            neg = torch.randint(0, N, (args.negs,), device=dev)
            if HN is not None:
                neg = torch.cat([neg, HN])
            allrows = torch.cat([p_rows.reshape(-1), neg])
            E = model(Ft[allrows])
            S = model(Ft[s_rows])
            logits = (S @ E.T) / args.tau
            B = len(bi)
            tgt = torch.zeros_like(logits)
            ar = torch.arange(B, device=dev).unsqueeze(1)
            tgt[ar, torch.arange(K, device=dev).unsqueeze(0) + ar * K] = rank_w.unsqueeze(0)
            logits = logits.masked_fill(allrows.unsqueeze(0) == s_rows.unsqueeze(1), -1e4)
            loss = -(tgt * Fn.log_softmax(logits, dim=1)).sum(1).mean() / rank_w.sum()
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            if step < steps - 1:
                sched.step()
            step += 1
            tot += float(loss)
            nb += 1
        hist.append(tot / max(nb, 1))
    model.eval()
    with torch.no_grad():
        E = np.empty((N, args.dim), np.float32)
        for b in range(0, N, 4096):
            E[b:b + 4096] = model(Ft[b:b + 4096]).cpu().numpy()
    return model, E, hist


# --------------------------------------------------------------------------- probe
def mode_probe(args, d, paths, X, L_raw, dev):
    """LEAVE-LISTS-OUT: the test that decides whether this head is real.

    Train on the pairs from all lists except a held-out fold of whole lists, then ask whether
    co-membership inside those unseen lists is retrieved better than the untrained CLAP
    baseline. A head that has learned the operator's taste beats CLAP on lists it never saw.
    A head that has merely memorised which tracks were in the training lists does not."""
    members = [lm["rows"] for lm in d["list_members"] if len(lm["rows"]) >= args.pos_k + 1]
    names = [lm["file"] for lm in d["list_members"] if len(lm["rows"]) >= args.pos_k + 1]
    n = len(members)
    print(f"\nlists usable for the probe (>= {args.pos_k+1} resolved tracks): {n}")
    if n < args.folds * 2:
        print(f"!! only {n} lists -- {args.folds} folds leaves too few to train on.")
    order = list(range(n))
    random.Random(SEED).shuffle(order)
    folds = [order[i::args.folds] for i in range(args.folds)]

    Lz, _, _ = zscore(L_raw)
    F_in = np.concatenate([X, Lz], axis=1).astype(np.float32)
    hard = d.get("hard_negatives") or []

    print(f"\nPOOL for every number below: {X.shape[0]:,} tracks "
          f"(random baseline recall@{args.k} = {args.k/X.shape[0]:.5f})")
    print(f"{'fold':>4} {'heldout lists':>14} {'CLAP base':>10} {'trained':>9} "
          f"{'delta':>8} {'seeds':>6}")
    base_all, learn_all = [], []
    for fi, te in enumerate(folds):
        tr = [i for i in order if i not in te]
        tr_groups = [members[i] for i in tr]
        te_groups = [members[i] for i in te]
        _, E, hist = train_head(F_in, tr_groups, hard, args, dev, log=lambda s: None)
        rb, nb_, pool = recall_at_k(X, te_groups, k=args.k, max_seeds=args.max_seeds)
        rl, nl_, _ = recall_at_k(E, te_groups, k=args.k, max_seeds=args.max_seeds)
        base_all.append(rb)
        learn_all.append(rl)
        print(f"{fi:>4} {len(te):>14} {rb:10.4f} {rl:9.4f} {rl-rb:+8.4f} {nl_:>6}"
              f"   loss {hist[0]:.3f}->{hist[-1]:.3f}")
    mb, ml = float(np.mean(base_all)), float(np.mean(learn_all))
    print(f"\n  MEAN over {args.folds} folds   CLAP baseline {mb:.4f}   trained {ml:.4f}   "
          f"delta {ml-mb:+.4f}")
    print(f"  (pool {X.shape[0]:,}; convergence diagnostic only -- LAW 1 forbids selecting on it)")

    # sanity contrast: the SAME metric measured on lists the model DID train on. If the
    # trained number is high here and flat above, the head memorised rather than generalised.
    tr_groups = [members[i] for i in order[args.folds:]]
    _, E, _ = train_head(F_in, tr_groups, hard, args, dev, log=lambda s: None)
    rb_in, _, _ = recall_at_k(X, tr_groups, k=args.k, max_seeds=args.max_seeds)
    rl_in, _, _ = recall_at_k(E, tr_groups, k=args.k, max_seeds=args.max_seeds)
    print(f"\n  CONTRAST -- lists the model DID train on: CLAP {rb_in:.4f} -> trained "
          f"{rl_in:.4f}  ({rl_in-rb_in:+.4f})")
    verdict = "GENERALISES" if (ml - mb) > args.min_delta else "DOES NOT GENERALISE"
    print(f"\n  PROBE VERDICT: {verdict}   "
          f"(held-out delta {ml-mb:+.4f} vs required > {args.min_delta:+.4f};"
          f" in-sample delta {rl_in-rb_in:+.4f})")
    out = {"folds": args.folds, "pool_size": int(X.shape[0]), "k": args.k,
           "clap_baseline_heldout": mb, "trained_heldout": ml, "delta_heldout": ml - mb,
           "clap_baseline_insample": rb_in, "trained_insample": rl_in,
           "delta_insample": rl_in - rb_in, "n_lists": n, "verdict": verdict,
           "config": vars(args), "seed": SEED}
    with open(os.path.join(HERE, "behavior_probe_result.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"\nwrote {os.path.join(HERE, 'behavior_probe_result.json')}")
    return out


# --------------------------------------------------------------------------- train
def mode_train(args, d, paths, X, L_raw, dev):
    N = X.shape[0]
    rng = np.random.default_rng(1234)          # distill.py's track-holdout rng
    held = np.zeros(N, bool)
    if args.track_holdout > 0:
        held[rng.choice(N, int(args.track_holdout * N), replace=False)] = True
    Lz, mu, sd = zscore(L_raw, rows=~held if args.track_holdout > 0 else None)
    F_in = np.concatenate([X, Lz], axis=1).astype(np.float32)

    members = [lm["rows"] for lm in d["list_members"]]
    tr_groups, te_groups = [], []
    for g in members:
        vis = [r for r in g if not held[r]]
        hid = [r for r in g if held[r]]
        if len(vis) >= args.pos_k + 1:
            tr_groups.append(vis)
        if len(hid) >= 2:
            te_groups.append(hid)
    print(f"track holdout: {int(held.sum())}/{N} tracks never enter any loss term")
    print(f"train groups {len(tr_groups)}   holdout-eval groups {len(te_groups)}")

    model, E, hist = train_head(F_in, tr_groups, d.get("hard_negatives") or [], args, dev)
    print("  loss: " + " ".join(f"{h:.3f}" for h in hist))
    if te_groups:
        rb, nb_, pool = recall_at_k(X, te_groups, k=args.k, pool_mask=held, max_seeds=args.max_seeds)
        rl, _, _ = recall_at_k(E, te_groups, k=args.k, pool_mask=held, max_seeds=args.max_seeds)
        print(f"  held-out TRACK recall@{args.k}: CLAP {rb:.4f} -> trained {rl:.4f}  "
              f"(pool {pool:,}, seeds {nb_}) -- diagnostic only")

    import torch
    os.makedirs(MODELS, exist_ok=True)
    torch.save({"state": model.state_dict(), "d_in": F_in.shape[1], "d_out": args.dim,
                "hidden": args.hidden, "paths": paths},
               os.path.join(MODELS, "behavior_head.pt"))
    prov = {
        "mu": mu.tolist(), "sd": sd.tolist(), "clip": 6.0,
        "_provenance": {
            "built": time.strftime("%Y-%m-%d"),
            "method": "eval/train_behavior_head.py --mode train; librosa-79 z-scored with "
                      "zscore(rows=~held); CLAP-512 row-L2-normalized; head "
                      f"{F_in.shape[1]}->{args.hidden} GELU->{args.dim}, L2-normalized output",
            "objective": "InfoNCE over playlist co-membership + usermeta positives",
            "seed": SEED, "track_holdout": args.track_holdout,
            "source_db": args.db, "pool_size": int(N),
            "data_counts": d.get("gate", {}) | d.get("source_a", {}) | d.get("source_b", {}),
            "exclusions": "ABTest* playlist subtrees (machine-generated ear-test sets)",
            "config": vars(args),
        },
    }
    with open(os.path.join(MODELS, "behavior_norm.json"), "w", encoding="utf-8") as fh:
        json.dump(prov, fh)
    print(f"\nsaved {os.path.join(MODELS, 'behavior_head.pt')} + behavior_norm.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--pairs", default=os.path.join(HERE, "behavior_pairs.json"))
    ap.add_argument("--mode", default="probe", choices=["probe", "train"])
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--dim", type=int, default=512)
    ap.add_argument("--hidden", type=int, default=4096)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--tau", type=float, default=0.05)
    ap.add_argument("--pos-k", type=int, default=8)
    ap.add_argument("--negs", type=int, default=2048)
    ap.add_argument("--k", type=int, default=25, help="recall@k (report with pool size)")
    ap.add_argument("--max-seeds", type=int, default=600)
    ap.add_argument("--min-delta", type=float, default=0.01,
                    help="held-out improvement over CLAP required to call it generalisation")
    ap.add_argument("--track-holdout", type=float, default=0.10)
    args = ap.parse_args()

    import torch
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"torch {torch.__version__}  device {dev}"
          f"{'  ' + torch.cuda.get_device_name(0) if dev == 'cuda' else ''}")
    random.seed(SEED)
    np.random.seed(SEED)

    d = load_pairs(args.pairs)
    paths = d["paths"]
    X, L_raw = load_features(args.db, paths)
    print(f"features: CLAP-512 + librosa-79 over {len(paths):,} pool tracks")

    if args.mode == "probe":
        mode_probe(args, d, paths, X, L_raw, dev)
    else:
        mode_train(args, d, paths, X, L_raw, dev)


if __name__ == "__main__":
    main()
