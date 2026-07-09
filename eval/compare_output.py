"""Side-by-side OUTPUT comparison: MusicIP mix vs our V2/CLAP mix, same seed.

The honest way to judge the two engines is to look at what they actually return, not a
single (biased) metric. For each seed this prints both engines' picks next to each other
plus a couple of descriptive properties (distinct artists / genres in the returned set).
No quality claim is made -- you decide by eye/ear.

Run:  python attune/eval/compare_output.py --db mixer-ng/data/mixer.db --seed "black dog"
      python attune/eval/compare_output.py --db mixer-ng/data/mixer.db          (samples a few)
"""
import argparse
import importlib.util
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


HY = _load("hybrid", os.path.join(SRC, "hybrid.py"))
ME = _load("musicip_engine", os.path.join(SRC, "musicip_engine.py"))


def _label(eng, p):
    m = eng.meta.get(p, {})
    return f"{m.get('artist') or '?'} - {m.get('title') or os.path.basename(p)}"


def _props(eng, paths):
    arts = {(eng.meta.get(p, {}).get('artist') or '').lower() for p in paths}
    gens = set()
    for p in paths:
        g = (eng.meta.get(p, {}).get('genre') or '')
        gens |= {t.strip().lower() for t in g.replace('/', ',').split(',') if t.strip()}
    return len([a for a in arts if a]), len(gens)


def main():
    ap = argparse.ArgumentParser(description="MusicIP vs V2 mix output, side by side")
    ap.add_argument("--db", required=True)
    ap.add_argument("--url", default="http://localhost:10002")
    ap.add_argument("--seed", help="name/artist substring; if omitted, samples a few")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--samples", type=int, default=3)
    args = ap.parse_args()

    eng = HY.HybridEngine(args.db)
    mip = ME.MusicIPEngine(args.url)
    if not mip.alive():
        raise SystemExit("MusicIP not responding on " + args.url)
    recon = ME.build_reconciler(eng.paths)
    mip_by_relkey = {ME.relkey(p): p for p in mip.songs()}

    if args.seed:
        s = args.seed.lower()
        seeds = [i for i, p in enumerate(eng.paths) if s in _label(eng, p).lower()
                 and ME.relkey(p) in mip_by_relkey][:1]
        if not seeds:
            raise SystemExit(f"no seed matching '{args.seed}' that MusicIP also knows")
    else:
        shared = [i for i, p in enumerate(eng.paths) if ME.relkey(p) in mip_by_relkey]
        seeds = random.Random(7).sample(shared, min(args.samples, len(shared)))

    for si in seeds:
        seed_p = eng.paths[si]
        v2 = eng.mix(seed_p, size=args.n) or []
        seed_unc = mip_by_relkey[ME.relkey(seed_p)]
        mip_unc = mip.similar(seed_unc, size=args.n)
        mip_idx = [recon.get(ME.relkey(p)) for p in mip_unc]
        mip_paths = [eng.paths[j] for j in mip_idx if j is not None][:args.n]

        va, vg = _props(eng, v2)
        ma, mg = _props(eng, mip_paths)
        print("\n" + "=" * 92)
        print(f"SEED: {_label(eng, seed_p)}")
        print(f"{'V2 / CLAP  (artists=' + str(va) + ' genres=' + str(vg) + ')':<46}"
              f"{'MusicIP  (artists=' + str(ma) + ' genres=' + str(mg) + ')'}")
        print("-" * 92)
        for r in range(max(len(v2), len(mip_paths))):
            l = _label(eng, v2[r])[:44] if r < len(v2) else ""
            rr = _label(eng, mip_paths[r])[:44] if r < len(mip_paths) else ""
            print(f"{l:<46}{rr}")


if __name__ == "__main__":
    main()
