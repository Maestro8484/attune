r"""Where the MusicIP profile's cut-off belongs, measured on the held-out seeds (step 4 of
HANDOFF_MUSICIP_REACH.md).

The fit line turns the count into a maximum: a song whose fit (its score as a share of the
seed's own) sits under the line is not taken. MusicIP has no such line; it fills the count for
every seed it knows. So the question is where, if anywhere, the fit stops saying anything about
MusicIP's choice. For every held-out seed, every MusicIP-known candidate is put in a fit band,
and the share of each band's songs that MusicIP picked is counted. Where that share falls to
what a random song gets (99 picks over the known pool), the line has nothing left to say, and
that is where it goes. Also printed: the fit of each seed's 100th song (the rule the other two
lines were placed by) and how many songs a 100-song walk would keep at candidate lines.

Lean venv, numpy only, read-only. From the workspace root:
  mixer-ng\.venv\Scripts\python.exe attune\eval\musicip_line.py --db mixer-ng\data\mixer.db ^
      --teacher audit-collections\musicip-teacher-2026-10-06\teacher_style5_all.json ^
      --head attune\src\models\musicip_head.npz --out audit-sonic\musicip_line.json
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys

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
AG = _load("musicip_agreement", os.path.join(HERE, "musicip_agreement.py"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--teacher", required=True)
    ap.add_argument("--head", default=None, help="head file; default the engine's MUSICIP_HEAD_FILE")
    ap.add_argument("--profile", default="musicip")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    eng = HY.HybridEngine(args.db, profile=args.profile, head_file=args.head)
    N = len(eng.paths)
    recon = ME.build_reconciler(eng.paths)
    held, lists, known, setting = AG.load_teacher(args.teacher, recon)
    known_mask = np.zeros(N, bool)
    known_mask[list(known)] = True
    print(f"profile {eng.profile}; pool {N}; known {int(known_mask.sum())}; held-out seeds {len(held)}")
    base = 99.0 / known_mask.sum()
    edges = np.round(np.arange(1.0, -0.001, -0.05), 2)          # 1.00, 0.95, ... 0.00
    hits = np.zeros(len(edges) - 1)
    tot = np.zeros(len(edges) - 1)
    fit100, fit50, fit25 = [], [], []
    per_seed = {}
    for si in held:
        s = eng._score(si)
        ceiling = float(s[si]) if s[si] > 1e-9 else 1.0
        fit = s / ceiling
        fit[si] = -np.inf
        cand = np.flatnonzero(known_mask)
        cand = cand[cand != si]
        f = fit[cand]
        picked = np.zeros(N, bool)
        picked[lists[si]] = True
        p = picked[cand]
        for b in range(len(edges) - 1):
            m = (f <= edges[b]) & (f > edges[b + 1]) if b else (f > edges[b + 1])
            tot[b] += m.sum()
            hits[b] += (m & p).sum()
        order = np.sort(f)[::-1]
        fit100.append(float(order[99]))
        fit50.append(float(order[49]))
        fit25.append(float(order[24]))
        per_seed[int(si)] = {"fit25": float(order[24]), "fit50": float(order[49]), "fit100": float(order[99])}
    print(f"\nrandom share of a band that MusicIP picked: {base:.4f} ({99} of {int(known_mask.sum())})")
    print(f"{'fit band':>14} {'songs':>9} {'picked':>7} {'share':>7} {'x random':>9}")
    bands = []
    for b in range(len(edges) - 1):
        if tot[b] == 0:
            continue
        share = hits[b] / tot[b]
        bands.append({"hi": float(edges[b]), "lo": float(edges[b + 1]), "songs": int(tot[b]),
                      "picked": int(hits[b]), "share": float(share), "x_random": float(share / base)})
        print(f"{edges[b + 1]:.2f} to {edges[b]:.2f} {int(tot[b]):9d} {int(hits[b]):7d} {share:7.4f} {share / base:9.1f}")
    q = lambda a: {"p5": float(np.percentile(a, 5)), "p25": float(np.percentile(a, 25)),
                   "median": float(np.median(a)), "p75": float(np.percentile(a, 75))}
    print(f"\nfit of the 25th song: {q(fit25)}\nfit of the 50th song: {q(fit50)}\nfit of the 100th song: {q(fit100)}")
    # the lines a 100-song walk would feel: how many of 100 fit at each candidate line
    counts = {}
    for line in (0.70, 0.65, 0.60, 0.55, 0.50, 0.45, 0.40, 0.35, 0.30):
        kept = []
        for si in held:
            s = eng._score(si)
            fit = s / (float(s[si]) if s[si] > 1e-9 else 1.0)
            fit[si] = -np.inf
            fit[~known_mask] = -np.inf
            kept.append(int(min(100, (fit >= line).sum())))
        kept = np.array(kept)
        counts[str(line)] = {"median": float(np.median(kept)), "reach100": int((kept >= 100).sum()),
                             "under10": int((kept < 10).sum()), "n": int(len(kept))}
        print(f"line {line:.2f}: of 100 asked, median kept {np.median(kept):.0f}; {int((kept >= 100).sum())} of {len(kept)} seeds reach 100; {int((kept < 10).sum())} get under 10")
    out = {"profile": eng.profile, "pool": N, "known": int(known_mask.sum()), "held_out": len(held),
           "random_share": base, "bands": bands, "fit25": q(fit25), "fit50": q(fit50), "fit100": q(fit100),
           "kept_at_line": counts}
    if args.out:
        json.dump(out, open(args.out, "w"), indent=1)
        print(f"saved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
