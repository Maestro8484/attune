"""Self-test for the common engine interface (src/engine.py): loads BOTH V2Engine (our
CLAP/librosa hybrid) and MusicIPAdapter (a live MusicIP Mixer) against the SAME DB, picks
one seed both engines can resolve, and prints the top-N similar() results from each -- all
as labels resolved to OUR pool, proving refs from either engine are interchangeable.

Read-only: HybridEngine opens the DB via a file: URI in mode=ro; MusicIPAdapter only issues
GET requests to the live MusicIP Mixer API. Nothing is written anywhere.

  python attune/eval/engine_iface_check.py --db mixer-ng/data/mixer.db
"""
from __future__ import annotations
import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # engine.py's @dataclass needs its module registered to resolve annotations
    spec.loader.exec_module(mod)
    return mod


EN = _load("engine", os.path.join(SRC, "engine.py"))


def main():
    ap = argparse.ArgumentParser(description="Load both engines, print top-N similar() for one shared seed")
    ap.add_argument("--db", required=True)
    ap.add_argument("--url", default="http://localhost:10002")
    ap.add_argument("--n", type=int, default=8)
    args = ap.parse_args()

    print(f"loading V2Engine from {args.db} ...")
    v2 = EN.V2Engine(db_path=args.db)
    print(f"  V2 pool: {len(v2.eng.paths):,} tracks; capabilities={sorted(v2.capabilities)}")

    print(f"connecting MusicIPAdapter to {args.url} ...")
    mip = EN.MusicIPAdapter(v2.eng.paths, url=args.url, log=print)
    if not mip.mip.alive():
        raise SystemExit(f"MusicIP not responding on {args.url}")
    mip.attach_meta(v2.eng.meta)
    print(f"  MusicIPAdapter ready; capabilities={sorted(mip.capabilities)}")

    # pick a seed BOTH engines can resolve: iterate our pool until MusicIP also knows it
    # (i.e. _seed_unc() resolves), so the comparison is apples-to-apples.
    seed = None
    for i in range(len(v2.eng.paths)):
        candidate = EN.Ref(pool_i=i, label=v2._labels[i])
        if mip._seed_unc(candidate) is not None:
            seed = candidate
            break
    if seed is None:
        raise SystemExit("no seed in our pool is known to MusicIP -- check reconciliation")

    print(f"\nshared seed: [{seed.pool_i}] {seed.label}\n")

    print(f"=== V2Engine.similar() top-{args.n} ===")
    v2_hits = v2.similar(seed, size=args.n)
    for r in v2_hits:
        print(f"  [{r.pool_i:6d}] {r.label}")

    print(f"\n=== MusicIPAdapter.similar() top-{args.n} ===")
    mip_hits = mip.similar(seed, size=args.n)
    for r in mip_hits:
        print(f"  [{r.pool_i:6d}] {r.label}")

    print(f"\nV2 returned {len(v2_hits)}/{args.n}; MusicIP returned {len(mip_hits)}/{args.n} "
          f"(after reconciling MusicIP's UNC results to our pool).")

    # also sanity-check search() on both, same query, over the same pool
    q = "love"
    print(f"\n=== search(\"{q}\") sanity check (first 5 each) ===")
    v2_search = v2.search(q, limit=5)
    mip_search = mip.search(q, limit=5)
    print(f"  V2Engine.search: {len(v2_search)} hits")
    for r in v2_search:
        print(f"    [{r.pool_i:6d}] {r.label}")
    print(f"  MusicIPAdapter.search: {len(mip_search)} hits")
    for r in mip_search:
        print(f"    [{r.pool_i:6d}] {r.label}")


if __name__ == "__main__":
    main()
