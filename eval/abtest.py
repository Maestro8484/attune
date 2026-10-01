"""Blind A/B listening-test harness -- the LAW 1 ship gate, engine-agnostic.

For N genre-diverse seeds, export one anonymized .m3u8 per requested engine
(letters A/B/... randomly assigned per seed) into the playlist folder, and seal
the engine->letter map into a timestamped key file under attune/eval/. The
operator listens blind, then runs --score to rank each test interactively; the
key is unsealed only at scoring time and the verdicts are appended to
attune/eval/abtest_results.jsonl.

Protocol ported from make_abtest.py, in the private MusicIP research folder and not
in this repository (the
harness that ran the original five-engine bakeoff); the engine wiring here goes
through src/engine.py's common Engine contract instead of that repo's ad-hoc
matrices, so any engine satisfying search()/similar() can enter a test.

LAW 1: a retrieval metric may be reported, never used to select what ships --
this harness produces the evidence that IS allowed to decide.
LAW 3: every key file records each engine's candidate-pool size next to it.

This script only READS the db (HybridEngine opens it read-only). It writes
only under attune/eval/ and the playlist output folder.

Engines: v2, musicip, learned, and the scoring versions in eval/variants.py (v2-fused,
v2-feel, v2-noclap, v2-feel-file). A name may end in @<label> to run that version over a
second copy of the library named with --db-alt label=path (same songs, different stored
fingerprints), so two analyses of one library can be heard side by side.

Usage:
    python attune/eval/abtest.py --engines v2,learned --n-seeds 5 --k 15
    python attune/eval/abtest.py --engines v2,v2-fused,v2-noclap --n-seeds 3 --k 20
    python attune/eval/abtest.py --score            # rank the latest key file, typed in
    python attune/eval/abtest.py --score --rank "Test1=B>A=C" --rank "Test2=A>B>C"
                                                    # the same, with the rankings given
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config as cfgmod   # noqa: E402  (path shimmed above)
import variants as variantmod   # noqa: E402

# Default export folder when neither --out nor settings.json names one. Repo-local
# on purpose: an eval script should never write into somebody's real library.
FALLBACK_OUT = os.path.join(_HERE, "abtest_out")
RESULTS_PATH = os.path.join(_HERE, "abtest_results.jsonl")


def _load_engine_iface():
    """Import src/engine.py by path, same as web/app.py's _load_engine_iface() --
    its @dataclass needs the module registered in sys.modules."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("engine", os.path.join(_SRC, "engine.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["engine"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Engine construction
# ---------------------------------------------------------------------------

def build_engines(names, db_path, musicip_url, feel_file=None, db_alt=None):
    """Build each requested engine over ONE shared HybridEngine pool (the db load is
    the expensive part; every Engine subclass resolves results to this pool anyway).
    A name ending in @<label> is built over the library copy `db_alt[label]` instead,
    loaded once per label. Returns (hybrid_eng, {name: (engine_obj, pool_size)});
    engine_obj.eng is the HybridEngine whose pool that engine's results index."""
    eiface = _load_engine_iface()
    import importlib.util
    spec = importlib.util.spec_from_file_location("hybrid", os.path.join(_SRC, "hybrid.py"))
    hy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hy)
    print(f"loading library db (this is the slow part): {db_path}")
    eng = hy.HybridEngine(db_path)

    out = {}
    alt_engines = {}
    for name in names:
        kind, _, label = name.partition("@")
        base = eng
        if label:
            if label not in (db_alt or {}):
                raise SystemExit(f"engine '{name}' needs --db-alt {label}=<path>")
            if label not in alt_engines:
                print(f"loading library copy '{label}': {db_alt[label]}")
                alt_engines[label] = hy.HybridEngine(db_alt[label])
            base = alt_engines[label]
        if kind in variantmod.VARIANTS:
            hv = base if (kind == "v2" and not label) else variantmod.variant_engine(
                base, kind, feel_file=feel_file)
            obj = eiface.V2Engine(hybrid_engine=hv)
            pool = len(hv.paths)
        elif label:
            raise SystemExit(f"only the v2 scoring versions can take @{label}, not '{kind}'")
        elif name == "musicip":
            obj = eiface.MusicIPAdapter(eng.paths, url=musicip_url, log=print)
            obj.attach_meta(eng.meta)
            pool = len(obj.recon)
        elif name == "learned":
            obj = eiface.LearnedEngine(db_path, hybrid_engine=eng, log=print)
            pool = obj.pool_size
        else:
            raise SystemExit(f"unknown engine '{name}' (known: musicip, learned, "
                             f"{', '.join(variantmod.VARIANTS)})")
        out[name] = (obj, pool)
        print(f"engine '{name}' ready, candidate pool = {pool} tracks")
    return eng, out


