r"""How far each way of scoring agrees with MusicIP Mixer's lists, on seeds nothing was trained on.

The yardstick for HANDOFF_MUSICIP_REACH.md (TODO.md row 62, Joe's ruling of 2026-10-06: for that
one task MusicIP's lists pick what is built). For every held-out seed MusicIP gave 99 songs at
Joe's setting; each version of the engine below gives its own 99 best, and the number reported is
how many are the same. Read against the ceiling: MusicIP agrees with itself 99 of 99 at that
setting (variety 0 is deterministic, measured 2026-10-06, audit-collections/roller-derby-queen-
2026-10-06/self_agreement.json), so 99 is perfect and random is about 0.6.

Two candidate pools, both reported (LAW 3):
  known   the songs MusicIP knows that are also in Attune's pool (its Albums). The fair number:
          a Single is a song MusicIP could never have returned.
  full    Attune's whole pool. What the window would show, line off.
Two ways of picking the 99:
  top     the 99 best scores, nothing else. The score itself.
  walk    the engine's own best-first walk with its artist spacing (hybrid.mix, line off), on the
          known pool. What a mix would contain.
recall@25 is also printed, so the number can be set beside the July 2026 distillation's (0.15 on
the earlier fingerprints, pool 17k, MusicIP/extracted/distill_result.json).

Versions: the plain fingerprint, the sound head alone, the sound profile as shipped, the V2 recipe,
and any head file given with --head (name=path), alone and inside the sound profile's blend.
Lean venv, numpy only, read-only on the library. From the workspace root:

  mixer-ng\.venv\Scripts\python.exe attune\eval\musicip_agreement.py --db mixer-ng\data\mixer.db ^
      --teacher audit-collections\musicip-teacher-2026-10-06\teacher_style5.json ^
      --head mip512=audit-sonic\_scratch\musicip_head\mip512.npz --out audit-sonic\musicip_agreement.json
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
import time

import numpy as np

if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ME = _load("musicip_engine", os.path.join(SRC, "musicip_engine.py"))
HY = _load("hybrid", os.path.join(SRC, "hybrid.py"))


def load_teacher(path, recon):
    d = json.load(open(path, encoding="utf-8"))
    held, lists = [], {}
    for seed in d["held_out"]:
        si = recon.get(seed)
        if si is None:
            continue
        rows = [recon[t] for t in d["lists"][seed] if t in recon]
        if len(rows) >= 30:
            held.append(si)
            lists[si] = rows
    known = set()
    for seed, tl in d["lists"].items():
        si = recon.get(seed)
        if si is not None:
            known.add(si)
        known.update(recon[t] for t in tl if t in recon)
    return held, lists, known, d.get("setting", {})


def common(score, si, tgt, allowed, k):
    sc = score.copy()
    sc[~allowed] = -np.inf
    sc[si] = -np.inf
    top = np.argpartition(-sc, k - 1)[:k]
    return len(set(top.tolist()) & set(tgt))


def summarize(vals):
    a = np.array(vals, float)
    return {"mean": float(a.mean()), "median": float(np.median(a)), "p25": float(np.percentile(a, 25)),
            "p75": float(np.percentile(a, 75)), "min": float(a.min()), "max": float(a.max()), "n": int(len(a))}


def measure(name, score_fn, held, lists, known_mask, full_mask, eng_walk=None):
    c_known, c_full, r25, c_walk = [], [], [], []
    t0 = time.time()
    for si in held:
        tgt = lists[si]
        s = score_fn(si)
        c_known.append(common(s, si, tgt, known_mask, 99))
        c_full.append(common(s, si, tgt, full_mask, 99))
        r25.append(common(s, si, tgt[:25], known_mask, 25) / 25.0)
        if eng_walk is not None:
            picks = eng_walk.mix(eng_walk.paths[si], size=99, allowed=known_mask)
            c_walk.append(len({eng_walk.idx[p] for p in picks} & set(tgt)))
    out = {"common_of_99_known": summarize(c_known), "common_of_99_full": summarize(c_full),
           "recall25_known": summarize(r25), "seconds": int(time.time() - t0)}
    if c_walk:
        out["common_of_99_walk_known"] = summarize(c_walk)
    w = f"  walk {out['common_of_99_walk_known']['mean']:5.1f}" if c_walk else ""
    print(f"{name:52} top/known {out['common_of_99_known']['mean']:5.1f} (median {out['common_of_99_known']['median']:4.0f})"
          f"  top/full {out['common_of_99_full']['mean']:5.1f}  recall@25 {out['recall25_known']['mean']:.3f}{w}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--teacher", required=True)
    ap.add_argument("--head", action="append", default=[], help="name=path of a head .npz to score; repeatable")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-walk", action="store_true")
    ap.add_argument("--only", default=None, help="comma list of version names to run (substring match)")
    ap.add_argument("--hub", action="store_true", help="also score each head with two hubness corrections")
    ap.add_argument("--extras", action="store_true",
                    help="also score each head blended with the descriptors, the fingerprint and an "
                         "artist profile, on the z scale")
    args = ap.parse_args(argv)

    eng = HY.HybridEngine(args.db)                        # the library's own profile
    N = len(eng.paths)
    recon = ME.build_reconciler(eng.paths)
    held, lists, known, setting = load_teacher(args.teacher, recon)
    known_mask = np.zeros(N, bool)
    known_mask[list(known)] = True
    full_mask = np.ones(N, bool)
    print(f"pool {N} (profile {eng.profile}); MusicIP-known in pool {int(known_mask.sum())}; "
          f"held-out seeds {len(held)}; MusicIP setting {setting}")
    print(f"ceiling: MusicIP against itself 99 of 99 at this setting; random about {99 * 99 / known_mask.sum():.1f}")
    results = {"pool": N, "known_pool": int(known_mask.sum()), "held_out": len(held), "setting": setting,
               "versions": {}}

    def want(name):
        return not args.only or any(s.strip() and s.strip() in name for s in args.only.split(","))

    X = eng.X
    if want("fingerprint alone"):
        results["versions"]["fingerprint alone (cosine)"] = measure(
            "fingerprint alone (cosine)", lambda si: X @ X[si], held, lists, known_mask, full_mask)
    if eng.profile == "sound" and want("sound"):
        H = eng._clap_matrix("head")
        results["versions"]["sound head alone"] = measure(
            "sound head alone", lambda si: H @ H[si], held, lists, known_mask, full_mask)
        results["versions"]["sound profile as shipped"] = measure(
            "sound profile as shipped", eng._score, held, lists, known_mask, full_mask,
            None if args.no_walk else eng)
    if want("V2"):
        # V2 scores the descriptor term too, so its pool is the songs that have both a
        # fingerprint and descriptors: a few fewer rows, in its own order. Translate by path.
        v2 = HY.HybridEngine(args.db, profile="v2")
        to_v2 = {i: v2.idx[p] for i, p in enumerate(eng.paths) if p in v2.idx}
        held2 = [to_v2[si] for si in held if si in to_v2]
        lists2 = {to_v2[si]: [to_v2[j] for j in tl if j in to_v2] for si, tl in lists.items() if si in to_v2}
        known2 = np.zeros(len(v2.paths), bool)
        known2[[to_v2[i] for i in np.flatnonzero(known_mask) if i in to_v2]] = True
        results["versions"]["V2 recipe on these fingerprints"] = measure(
            "V2 recipe on these fingerprints", v2._score, held2, lists2, known2, np.ones(len(v2.paths), bool))
    # the other ingredients a head could be blended with, each as a unit-row matrix whose
    # cosine against the seed's row is the ingredient (then put on the z scale per seed)
    def unit(A):
        return A / np.maximum(np.linalg.norm(A, axis=1, keepdims=True), 1e-9)

    def z(g):
        sd = g.std()
        return (g - g.mean()) / sd if sd > 1e-12 else np.zeros_like(g)

    LZ = eng._lib_raw.astype(np.float64)
    have = np.abs(LZ).sum(1) > 0
    mu, sd = LZ[have].mean(0), LZ[have].std(0)
    sd[sd < 1e-9] = 1e-9
    LZ = unit((LZ - mu) / sd)
    LZ[~have] = 0.0

    def artist_profile(M):
        """The patent's artist profile: each artist's average row of M, unit length, so the
        ingredient is how alike two ARTISTS sound on the whole, not two songs. A song with no
        artist tag is its own artist."""
        A = np.array(M, np.float64)
        groups = {}
        for i, a in enumerate(eng.artist):
            if a:
                groups.setdefault(a.strip().lower(), []).append(i)
        for rows in groups.values():
            if len(rows) > 1:
                A[rows] = M[rows].mean(0)
        return unit(A)

    for spec in args.head:
        name, path = spec.split("=", 1)
        h = HY._load_head(path)
        if h is None:
            print(f"{name}: not a head file the engine can read ({path})")
            continue
        if not want(name):
            continue
        e = HY.HybridEngine(args.db, profile="musicip", head_file=path)
        M = e._clap_matrix("head").astype(np.float64)
        results["versions"][f"{name} head alone"] = measure(
            f"{name} head alone", lambda si, M=M: M @ M[si], held, lists, known_mask, full_mask,
            None if args.no_walk else e)
        if args.hub:
            # Two corrections for hubs (songs that sit near everything and fill every list),
            # both prior art: centering the output, and CSLS (Conneau et al. 2018, used against
            # hubness in cross-lingual word retrieval): a candidate's cosine is penalised by the
            # average cosine of its own k nearest neighbours, so a song near everything gains
            # nothing from being near this seed in particular.
            Mc = unit(M - M.mean(0))
            results["versions"][f"{name} head centered"] = measure(
                f"{name} head centered", lambda si, Mc=Mc: Mc @ Mc[si], held, lists, known_mask, full_mask)
            k = 10
            r = np.zeros(len(M))
            for b in range(0, len(M), 2048):
                S = M[b:b + 2048] @ M.T
                S[np.arange(S.shape[0]), np.arange(b, b + S.shape[0])] = -np.inf
                top = -np.partition(-S, k - 1, axis=1)[:, :k]
                r[b:b + 2048] = top.mean(1)
            results["versions"][f"{name} head CSLS k={k}"] = measure(
                f"{name} head CSLS k={k}", lambda si, M=M, r=r: 2 * (M @ M[si]) - r, held, lists, known_mask, full_mask)
            r5 = np.zeros(len(M))
            for b in range(0, len(M), 2048):
                S = M[b:b + 2048] @ M.T
                S[np.arange(S.shape[0]), np.arange(b, b + S.shape[0])] = -np.inf
                r5[b:b + 2048] = (-np.partition(-S, 49, axis=1)[:, :50]).mean(1)
            results["versions"][f"{name} head CSLS k=50"] = measure(
                f"{name} head CSLS k=50", lambda si, M=M, r=r5: 2 * (M @ M[si]) - r, held, lists, known_mask, full_mask)
            results["versions"][f"{name} head half CSLS k=50"] = measure(
                f"{name} head half CSLS k=50", lambda si, M=M, r=r5: 2 * (M @ M[si]) - 0.5 * r, held, lists, known_mask, full_mask)
        if not args.extras:
            continue
        AP = artist_profile(M)
        extras = {
            "+ 0.25 descriptors": lambda si: z(M @ M[si]) + 0.25 * z(LZ @ LZ[si]),
            "+ 0.5 descriptors": lambda si: z(M @ M[si]) + 0.5 * z(LZ @ LZ[si]),
            "+ 0.5 fingerprint": lambda si: z(M @ M[si]) + 0.5 * z(X @ X[si]),
            "+ 0.25 artist profile": lambda si: z(M @ M[si]) + 0.25 * z(AP @ AP[si]),
            "+ 0.5 artist profile": lambda si: z(M @ M[si]) + 0.5 * z(AP @ AP[si]),
            "+ 1.0 artist profile": lambda si: z(M @ M[si]) + 1.0 * z(AP @ AP[si]),
            "+ 0.5 artist profile + 0.25 descriptors": lambda si: (z(M @ M[si]) + 0.5 * z(AP @ AP[si])
                                                                   + 0.25 * z(LZ @ LZ[si])),
        }
        for label, fn in extras.items():
            results["versions"][f"{name} {label}"] = measure(f"{name} {label}", fn, held, lists, known_mask, full_mask)
        e2 = HY.HybridEngine(args.db, profile="sound", head_file=path)
        results["versions"][f"{name} head in the sound profile blend"] = measure(
            f"{name} head in the sound profile blend", e2._score, held, lists, known_mask, full_mask)
    if args.out:
        json.dump(results, open(args.out, "w"), indent=1)
        print(f"saved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
