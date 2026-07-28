r"""Phase 1 of the behavioral head -- mine the operator's own listening signal into
(anchor, positive) pairs, and COUNT it honestly before anything gets trained.

The premise of a personal taste engine is that there exists a behavioural record of what
the operator actually likes, independent of any similarity engine's opinion. Two places
such a record could live on this machine:

  Source A  playlist co-occurrence -- every .m3u/.m3u8 under the configured playlist dir.
            Two tracks the operator put in the same playlist are a weak positive; two
            tracks he put NEXT TO each other are a stronger one.
  Source B  usermeta (web/userdata.py) -- loved / rating / play_count / skip_count, the
            in-app signal. loved+loved and rating>=4 co-membership are positives;
            skip_count>=2 tracks are hard negatives.

WHAT THIS SCRIPT IS FOR: producing the counts that decide whether training is honest.
A head trained on too little signal, or on signal that is really another engine's output
wearing a playlist's clothes, is worse than no head -- it launders the teacher's opinion
back as "your taste". So this script also classifies each playlist by PROVENANCE and
reports the machine-generated share separately. Read SIGNAL_REPORT.md before trusting a
number from here.

Exclusions (both are machine-generated evaluation artifacts, never taste):
  * any directory whose name starts with "ABTest" (ABTest\, ABTest_v3\) -- our own
    sealed ear-test sets, written by eval/abtest.py.
  * non-local entries (http://, radio streams).

Reuse, not reinvent: path reconciliation between the DB's L:\_MUSIC\... paths and the
playlists' \\DiskStation\..., /mnt/Diskstation/..., M:\... mirrors is
src/musicip_engine.relkey -- the same mirror-invariant MusicIPAdapter and
eval/bakeoff_musicip.py already use. No new path logic here.

Runs under the LEAN venv (sqlite3 + numpy only, no torch).

usage:
  python eval/build_behavior_pairs.py --db ..\mixer-ng\data\mixer.db \
      --playlists "M:\_LAN-Playlists" --out eval/behavior_pairs.json
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import sys

if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
    # only rewrap a non-UTF-8 console; rewrapping an already-wrapped stdout detaches the
    # buffer the first wrapper owns and every later print raises (bites on import).
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # attune/

# --- prior art: the mirror-invariant relkey used by MusicIPAdapter / bakeoff_musicip ---
_spec = importlib.util.spec_from_file_location(
    "musicip_engine", os.path.join(ROOT, "src", "musicip_engine.py"))
_mip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mip)
relkey = _mip.relkey

PLAYLIST_EXT = (".m3u", ".m3u8")
EXCLUDE_DIR_PREFIX = "abtest"          # ABTest\, ABTest_v3\ -- our own generated test sets

# GO/NO-GO thresholds (set by the task brief, not by this script's results)
GATE_TRACKS = 1500
GATE_PAIRS = 20000


# --------------------------------------------------------------------------- pool
def load_pool(db_path):
    """The tracks a head could actually be trained on: those with BOTH CLAP-512 and a
    finite librosa-79, i.e. exactly HybridEngine's clap-intersect-librosa pool (hybrid.py
    __init__). Returned as {relkey: db_path} plus the ordered path list."""
    import numpy as np
    con = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True)
    lib = set()
    for p, blob, dim in con.execute(
            "SELECT path,vec,dim FROM features WHERE vec IS NOT NULL AND error IS NULL"):
        if dim != 79:
            continue
        v = np.frombuffer(blob, np.float32)
        if v.shape[0] == 79 and np.isfinite(v).all():
            lib.add(p)
    paths = []
    for p, dim, blob in con.execute("SELECT path,dim,vec FROM clap WHERE vec IS NOT NULL"):
        if dim == 512 and p in lib:
            paths.append(p)
    con.close()
    paths.sort()
    by_relkey = {}
    collisions = 0
    for p in paths:
        k = relkey(p)
        if k in by_relkey:
            collisions += 1
            continue
        by_relkey[k] = p
    return paths, by_relkey, collisions


def load_usermeta(db_path):
    con = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True)
    rows = {}
    try:
        for p, r, lv, pc, sc, lp in con.execute(
                "SELECT path,rating,loved,play_count,skip_count,last_played FROM usermeta"):
            rows[p] = {"rating": r or 0, "loved": lv or 0, "play_count": pc or 0,
                       "skip_count": sc or 0, "last_played": lp or 0}
    except sqlite3.OperationalError:
        pass
    con.close()
    return rows


# --------------------------------------------------------------------------- playlists
def is_excluded(rel_dir):
    parts = rel_dir.replace("\\", "/").split("/")
    return any(seg.lower().startswith(EXCLUDE_DIR_PREFIX) for seg in parts if seg)


def find_playlists(root, include_bak=True):
    """Every playlist file under `root` minus the ABTest subtrees. `.m3u.bak` files are
    included by default: they are byte-copies of the live lists (verified -- they dedupe
    away by content fingerprint), so including them costs nothing and guards against a
    list that only survives as a backup."""
    found, excluded = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        rel = "" if rel == "." else rel
        if is_excluded(rel):
            excluded.extend(os.path.join(dirpath, f) for f in filenames
                            if _is_playlist_name(f, include_bak))
            dirnames[:] = []
            continue
        for f in filenames:
            if _is_playlist_name(f, include_bak):
                found.append(os.path.join(dirpath, f))
    return sorted(found), sorted(excluded)


def _is_playlist_name(fn, include_bak):
    low = fn.lower()
    if low.endswith(PLAYLIST_EXT):
        return True
    if include_bak and (low.endswith(".m3u.bak") or low.endswith(".m3u8.bak")):
        return True
    return False


def classify(pl_path, root):
    """Provenance heuristic, name-based and deliberately conservative.

    A file called `like-<seed>` / `Like-<seed>` holding a round 50/100/150 tracks is the
    output shape of a similarity engine ("make me 100 tracks like this one" -- MusicIP,
    or MusicBee's Similar-To, which is MusicIP-backed). Those lists record the TEACHER's
    opinion of what goes with a seed, not the operator's. Counting them as taste would
    re-distill MusicIP, which is what src/models/metric_head.onnx already is.

    Returns "machine_similarity" or "curated_or_unknown"."""
    base = os.path.basename(pl_path).lower()
    rel = os.path.relpath(pl_path, root).replace("\\", "/").lower()
    if base.startswith("like-") or "-like-" in base or rel.startswith("_similar to/"):
        return "machine_similarity"
    return "curated_or_unknown"


def parse_playlist(path):
    """-> (entries, n_comment, n_url). Entries are raw path lines in file order."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return [], 0, 0
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1", "replace")
    entries, n_comment, n_url = [], 0, 0
    for line in text.splitlines():
        s = line.strip().lstrip("\ufeff")
        if not s:
            continue
        if s.startswith("#"):
            n_comment += 1
            continue
        low = s.lower()
        if low.startswith(("http://", "https://", "mms://", "rtsp://")):
            n_url += 1
            continue
        entries.append(s)
    return entries, n_comment, n_url


# --------------------------------------------------------------------------- mining
def mine(args):
    print(f"DB        : {args.db}")
    print(f"playlists : {args.playlists}")
    print()

    paths, by_relkey, collisions = load_pool(args.db)
    print(f"analysis pool (CLAP-512 AND librosa-79): {len(paths):,} tracks")
    if collisions:
        print(f"  note: {collisions} pool paths share a relkey with an earlier one "
              f"(mirror duplicates); first-wins")
    idx = {p: i for i, p in enumerate(paths)}

    pls, excluded = find_playlists(args.playlists, include_bak=not args.no_bak)
    print(f"playlist files found      : {len(pls)}")
    print(f"playlist files EXCLUDED   : {len(excluded)}  (ABTest* subtrees, machine-generated)")

    # ---- parse + resolve
    lists = []                      # {file, kind, entries, resolved:[pool_i], fingerprint}
    tot_lines = tot_res = tot_url = 0
    unresolved_samples = []
    for f in pls:
        entries, n_comment, n_url = parse_playlist(f)
        tot_lines += len(entries)
        tot_url += n_url
        resolved, unres = [], 0
        for e in entries:
            dbp = by_relkey.get(relkey(e))
            i = idx.get(dbp) if dbp else None
            if i is None:
                unres += 1
                if len(unresolved_samples) < 12:
                    unresolved_samples.append(e)
                continue
            resolved.append(i)
        tot_res += len(resolved)
        # fingerprint on the RESOLVED sequence: the same list saved four times under four
        # path mirrors fingerprints identically and is counted once.
        fp = hashlib.sha1(",".join(map(str, resolved)).encode()).hexdigest()
        lists.append({"file": os.path.relpath(f, args.playlists), "kind": classify(f, args.playlists),
                      "n_entries": len(entries), "n_resolved": len(resolved),
                      "n_unresolved": unres, "resolved": resolved, "fp": fp})

    print(f"path lines (local, non-comment): {tot_lines:,}")
    print(f"  resolved to a pool track     : {tot_res:,}"
          f"  ({100.0*tot_res/max(tot_lines,1):.1f}%)")
    print(f"  unresolved                   : {tot_lines-tot_res:,}")
    print(f"  stream/URL lines skipped     : {tot_url:,}")
    if unresolved_samples:
        print("  unresolved samples:")
        for s in unresolved_samples[:6]:
            print(f"    {s}")

    # ---- dedupe identical lists
    seen, uniq, dup = {}, [], 0
    for L in lists:
        if L["n_resolved"] < args.min_tracks:
            continue
        if L["fp"] in seen:
            seen[L["fp"]]["copies"].append(L["file"])
            dup += 1
            continue
        L["copies"] = []
        seen[L["fp"]] = L
        uniq.append(L)
    print()
    print(f"lists with >= {args.min_tracks} resolved tracks : "
          f"{sum(1 for L in lists if L['n_resolved'] >= args.min_tracks)}")
    print(f"  distinct after content-dedupe        : {len(uniq)}   "
          f"(exact duplicate copies dropped: {dup})")

    by_kind = collections.Counter(L["kind"] for L in uniq)
    print(f"  by provenance: {dict(by_kind)}")
    print()
    print(f"{'kind':20} {'n':>5}  file")
    for L in sorted(uniq, key=lambda x: (-x["n_resolved"], x["file"])):
        cop = f"  (+{len(L['copies'])} identical copies)" if L["copies"] else ""
        print(f"{L['kind']:20} {L['n_resolved']:5}  {L['file']}{cop}")

    # ---- Source A: co-occurrence pairs
    def pairs_from(subset):
        acc = {}
        for L in subset:
            r = L["resolved"]
            for a in range(len(r)):
                for b in range(a + 1, len(r)):
                    ia, ib = r[a], r[b]
                    if ia == ib:
                        continue
                    w = args.adj_weight if (b - a) <= args.adj_window else 1.0
                    key = (ia, ib) if ia < ib else (ib, ia)
                    acc[key] = acc.get(key, 0.0) + w
        return acc

    curated = [L for L in uniq if L["kind"] == "curated_or_unknown"]
    machine = [L for L in uniq if L["kind"] == "machine_similarity"]
    pa_all = pairs_from(uniq)
    pa_cur = pairs_from(curated)

    def cover(acc):
        s = set()
        for a, b in acc:
            s.add(a); s.add(b)
        return s

    cov_all, cov_cur = cover(pa_all), cover(pa_cur)
    print()
    print("=" * 74)
    print("SOURCE A -- playlist co-occurrence")
    print("=" * 74)
    print(f"  ALL non-ABTest lists ({len(uniq)} distinct)")
    print(f"    positive pairs        : {len(pa_all):,}")
    print(f"    distinct tracks       : {len(cov_all):,}  "
          f"({100.0*len(cov_all)/max(len(paths),1):.1f}% of the {len(paths):,}-track pool)")
    print(f"  CURATED ONLY ({len(curated)} distinct, machine-similarity lists removed)")
    print(f"    positive pairs        : {len(pa_cur):,}")
    print(f"    distinct tracks       : {len(cov_cur):,}")
    print(f"  machine-similarity lists excluded from the curated view: {len(machine)}")

    # ---- Source B: usermeta
    um = load_usermeta(args.db)
    in_pool = {p: v for p, v in um.items() if p in idx}
    loved = [idx[p] for p, v in in_pool.items() if v["loved"] == 1]
    rated4 = [idx[p] for p, v in in_pool.items() if v["rating"] >= 4]
    played = [idx[p] for p, v in in_pool.items() if v["play_count"] >= 1]
    skipped2 = [idx[p] for p, v in in_pool.items() if v["skip_count"] >= 2]
    any_sig = set(loved) | set(rated4) | set(played) | set(skipped2)
    pb = {}
    for grp in (loved, rated4):
        for a in range(len(grp)):
            for b in range(a + 1, len(grp)):
                ia, ib = sorted((grp[a], grp[b]))
                if ia != ib:
                    pb[(ia, ib)] = pb.get((ia, ib), 0.0) + 1.0
    print()
    print("=" * 74)
    print("SOURCE B -- usermeta (in-app behaviour)")
    print("=" * 74)
    print(f"  usermeta rows total       : {len(um):,}  (in pool: {len(in_pool):,})")
    print(f"  loved = 1                 : {len(loved)}")
    print(f"  rating >= 4               : {len(rated4)}")
    print(f"  play_count >= 1           : {len(played)}")
    print(f"  skip_count >= 2 (hard neg): {len(skipped2)}")
    print(f"  tracks with ANY signal    : {len(any_sig)}")
    print(f"  positive pairs from B     : {len(pb):,}")

    # ---- combined + gate
    combined = dict(pa_all)
    for k, v in pb.items():
        combined[k] = combined.get(k, 0.0) + v
    cov_comb = cover(combined)
    n_pairs, n_tracks = len(combined), len(cov_comb)

    print()
    print("=" * 74)
    print("GO / NO-GO GATE")
    print("=" * 74)
    print(f"  combined positive pairs   : {n_pairs:,}   (gate: >= {GATE_PAIRS:,})"
          f"   {'PASS' if n_pairs >= GATE_PAIRS else 'FAIL'}")
    print(f"  distinct tracks covered   : {n_tracks:,}   (gate: >= {GATE_TRACKS:,})"
          f"   {'PASS' if n_tracks >= GATE_TRACKS else 'FAIL'}")
    numeric_go = n_pairs >= GATE_PAIRS and n_tracks >= GATE_TRACKS
    print(f"  numeric verdict           : {'GO' if numeric_go else 'NO-GO'}")
    print()
    print("  PROVENANCE CHECK (the gate the numbers alone cannot express):")
    frac_machine = len(machine) / max(len(uniq), 1)
    pairs_machine = len(pa_all) - len(pa_cur)
    print(f"    distinct lists that are machine similarity output: "
          f"{len(machine)}/{len(uniq)} ({100*frac_machine:.0f}%)")
    print(f"    pairs contributed ONLY by those lists            : "
          f"{pairs_machine:,} of {len(pa_all):,} "
          f"({100.0*pairs_machine/max(len(pa_all),1):.0f}%)")
    print(f"    curated-only pairs / tracks                      : "
          f"{len(pa_cur):,} / {len(cov_cur):,}")

    out = {
        "gate": {"pairs": n_pairs, "tracks": n_tracks,
                 "gate_pairs": GATE_PAIRS, "gate_tracks": GATE_TRACKS,
                 "numeric_verdict": "GO" if numeric_go else "NO-GO"},
        "pool_size": len(paths),
        "playlists": {"found": len(pls), "excluded_abtest": len(excluded),
                      "distinct_after_dedupe": len(uniq), "duplicate_copies": dup,
                      "by_kind": dict(by_kind),
                      "lines_total": tot_lines, "lines_resolved": tot_res,
                      "lines_url_skipped": tot_url},
        "source_a": {"pairs_all": len(pa_all), "tracks_all": len(cov_all),
                     "pairs_curated": len(pa_cur), "tracks_curated": len(cov_cur),
                     "machine_lists": len(machine), "curated_lists": len(curated)},
        "source_b": {"loved": len(loved), "rating_ge4": len(rated4),
                     "played_ge1": len(played), "skipped_ge2": len(skipped2),
                     "any_signal": len(any_sig), "pairs": len(pb)},
        "config": {"adj_window": args.adj_window, "adj_weight": args.adj_weight,
                   "min_tracks": args.min_tracks, "include_bak": not args.no_bak,
                   "exclude_dir_prefix": EXCLUDE_DIR_PREFIX},
        "lists": [dict({k: L[k] for k in ("file", "kind", "n_entries", "n_resolved",
                                          "n_unresolved")}, copies=len(L["copies"]))
                  for L in uniq],
    }
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        payload = dict(out)
        if args.emit_pairs:
            payload["pairs"] = [[a, b, round(w, 3)] for (a, b), w in sorted(combined.items())]
            payload["hard_negatives"] = sorted(skipped2)
            payload["paths"] = paths
            # per-list membership in FILE ORDER: the only grouping that lets a consumer do an
            # honest LIST-level holdout. Pairs inside one list are not independent samples of
            # taste -- the list is. See SIGNAL_REPORT.md.
            payload["list_members"] = [{"file": L["file"], "kind": L["kind"],
                                        "rows": L["resolved"]} for L in uniq]
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        print(f"\nwrote {args.out}"
              f"{' (with pair list)' if args.emit_pairs else ' (counts only; --emit-pairs for the pairs)'}")
    return out


def main():
    ap = argparse.ArgumentParser(description="Phase 1: mine behavioural pairs and count the signal")
    ap.add_argument("--db", required=True, help="mixer.db (opened READ-ONLY)")
    ap.add_argument("--playlists", required=True, help="playlist root (read-only)")
    ap.add_argument("--out", default=os.path.join(HERE, "behavior_pairs.json"))
    ap.add_argument("--emit-pairs", action="store_true",
                    help="also write the pair list (large); default writes counts only")
    ap.add_argument("--adj-window", type=int, default=3,
                    help="|pos_i - pos_j| <= this counts as adjacent")
    ap.add_argument("--adj-weight", type=float, default=2.0, help="weight for adjacent pairs")
    ap.add_argument("--min-tracks", type=int, default=5,
                    help="a list needs this many resolved tracks to be usable")
    ap.add_argument("--no-bak", action="store_true", help="skip .m3u.bak files")
    mine(ap.parse_args())


if __name__ == "__main__":
    main()