# ---------------------------------------------------------------------------
# Seed selection: genre-diverse, deterministic, inside EVERY engine's coverage
# ---------------------------------------------------------------------------

def pick_seeds(eng, engines, n_seeds, k, rng, seed_paths=None):
    """Shuffle the shared pool deterministically, then take the first seed of each
    primary genre whose similar() probe yields a full k-track mix from EVERY
    requested engine. The probe result is kept so the mix isn't computed twice.
    `seed_paths`, when given, are the seeds instead, in that order (a test aimed at
    the songs a change touches); each must still give a full mix from every engine."""
    if seed_paths:
        missing = [p for p in seed_paths if p not in eng.idx]
        if missing:
            raise SystemExit(f"seed not in the mixable pool: {missing}")
        order = [eng.idx[p] for p in seed_paths]
        n_seeds = len(order)
    else:
        order = list(range(len(eng.paths)))
        rng.shuffle(order)
    seeds, used_genres = [], set()
    for i in order:
        p = eng.paths[i]
        g = (eng.meta.get(p, {}).get("genre") or "?")
        g0 = g.split(",")[0].split(";")[0].strip().lower() or "?"
        if g0 in used_genres and not seed_paths:
            continue
        mixes = {}
        for name, (obj, _pool) in engines.items():
            # an engine over a library copy has its own pool order: go by path. The
            # MusicIP adapter has no .eng and indexes the shared pool.
            own = getattr(obj, "eng", eng)
            oi = i if own.paths is eng.paths else own.idx.get(p)
            try:
                refs = obj.similar(oi, size=k) if oi is not None else []
            except Exception as e:
                print(f"  probe {name} failed on pool_i={i}: {e}")
                refs = []
            if not refs or len(refs) < k:
                mixes = None
                break
            mixes[name] = [own.paths[r.pool_i] for r in refs[:k]]
        if not mixes:
            continue
        used_genres.add(g0)
        seeds.append((i, p, mixes))
        if len(seeds) >= n_seeds:
            break
    if len(seeds) < n_seeds:
        raise SystemExit(f"only found {len(seeds)}/{n_seeds} seeds covered by all "
                         f"engines with k={k} -- lower --k or --n-seeds")
    return seeds


# ---------------------------------------------------------------------------
# Output: .m3u8 exactly in make_abtest.py's proven format
# ---------------------------------------------------------------------------

def write_m3u(path, tracks, meta):
    """UTF-8 BOM + CRLF + #EXTM3U/#EXTINF with absolute paths -- the format the
    LAN players already accept from make_abtest.py."""
    lines = ["#EXTM3U"]
    for t in tracks:
        m = meta.get(t, {})
        a = m.get("artist") or "?"
        ti = m.get("title") or os.path.basename(t)
        lines.append(f"#EXTINF:-1,{a} - {ti}")
        lines.append(t)
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")


def _label_for_seed(eng, path, fallback):
    m = eng.meta.get(path, {})
    name = f"{m.get('artist') or ''} {m.get('title') or ''}".strip() or fallback
    return "".join(c for c in name if c.isalnum() or c in " -")[:30].strip() or fallback


