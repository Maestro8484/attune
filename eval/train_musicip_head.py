r"""Teach a small head to rank the way MusicIP Mixer ranks, from MusicIP's own lists.

What this is: knowledge distillation. MusicIP Mixer (2005 to 2010, abandonware) ranks a library by a
similarity function nobody can read. Its lists for thousands of seeds, pulled through its local API,
are the teacher; a two-layer head over the sound fingerprints Attune already stores (LAW 2: the base
fingerprint is never swapped, only re-projected) is the student. The head learns a space in which
MusicIP's picks for a seed sit nearest the seed. The shipped learned engine (metric_head.onnx) was
made the same way in July 2026 on the earlier kind of fingerprint; this trainer does it again on the
fingerprints from the model's trained weights, in the sound head's file format, so the engine runs
it with numpy alone (hybrid._apply_head) and every walk, line, floor and trace keeps working.

Prior art reused, not reinvented:
  loss and split    MusicIP/extracted/distill.py (the July trainer): rank-weighted softmax over each
                    seed's top-K teacher picks against random negatives, a seed holdout for
                    checkpoint choice, an optional TRACK holdout for the inductive test
  head and file     audit-sonic/harness/sound_head_train.py and sound_head_export.py: mean, two
                    layers with the tanh form of GELU, unit output, 16-bit weights in an .npz that
                    hybrid._load_head reads

Teacher lists come from two places, both keyed by musicip_engine.relkey:
  --teacher     json written by audit-collections/musicip-teacher-2026-10-06/pull_teacher.py
                (Joe's setting, style 5 variety 0; its "held_out" seeds are NEVER trained on)
  --teacher-db  the July corpus MusicIP/extracted/teacher.db (--teacher-db-style picks the
                setting; style 40 lists agree 85% with style 5 lists, measured 2026-10-06)

Every retrieval number printed here is a convergence diagnostic for choosing a checkpoint, reported
with its pool size (LAW 3). The number that matters is measured afterwards, on the held-out seeds,
by eval/musicip_agreement.py. Runs under the ML venv (torch, GPU). Read-only on the library.

usage (from the workspace root):
  mixer-ng\.venv-ml\Scripts\python.exe attune\eval\train_musicip_head.py --db mixer-ng\data\mixer.db ^
      --teacher audit-collections\musicip-teacher-2026-10-06\teacher_style5.json ^
      --teacher-db ..\MusicIP\extracted\teacher.db --teacher-db-style 40 ^
      --out audit-sonic\_scratch\musicip_head --tag mip512
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import math
import os
import random
import sqlite3
import sys
import time
import zlib

import numpy as np

if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
SEED = 20261006
CLIP = 6.0


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ME = _load("musicip_engine", os.path.join(SRC, "musicip_engine.py"))
HY = _load("hybrid", os.path.join(SRC, "hybrid.py"))


# --------------------------------------------------------------------------- data
def load_pool(db_path):
    """The engine's own pool, in its own order, with the stored fingerprints exactly as the
    engine holds them (eng.X) and the raw 79 sound descriptors beside them."""
    eng = HY.HybridEngine(db_path, profile="sound" if HY._fingerprint_weights(db_path)
                          == HY.TRAINED_CLAP_WEIGHTS else "v2")
    con = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True)
    lib = {}
    for p, blob, dim in con.execute("SELECT path,vec,dim FROM features WHERE vec IS NOT NULL AND error IS NULL"):
        if dim != 79:
            continue
        v = np.frombuffer(blob, np.float32)
        if v.shape[0] == 79 and np.isfinite(v).all():
            lib[p] = v
    con.close()
    L = np.zeros((len(eng.paths), 79), np.float32)
    have = np.zeros(len(eng.paths), bool)
    for i, p in enumerate(eng.paths):
        v = lib.get(p)
        if v is not None:
            L[i] = v
            have[i] = True
    return eng, L, have


def load_teacher_json(path, recon):
    d = json.load(open(path, encoding="utf-8"))
    lists, held = {}, set()
    for seed, tl in d["lists"].items():
        si = recon.get(seed)
        if si is None:
            continue
        rows = [recon[t] for t in tl if t in recon]
        if len(rows) >= 30:
            lists[si] = rows
    for seed in d.get("held_out", []):
        si = recon.get(seed)
        if si is not None:
            held.add(si)
    return lists, held, d.get("setting", {})


def load_teacher_db(path, style, recon):
    con = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
    rows = con.execute("SELECT seed,tracks FROM mix WHERE style=? AND variety=0 AND size=100 "
                       "AND tracks IS NOT NULL AND err IS NULL", (style,)).fetchall()
    con.close()
    lists = {}
    for seed, blob in rows:
        si = recon.get(ME.relkey(seed))
        if si is None:
            continue
        tl = zlib.decompress(blob).decode("utf-8").split("\n")[1:]
        rows_i = [recon[ME.relkey(t)] for t in tl if t.strip() and ME.relkey(t) in recon]
        if len(rows_i) >= 30:
            lists[si] = rows_i
    return lists


def recall_at_k(score_fn, lists, k, allowed):
    """mean over seeds of |top-k by score within `allowed` ∩ teacher top-k| / k."""
    rec = []
    for si, tl in lists.items():
        sc = score_fn(si).copy()
        sc[~allowed] = -np.inf
        sc[si] = -np.inf
        tgt = set(tl[:k])
        top = np.argpartition(-sc, k - 1)[:k]
        rec.append(len(set(top.tolist()) & tgt) / k)
    return float(np.mean(rec))


# --------------------------------------------------------------------------- the head
def run_head_numpy(h, X):
    """hybrid._apply_head, written out, so the exported file is checked against the trainer."""
    Xc = X.astype(np.float32) - h["mean"].astype(np.float32)
    Xc /= np.maximum(np.linalg.norm(Xc, axis=1, keepdims=True), 1e-9)
    a = Xc @ h["w1"].astype(np.float32).T + h["b1"].astype(np.float32)
    a = 0.5 * a * (1.0 + np.tanh(0.7978845608028654 * (a + 0.044715 * a ** 3)))
    y = a @ h["w2"].astype(np.float32).T + h["b2"].astype(np.float32)
    return y / np.maximum(np.linalg.norm(y, axis=1, keepdims=True), 1e-9)


def _save(fn, path, *a, **kw):
    """Write beside the target, then move it into place; a few retries, because a file that
    was rewritten a second ago can still be held open by the virus scanner on Windows
    (measured 2026-10-06: one run in six lost its checkpoint to a PermissionError)."""
    root, ext = os.path.splitext(path)          # np.save and np.savez add the extension themselves
    tmp = root + ".part" + ext
    for attempt in range(6):
        try:
            fn(tmp, *a, **kw)
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.5 * (attempt + 1))
    fn(path, *a, **kw)


def run_head_numpy_lib(h, X, L_raw, have_lib):
    """The clap+lib form: the centered unit fingerprint with the z-scored descriptors beside it
    (zeros where a song has none), then the two layers. Only for a head trained with --inputs
    clap+lib; the engine has no path for it unless that form wins and is wired in."""
    Xc = X.astype(np.float32) - h["mean"].astype(np.float32)[:X.shape[1]]
    Xc /= np.maximum(np.linalg.norm(Xc, axis=1, keepdims=True), 1e-9)
    c = float(h["lib_clip"])
    LZ = np.clip((L_raw - h["lib_mu"]) / h["lib_sd"], -c, c).astype(np.float32)
    LZ[~have_lib] = 0.0
    a = np.hstack([Xc, LZ]) @ h["w1"].astype(np.float32).T + h["b1"].astype(np.float32)
    a = 0.5 * a * (1.0 + np.tanh(0.7978845608028654 * (a + 0.044715 * a ** 3)))
    y = a @ h["w2"].astype(np.float32).T + h["b2"].astype(np.float32)
    return y / np.maximum(np.linalg.norm(y, axis=1, keepdims=True), 1e-9)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--teacher", action="append", default=[], help="teacher json (pull_teacher.py); repeatable")
    ap.add_argument("--teacher-db", default=None, help="the July teacher.db")
    ap.add_argument("--teacher-db-style", type=int, default=40)
    ap.add_argument("--out", required=True, help="folder for the head file and the result json")
    ap.add_argument("--tag", default="mip")
    ap.add_argument("--inputs", choices=("clap", "clap+lib"), default="clap",
                    help="clap: the 512 fingerprint alone; clap+lib: plus the 79 descriptors, z-scored")
    ap.add_argument("--hidden", type=int, default=2048)
    ap.add_argument("--dim", type=int, default=256)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=5e-4)
    ap.add_argument("--tau", type=float, default=0.05)
    ap.add_argument("--pos-k", type=int, default=25)
    ap.add_argument("--negs", type=int, default=4096)
    ap.add_argument("--drop", type=float, default=0.1)
    ap.add_argument("--track-holdout", type=float, default=0.0,
                    help="share of MusicIP-known tracks never seen in any loss term (the inductive test)")
    ap.add_argument("--val-share", type=float, default=0.1)
    args = ap.parse_args(argv)

    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    t0 = time.time()
    eng, L_raw, have_lib = load_pool(args.db)
    N = len(eng.paths)
    recon = ME.build_reconciler(eng.paths)
    print(f"pool {N} songs (profile {eng.profile}); descriptors for {int(have_lib.sum())}; device {dev}")

    lists, held, setting = {}, set(), {}
    for p in args.teacher:
        li, he, se = load_teacher_json(p, recon)
        print(f"teacher {os.path.basename(p)}: {len(li)} seeds usable, {len(he)} held out, setting {se}")
        lists.update(li)
        held |= he
        setting = se
    july = {}
    if args.teacher_db:
        july = load_teacher_db(args.teacher_db, args.teacher_db_style, recon)
        print(f"teacher.db style {args.teacher_db_style}: {len(july)} seeds usable")
    # a held-out seed's list is never trained on, at any setting
    train_lists = {si: tl for si, tl in july.items() if si not in held}
    train_lists.update({si: tl for si, tl in lists.items() if si not in held})
    known = np.zeros(N, bool)                       # what MusicIP knows: every seed and pick
    for si, tl in list(july.items()) + list(lists.items()):
        known[si] = True
        known[tl] = True
    print(f"training seeds {len(train_lists)}; MusicIP-known songs in the pool {int(known.sum())}; "
          f"held out {len(held)} (measured only by musicip_agreement.py)")

    # ---- inputs: the fingerprint as the engine holds it (unit rows), centered by the head's mean
    rng = np.random.default_rng(SEED)
    X = eng.X.astype(np.float32)
    mean = X.mean(0).astype(np.float32)
    Xc = X - mean
    Xc /= np.maximum(np.linalg.norm(Xc, axis=1, keepdims=True), 1e-9)
    lib_mu = lib_sd = None
    if args.inputs == "clap+lib":
        vis = have_lib & known
        lib_mu, lib_sd = L_raw[vis].mean(0), L_raw[vis].std(0)
        lib_sd[lib_sd < 1e-9] = 1e-9
        LZ = np.clip((L_raw - lib_mu) / lib_sd, -CLIP, CLIP).astype(np.float32)
        LZ[~have_lib] = 0.0
        feats = np.hstack([Xc, LZ])
    else:
        feats = Xc
    D = feats.shape[1]

    # ---- the splits
    seeds = sorted(train_lists)
    random.Random(SEED).shuffle(seeds)
    n_val = max(50, int(args.val_share * len(seeds)))
    val_seeds, tr_seeds = seeds[:n_val], seeds[n_val:]
    held_tracks = None
    if args.track_holdout > 0:
        cand = np.flatnonzero(known)
        held_tracks = np.zeros(N, bool)
        held_tracks[rng.choice(cand, int(args.track_holdout * len(cand)), replace=False)] = True
        tr_seeds = [s for s in tr_seeds if not held_tracks[s]]
        val_seeds = [s for s in val_seeds if not held_tracks[s]]
        print(f"TRACK HOLDOUT: {int(held_tracks.sum())} MusicIP-known songs in no loss term")
    K = args.pos_k
    pos = {}
    for s in tr_seeds:
        c = [j for j in train_lists[s] if held_tracks is None or not held_tracks[j]]
        if len(c) >= K:
            pos[s] = c[:K]
    tr_seeds = [s for s in tr_seeds if s in pos]
    print(f"train seeds {len(tr_seeds)}  val seeds {len(val_seeds)}  inputs {D}")

    # ---- negatives are drawn from what MusicIP knows: a song it cannot return is neither a
    # positive nor a negative, so the head learns nothing about Singles from their absence
    neg_pool = np.flatnonzero(known & (~held_tracks if held_tracks is not None else True))
    Ft = torch.from_numpy(feats).to(dev)
    sidx = torch.tensor(tr_seeds, device=dev)
    P = torch.tensor([pos[s] for s in tr_seeds], device=dev)
    negT = torch.from_numpy(neg_pool).to(dev)
    rank_w = torch.tensor([1.0 / math.log2(r + 2) for r in range(K)], device=dev)

    class Head(nn.Module):
        def __init__(self):
            super().__init__()
            self.net = nn.Sequential(nn.Linear(D, args.hidden), nn.GELU(approximate="tanh"),
                                     nn.Dropout(args.drop), nn.Linear(args.hidden, args.dim))

        def forward(self, x):
            return F.normalize(self.net(x), dim=-1)

    torch.manual_seed(SEED)
    model = Head().to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    steps = args.epochs * max(1, len(tr_seeds) // args.batch)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, args.lr, total_steps=steps, pct_start=0.2)
    print(f"head {D}->{args.hidden}->{args.dim}, tau {args.tau}, K {K}, negs {args.negs}, "
          f"{sum(p.numel() for p in model.parameters())} params, {steps} steps")
    allowed_val = known.copy()
    val = {s: train_lists[s] for s in val_seeds}
    best, best_ep, step = -1.0, 0, 0
    os.makedirs(args.out, exist_ok=True)
    g = torch.Generator(device=dev).manual_seed(SEED)
    for ep in range(args.epochs):
        model.train()
        perm = torch.randperm(len(tr_seeds), device=dev, generator=g)
        tot, nb = 0.0, 0
        for b0 in range(0, len(tr_seeds) - args.batch + 1, args.batch):
            bi = perm[b0:b0 + args.batch]
            s_rows, p_rows = sidx[bi], P[bi]
            neg = negT[torch.randint(0, len(neg_pool), (args.negs,), device=dev, generator=g)]
            allrows = torch.cat([p_rows.reshape(-1), neg])
            E = model(Ft[allrows])
            S = model(Ft[s_rows])
            logits = (S @ E.T) / args.tau
            B = len(bi)
            tgt = torch.zeros_like(logits)
            ar = torch.arange(B, device=dev).unsqueeze(1)
            tgt[ar, torch.arange(K, device=dev).unsqueeze(0) + ar * K] = rank_w.unsqueeze(0)
            logits = logits.masked_fill(allrows.unsqueeze(0) == s_rows.unsqueeze(1), -1e4)
            loss = -(tgt * F.log_softmax(logits, dim=1)).sum(1).mean() / rank_w.sum()
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            if step < steps - 1:
                sched.step()
            step += 1
            tot += float(loss)
            nb += 1
        model.eval()
        with torch.no_grad():
            E = torch.cat([model(Ft[i:i + 4096]) for i in range(0, N, 4096)]).cpu().numpy()
        rv = recall_at_k(lambda si: E @ E[si], val, 25, allowed_val)
        flag = ""
        if rv > best:
            best, best_ep, flag = rv, ep + 1, "  <- best"
            sd = {k: v.detach().cpu().numpy() for k, v in model.state_dict().items()}
            _save(np.save, os.path.join(args.out, f"{args.tag}.npy"), E.astype(np.float32))
            _save(np.savez, os.path.join(args.out, f"{args.tag}_train.npz"), mean=mean,
                  w1=sd["net.0.weight"], b1=sd["net.0.bias"], w2=sd["net.3.weight"], b2=sd["net.3.bias"],
                  **({"lib_mu": lib_mu.astype(np.float32), "lib_sd": lib_sd.astype(np.float32),
                      "lib_clip": np.float32(CLIP)} if lib_mu is not None else {}))
        print(f"  ep{ep + 1:02d} loss {tot / max(nb, 1):.4f}  val recall@25 {rv:.4f} "
              f"(pool {int(allowed_val.sum())}) ({time.time() - t0:.0f}s){flag}")

    # ---- the file the engine reads: 16-bit weights, the sound head's layout
    tr = np.load(os.path.join(args.out, f"{args.tag}_train.npz"))
    out_file = os.path.join(args.out, f"{args.tag}.npz")
    extra = {}
    if "lib_mu" in tr:
        extra = {"lib_mu": tr["lib_mu"], "lib_sd": tr["lib_sd"], "lib_clip": tr["lib_clip"]}
    np.savez(out_file, mean=tr["mean"].astype(np.float32),
             w1=tr["w1"].astype(np.float16), b1=tr["b1"].astype(np.float16),
             w2=tr["w2"].astype(np.float16), b2=tr["b2"].astype(np.float16),
             made_for=np.array(HY.TRAINED_CLAP_WEIGHTS),
             taught_by=np.array(f"MusicIP Mixer 1.9 lists through its local API, style {setting.get('style', '?')} "
                                f"variety {setting.get('variety', '?')} and the July 2026 corpus at style "
                                f"{args.teacher_db_style}; {len(tr_seeds)} seeds"),
             inputs=np.array(args.inputs), **extra)
    # ---- parity: numpy from the 16-bit file against the trainer's own output
    ref = np.load(os.path.join(args.out, f"{args.tag}.npy"))
    h = dict(np.load(out_file))
    # run_head_numpy centers by the head's own mean, so it takes the engine's X, not Xc
    if args.inputs == "clap":
        got = run_head_numpy(h, X)
    else:
        got = run_head_numpy_lib(h, X, L_raw, have_lib)
    cos = (ref * got).sum(1)
    pick = rng.choice(N, 500, replace=False)
    same = 0
    for i in pick:
        same += len(set(np.argsort(-(ref @ ref[i]))[:51]) & set(np.argsort(-(got @ got[i]))[:51])) - 1
    result = {"config": vars(args), "best_val_recall25": best, "best_epoch": best_ep,
              "val_pool": int(allowed_val.sum()), "train_seeds": len(tr_seeds), "val_seeds": len(val_seeds),
              "held_out_seeds": len(held), "parity_cos_min": float(cos.min()), "parity_cos_mean": float(cos.mean()),
              "parity_nearest50_same": int(same), "parity_nearest50_of": 500 * 50,
              "inputs_dim": int(D), "head_file": out_file, "seconds": int(time.time() - t0)}
    if held_tracks is not None:
        ind = {s: [j for j in train_lists[s] if held_tracks[j]] for s in val_seeds}
        ind = {s: tl for s, tl in ind.items() if len(tl) >= 5}
        n_h = int(held_tracks.sum())
        r_h = recall_at_k(lambda si: ref @ ref[si], ind, 5, held_tracks)
        r_c = recall_at_k(lambda si: X @ X[si], ind, 5, held_tracks)
        result.update({"inductive_recall5_head": r_h, "inductive_recall5_clap": r_c, "inductive_pool": n_h,
                       "inductive_seeds": len(ind)})
        print(f"INDUCTIVE (pool = the {n_h} songs no loss term saw; recall@5 of MusicIP's picks among them, "
              f"{len(ind)} seeds): head {r_h:.4f}  plain fingerprint {r_c:.4f}")
    json.dump(result, open(os.path.join(args.out, f"{args.tag}_result.json"), "w"), indent=1)
    print(f"best val recall@25 {best:.4f} at epoch {best_ep} (pool {int(allowed_val.sum())}); "
          f"parity from the 16-bit file: cosine min {cos.min():.6f} mean {cos.mean():.6f}, "
          f"nearest 50 of 500 songs {same} of {500 * 50} the same; wrote {out_file} in {int(time.time() - t0)}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