def _scoring_of(obj):
    """The scoring switches an engine ran with, for the key file; None for an engine
    that is not a V2 scoring version."""
    h = getattr(obj, "eng", None)
    if h is None or not hasattr(h, "fusion") or not hasattr(obj, "set_weights"):
        return None
    return {"fusion": h.fusion, "clap_space": h.clap_space,
            "weights": {k: v for k, v in h.w.items() if v}}


def run_generate(args):
    names = [s.strip().lower() for s in args.engines.split(",") if s.strip()]
    if len(names) != len(set(names)):
        raise SystemExit("duplicate engine names in --engines")
    settings = cfgmod.load()
    db = cfgmod.effective(args.db, "ATTUNE_DB", "db_path", settings)
    if not db or not os.path.exists(db):
        raise SystemExit(f"db not found: {db!r} (pass --db or set db_path in settings.json)")
    out_dir = args.out or (os.path.join(settings.get("playlist_dir"), "ABTest")
                           if settings.get("playlist_dir") else FALLBACK_OUT)
    musicip_url = settings.get("musicip_url") or "http://localhost:10002"

    rng = random.Random(args.rng_seed)
    db_alt = {}
    for spec in (args.db_alt or []):
        label, _, path = spec.partition("=")
        if not label or not os.path.exists(path):
            raise SystemExit(f"--db-alt wants label=<existing path>, got {spec!r}")
        db_alt[label] = path
    eng, engines = build_engines(names, db, musicip_url, feel_file=args.feel_file,
                                 db_alt=db_alt)
    seed_paths = None
    if args.seeds:
        with open(args.seeds, encoding="utf-8-sig") as fh:
            seed_paths = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    seeds = pick_seeds(eng, engines, args.n_seeds, args.k, rng, seed_paths)
    print(f"seeds: {[_label_for_seed(eng, p, f'seed{i}') for i, p, _ in seeds]}")

    os.makedirs(out_dir, exist_ok=True)
    letters_all = [chr(ord("A") + j) for j in range(len(names))]
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    key = {
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "db": db,
        "playlist_dir": out_dir,
        "rng_seed": args.rng_seed,
        "k": args.k,
        # LAW 3: pool size stamped per engine, plus a version id when one is exposed
        "engines": {n: {"pool_size": pool,
                        "version": getattr(obj, "version", None),
                        "scoring": _scoring_of(obj),
                        "db": db_alt.get(n.partition("@")[2], db)}
                    for n, (obj, pool) in engines.items()},
        "tests": {},
    }
    for ti, (pool_i, seed_path, mixes) in enumerate(seeds, 1):
        letters = letters_all[:]
        rng.shuffle(letters)
        assign = dict(zip(letters, names))          # letter -> engine, random per seed
        label = _label_for_seed(eng, seed_path, f"seed{ti}")
        for letter in sorted(assign):
            write_m3u(os.path.join(out_dir, f"Test{ti} [{label}] - {letter}.m3u8"),
                      mixes[assign[letter]], eng.meta)
        key["tests"][f"Test{ti}"] = {
            "label": label,
            "seed": seed_path,
            "letter_to_engine": assign,
        }
        print(f"  Test{ti} [{label}]: {len(assign)} blinded sets written")

    key_path = os.path.join(_HERE, f"abtest_key_{ts}.json")
    with open(key_path, "w", encoding="utf-8") as fh:
        json.dump(key, fh, indent=1, ensure_ascii=False)
    print(f"\nwrote {len(seeds) * len(names)} playlists to {out_dir}")
    print(f"answer key sealed in {key_path} -- no peeking until you've ranked them")
    print(f"score with:  python {os.path.relpath(__file__)} --score")


# ---------------------------------------------------------------------------
# --score: interactive post-listen ranking, unseal, append to results.jsonl
# ---------------------------------------------------------------------------

def _parse_ranking(text, letters):
    """'B>A=C' -> [['B'], ['A','C']] (rank groups, best first). Only the test's own
    letters, each exactly once."""
    groups = []
    for grp in text.upper().replace(" ", "").split(">"):
        g = [c for c in grp.split("=") if c]
        if not g:
            return None
        groups.append(g)
    flat = [c for g in groups for c in g]
    if sorted(flat) != sorted(letters):
        return None
    return groups


def run_score(args):
    key_path = args.key
    if not key_path:
        cands = sorted(glob.glob(os.path.join(_HERE, "abtest_key_*.json")))
        if not cands:
            raise SystemExit("no abtest_key_*.json found in attune/eval/ -- generate first")
        key_path = cands[-1]
    with open(key_path, encoding="utf-8") as fh:
        key = json.load(fh)
    print(f"scoring {os.path.basename(key_path)} "
          f"({len(key['tests'])} tests, engines: {', '.join(key['engines'])})")
    print("rank the blinded sets per test, best first: e.g.  B>A   or  B=A  (tie)\n")

    # Rankings handed in on the command line (--rank "Test1=B>A=C"), for when the
    # listener answers somewhere other than this prompt. Every test must be covered.
    given = {}
    for spec in (args.rank or []):
        tname, _, ranking = spec.partition("=")
        given[tname.strip()] = ranking.strip()
    if given:
        unknown = sorted(set(given) - set(key["tests"]))
        missing = sorted(set(key["tests"]) - set(given))
        if unknown or missing:
            raise SystemExit(f"--rank must name every test once: unknown {unknown}, "
                             f"missing {missing}")
        for tname, t in key["tests"].items():
            if not _parse_ranking(given[tname], sorted(t["letter_to_engine"])):
                raise SystemExit(f"{tname}: use each of {sorted(t['letter_to_engine'])} "
                                 f"exactly once, e.g. B>A=C; got {given[tname]!r}")

    record = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "key_file": os.path.basename(key_path),
        "engines": key["engines"],          # carries pool sizes into the result (LAW 3)
        "tests": [],
    }
    for tname, t in key["tests"].items():
        letters = sorted(t["letter_to_engine"])
        while True:
            raw = given[tname] if given else input(
                f"{tname} [{t['label']}]  ({'/'.join(letters)}): ").strip()
            groups = _parse_ranking(raw, letters)
            if groups:
                break
            print(f"  use each of {letters} exactly once, e.g. {'>'.join(letters)}")
        unsealed = [[t["letter_to_engine"][c] for c in g] for g in groups]
        record["tests"].append({
            "test": tname,
            "label": t["label"],
            "seed": t["seed"],
            "ranking_letters": groups,
            "ranking_engines": unsealed,
        })
        print(f"  unsealed: {' > '.join(' = '.join(g) for g in unsealed)}")

    with open(RESULTS_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"\nappended to {RESULTS_PATH}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engines", default="v2",
                    help="comma list of engines to blind-test: v2, musicip, learned, or a "
                         "scoring version from eval/variants.py")
    ap.add_argument("--n-seeds", type=int, default=5)
    ap.add_argument("--k", type=int, default=15,
                    help="tracks per playlist -- long enough to judge, short enough to listen")
    ap.add_argument("--db", default=None, help="mixer.db (default: settings.json db_path)")
    ap.add_argument("--rng-seed", type=int, default=11,
                    help="seed for seed-pick + letter blinding (deterministic reruns)")
    ap.add_argument("--out", default=None,
                    help="playlist output dir (default: <settings playlist_dir>/ABTest)")
    ap.add_argument("--seeds", default=None,
                    help="text file of seed paths, one per line, used instead of random seeds")
    ap.add_argument("--score", action="store_true",
                    help="rank the sets from the latest (or --key) sealed key file")
    ap.add_argument("--key", default=None, help="key file to score (default: newest)")
    ap.add_argument("--rank", action="append", default=None, metavar="TestN=B>A=C",
                    help="with --score: one test's ranking, best first; repeat per test")
    ap.add_argument("--feel-file", default=None,
                    help="feel scores file for the v2-feel-file version")
    ap.add_argument("--db-alt", action="append", default=None, metavar="LABEL=PATH",
                    help="a second copy of the library for engines named <version>@LABEL")
    args = ap.parse_args()
    if args.score:
        run_score(args)
    else:
        run_generate(args)


if __name__ == "__main__":
    main()
